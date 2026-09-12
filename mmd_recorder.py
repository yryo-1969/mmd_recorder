"""MMD screen recorder: auto-detects MMD's play/stop button and records the
MMD window (whatever its current size is) to MP4 or lossless AVI.

Recording flow:
1. Ask whether to record the full MMD window or just the 3D viewport.
2. Ask whether to use lossless quality (AVI, for further editing) or MP4 (light).
3. Ask whether to include audio (system loopback audio).
4. Announce "press MMD's play button".
5. Detect play start (native button state, not screen scraping) -> start recording.
6. Detect play stop -> stop recording, mux audio if requested.
7. Ask whether to keep the result, and where to save it.
8. If a lossless AVI was saved, offer to also export a light MP4 copy.

Conversion mode: if launched with a file path argument (e.g. an AVI file
dropped onto the script/shortcut, MMDトランスフォーム-style), skip recording
entirely and just convert that AVI to MP4.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import wave
from datetime import datetime
from tkinter import filedialog, messagebox

MMD_MCP_SRC = r"E:\AI用\mmd-mcp\src"


def _resolve_tool(name, dev_path):
    """A frozen (PyInstaller) build ships ffmpeg/ffprobe next to the exe, since
    a recipient's machine won't have this dev machine's E:\\AI用\\RVC path --
    prefer that bundled copy when running frozen, else fall back to the dev path
    (local testing / running mmd_recorder.py directly)."""
    if getattr(sys, "frozen", False):
        bundled = os.path.join(os.path.dirname(sys.executable), name)
        if os.path.exists(bundled):
            return bundled
    return dev_path


FFMPEG = _resolve_tool("ffmpeg.exe", r"E:\AI用\RVC\ffmpeg.exe")
FFPROBE = _resolve_tool("ffprobe.exe", r"E:\AI用\RVC\ffprobe.exe")
# A distributed exe has no reason to know about this dev machine's personal
# VClip folder organization -- give it a generic per-user default instead.
DEFAULT_SAVE_DIR = (
    os.path.join(os.path.expanduser("~"), "Desktop", "MMDレック")
    if getattr(sys, "frozen", False)
    else r"C:\Users\akiko\OneDrive\Desktop\VClip\VClip"
)
# 60fps matches MMD's own typical refresh rate (confirmed live: a recording's
# own on-screen "XX fps" counter read ~52-60fps while capture ran at the old
# 30fps setting, so half the motion was being discarded). 30fps recordings
# are noticeably choppier than what MMD is actually doing.
FPS = 60
POLL_INTERVAL = 0.15

if not getattr(sys, "frozen", False):
    # A frozen build already has mmd_mcp bundled in via the .spec's pathex, so
    # this dev-machine-only path would just be dead weight (harmlessly ignored,
    # but pointless) in a distributed exe.
    sys.path.insert(0, MMD_MCP_SRC)
from mmd_mcp import playback  # noqa: E402
from mmd_mcp.windows import list_windows  # noqa: E402

import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402


def ask_yes_no(title, message):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    result = messagebox.askyesno(title, message, parent=root)
    root.destroy()
    return result


def show_info(title, message):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showinfo(title, message, parent=root)
    root.destroy()


def show_error(title, message):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showerror(title, message, parent=root)
    root.destroy()


def find_mmd_window():
    windows = list_windows()
    if not windows:
        raise RuntimeError("MMDのウィンドウが見つかりません。MMDを起動してから実行してください。")
    if len(windows) > 1:
        raise RuntimeError("MMDのウィンドウが複数見つかりました。1つだけ起動した状態で実行してください。")
    return windows[0]


def is_playing(hwnd):
    return bool(playback.get(hwnd)["playing"])


def bring_to_front(hwnd):
    """Make sure MMD is actually the visible top window before a screen capture --
    gdigrab captures raw screen pixels at a fixed region, so if another window
    (browser, etc.) is covering that area, THAT gets recorded instead of MMD."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    win32gui.BringWindowToTop(hwnd)
    time.sleep(0.2)


def wait_for_play_state(hwnd, want_playing, label):
    print(f"[MMDレック] {label} 待機中...")
    while True:
        try:
            if is_playing(hwnd) == want_playing:
                return
        except Exception as exc:  # MMD window may go away, dialog may pop up, etc.
            raise RuntimeError(f"MMDの状態を確認できませんでした: {exc}")
        time.sleep(POLL_INTERVAL)


def get_client_rect_on_screen(hwnd):
    """The whole client area (menus/panels/timeline included), not the title bar."""
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    right, bottom = win32gui.GetClientRect(hwnd)[2], win32gui.GetClientRect(hwnd)[3]
    width, height = right, bottom
    width -= width % 2
    height -= height % 2
    return left, top, width, height


# "Screen only" recording forces MMD's window to this exact client size first,
# so the viewport crop below (calibrated once, by pixel, against this exact size)
# is always correct. A fixed, modest size also keeps it usable on a laptop screen,
# not just a large desktop monitor.
FIXED_CLIENT_WIDTH = 1200
FIXED_CLIENT_HEIGHT = 700

# Pixel-exact margins from the client-area edges to MMD's 3D viewport (the render
# area only, excluding the left frame/timeline panel, the top toolbar strip and the
# bottom status line + operation panels). Calibrated 2026-09-12 by sampling a
# PrintWindow capture of MMD 9.32 x64 pixel-by-pixel at exactly the fixed size
# above and visually confirming the resulting crop. These numbers are only valid
# at that exact client size -- hence forcing the window to it first.
VIEWPORT_MARGIN_LEFT = 259
VIEWPORT_MARGIN_TOP = 25
VIEWPORT_MARGIN_RIGHT = 0
VIEWPORT_MARGIN_BOTTOM = 193


def resize_mmd_window(hwnd, target_client_width, target_client_height):
    """Force MMD's client area to an exact size so the viewport crop lines up."""
    win_left, win_top, win_right, win_bottom = win32gui.GetWindowRect(hwnd)
    _, _, client_r, client_b = win32gui.GetClientRect(hwnd)
    chrome_w = (win_right - win_left) - client_r
    chrome_h = (win_bottom - win_top) - client_b
    new_w = target_client_width + chrome_w
    new_h = target_client_height + chrome_h
    win32gui.SetWindowPos(hwnd, 0, win_left, win_top, new_w, new_h, win32con.SWP_NOZORDER)
    time.sleep(0.2)


# The frame/timeline panel's own vertical scrollbar (control 427) hugs the right
# edge of that panel no matter how wide the user has dragged it -- unlike the
# outer window, MMD remembers a manually-dragged panel width across resizes, so
# forcing the window size alone is not enough (found live 2026-09-12: a run
# recorded mostly left-panel content because that panel had drifted wide).
TIMELINE_SCROLLBAR_ID = 427


def reset_viewport_splitter(hwnd):
    """Drag the left panel's splitter back to the calibrated width, live."""
    scrollbar = win32gui.GetDlgItem(hwnd, TIMELINE_SCROLLBAR_ID)
    if not scrollbar or win32gui.GetClassName(scrollbar) != "ScrollBar":
        return  # unknown layout; leave it to the size-mismatch check downstream
    sb_rect = win32gui.GetWindowRect(scrollbar)
    client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
    current_right_rel = sb_rect[2] - client_left
    if abs(current_right_rel - VIEWPORT_MARGIN_LEFT) <= 3:
        return  # already close enough

    start_x = sb_rect[2] + 3
    start_y = (sb_rect[1] + sb_rect[3]) // 2
    target_x = client_left + VIEWPORT_MARGIN_LEFT

    win32api.SetCursorPos((start_x, start_y))
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    steps = 20
    for i in range(1, steps + 1):
        x = start_x + (target_x - start_x) * i // steps
        win32api.SetCursorPos((x, start_y))
        time.sleep(0.02)
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.3)


def get_viewport_rect_on_screen(hwnd):
    """Just MMD's 3D render view. Call resize_mmd_window() first."""
    client_left, client_top, client_width, client_height = get_client_rect_on_screen(hwnd)
    if (client_width, client_height) != (FIXED_CLIENT_WIDTH, FIXED_CLIENT_HEIGHT):
        raise RuntimeError("MMDのウィンドウサイズが想定と異なります。もう一度実行してください。")
    left = client_left + VIEWPORT_MARGIN_LEFT
    top = client_top + VIEWPORT_MARGIN_TOP
    width = client_width - VIEWPORT_MARGIN_LEFT - VIEWPORT_MARGIN_RIGHT
    height = client_height - VIEWPORT_MARGIN_TOP - VIEWPORT_MARGIN_BOTTOM
    width -= width % 2
    height -= height % 2
    return left, top, width, height


class AudioRecorder:
    def __init__(self, wav_path):
        self.wav_path = wav_path
        self._pa = None
        self._stream = None
        self._wav_file = None

    def start(self):
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        if not default_speakers.get("isLoopbackDevice"):
            for loopback in self._pa.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    default_speakers = loopback
                    break
            else:
                raise RuntimeError("システム音声のループバックデバイスが見つかりませんでした。")

        channels = int(default_speakers["maxInputChannels"])
        rate = int(default_speakers["defaultSampleRate"])

        self._wav_file = wave.open(self.wav_path, "wb")
        self._wav_file.setnchannels(channels)
        self._wav_file.setsampwidth(pyaudio.get_sample_size(pyaudio.paInt16))
        self._wav_file.setframerate(rate)

        def callback(in_data, frame_count, time_info, status):
            self._wav_file.writeframes(in_data)
            return (in_data, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            frames_per_buffer=1024,
            input=True,
            input_device_index=default_speakers["index"],
            stream_callback=callback,
        )
        self._stream.start_stream()

    def stop(self):
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
        if self._wav_file is not None:
            self._wav_file.close()
        if self._pa is not None:
            self._pa.terminate()


# 24Mbps was tuned and confirmed to look great at MMDレック's usual, viewport-crop
# recording size (~940x482) -- but that's a fixed bit budget regardless of frame
# size, so a full-window (not viewport-only) recording on a 4K display ends up
# encoding true 3840x2160 detail at the same 24Mbps and comes out visibly blocky
# (reported live 2026-09-12, worst in the busiest/most detailed opening seconds
# of a 4K test recording). BASE_BITRATE is now a floor, scaled up for frames
# bigger than the reference size instead of a one-size-fits-all constant.
BASE_BITRATE = 24_000_000
REFERENCE_PIXELS = 940 * 482
# Diminishing per-pixel bits as frame size grows -- linear-with-pixel-count
# scaling explodes past ~400Mbps at 4K, which h264_mf can't realistically
# sustain; this exponent keeps a 4K (3840x2160) frame in a sane ~55-60Mbps
# range (in line with typical 4K60 delivery bitrates) while still clearly
# beating the flat 24M that caused the blockiness.
RESOLUTION_SCALE_EXPONENT = 0.3


def bitrate_settings_for(width, height):
    """(bitrate, maxrate, bufsize) in bits/sec, scaled for this frame size."""
    pixel_ratio = (width * height) / REFERENCE_PIXELS
    scale = max(1.0, pixel_ratio ** RESOLUTION_SCALE_EXPONENT)
    bitrate = int(BASE_BITRATE * scale)
    return bitrate, int(bitrate * 1.25), int(bitrate * 1.875)


def start_video_capture(left, top, width, height, out_path, lossless=False):
    """lossless=True writes UtVideo (fully lossless, no generational loss if the
    result is re-encoded later e.g. in AviUtl) to an .avi container -- much
    bigger files, but the right choice for a source that will be edited further.
    lossless=False writes H.264 to .mp4 at a bitrate high enough that
    compression artifacts are not the limiting factor for casual viewing/sharing.
    """
    if lossless:
        codec_args = ["-c:v", "utvideo", "-pix_fmt", "gbrp"]
    else:
        bitrate, maxrate, bufsize = bitrate_settings_for(width, height)
        codec_args = ["-c:v", "h264_mf", "-b:v", str(bitrate), "-maxrate", str(maxrate),
                      "-bufsize", str(bufsize), "-pix_fmt", "yuv420p"]
    cmd = [
        FFMPEG, "-y",
        "-f", "gdigrab",
        "-framerate", str(FPS),
        "-offset_x", str(left), "-offset_y", str(top),
        "-video_size", f"{width}x{height}",
        "-i", "desktop",
        *codec_args,
        out_path,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


def stop_video_capture(proc):
    try:
        proc.communicate(input=b"q", timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def mux_audio(video_path, audio_path, out_path, lossless=False):
    # Lossless video gets lossless (PCM) audio too -- AAC would be the only lossy
    # part left otherwise, and classic AVI containers expect PCM anyway.
    audio_codec = ["-c:a", "pcm_s16le"] if lossless else ["-c:a", "aac"]
    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        *audio_codec,
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)


def get_duration_seconds(path):
    """Total duration of a media file, via ffprobe. None if it can't be read
    (progress just won't show a percentage in that case)."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return float(result.stdout.strip())
    except (ValueError, OSError):
        return None


def get_video_resolution(path):
    """(width, height) of a video's first stream, via ffprobe. None if it can't
    be read (falls back to the unscaled base bitrate in that case)."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        width_str, height_str = result.stdout.strip().split("x")
        return int(width_str), int(height_str)
    except (ValueError, OSError):
        return None


def convert_avi_to_mp4(avi_path, mp4_path, include_audio=True):
    """Re-encode a lossless UtVideo AVI (MMDレック's high quality mode output) to
    a light H.264 MP4, using the same resolution-scaled bitrate as the normal
    MP4 recording mode (see bitrate_settings_for) so quality is consistent
    either way you got the video, at any source resolution.

    Prints a live "変換中: NN%" line to the console (updated in place via \\r) so
    the wait isn't just a blank black window -- ffmpeg's -progress pipe:1 gives
    machine-readable progress lines, from which out_time= is used to compute
    percent-of-total-duration."""
    duration = get_duration_seconds(avi_path)
    resolution = get_video_resolution(avi_path)
    if resolution:
        bitrate, maxrate, bufsize = bitrate_settings_for(*resolution)
    else:
        bitrate, maxrate, bufsize = BASE_BITRATE, int(BASE_BITRATE * 1.25), int(BASE_BITRATE * 1.875)
    audio_args = ["-c:a", "aac"] if include_audio else ["-an"]
    cmd = [
        FFMPEG, "-y",
        "-i", avi_path,
        "-c:v", "h264_mf", "-b:v", str(bitrate), "-maxrate", str(maxrate), "-bufsize", str(bufsize),
        "-pix_fmt", "yuv420p",
        *audio_args,
        "-progress", "pipe:1", "-nostats",
        mp4_path,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    last_shown = -1
    for line in proc.stdout:
        line = line.strip()
        if duration and line.startswith("out_time="):
            h, m, s = line.split("=", 1)[1].split(":")
            try:
                seconds = int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                continue
            percent = max(0, min(100, int(seconds / duration * 100)))
            if percent != last_shown:
                last_shown = percent
                print(f"\r[MMDレック] 変換中: {percent}%", end="", flush=True)
    proc.wait()
    if last_shown >= 0:
        print(f"\r[MMDレック] 変換中: 100%")
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def offer_mp4_export(avi_path):
    """After a lossless AVI is saved, offer to also produce a light MP4 copy
    for sharing (the AVI stays as the AviUtl-editing master)."""
    also_mp4 = ask_yes_no("MMDレック", "MP4版も出力しますか？\n"
                                      "（AviUtl編集用のAVIはそのまま残して、共有用の軽量MP4を別に作成します）")
    if not also_mp4:
        return

    default_mp4 = os.path.splitext(os.path.basename(avi_path))[0] + ".mp4"
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    mp4_path = filedialog.asksaveasfilename(
        parent=root, title="MP4の保存先", initialdir=os.path.dirname(avi_path),
        initialfile=default_mp4, defaultextension=".mp4",
        filetypes=[("MP4ファイル", "*.mp4")],
    )
    root.destroy()
    if not mp4_path:
        return

    include_audio = ask_yes_no("MMDレック", "音声も含めますか？\n"
                                          "「いいえ」を選ぶと映像のみのMP4になります。")

    show_info("MMDレック", "MP4への変換を開始します。\n"
                          "進行状況（%）はコンソール画面に表示されます。\n\n"
                          "※ファイルサイズが大きいと5分以上かかることがあります。")
    try:
        convert_avi_to_mp4(avi_path, mp4_path, include_audio=include_audio)
    except subprocess.CalledProcessError as exc:
        show_error("MMDレック", f"MP4への変換に失敗しました:\n{exc}")
        return
    show_info("MMDレック", f"MP4版が完成しました:\n{mp4_path}")


def convert_mode(avi_path):
    """Standalone AVI->MP4 conversion, invoked by launching the script with a
    file path argument (drag & drop onto the script or its shortcut)."""
    if not os.path.isfile(avi_path):
        show_error("MMDレック", f"ファイルが見つかりません:\n{avi_path}")
        return

    proceed = ask_yes_no("MMDレック", f"このファイルをMP4に変換しますか？\n\n{avi_path}")
    if not proceed:
        return

    default_mp4 = os.path.splitext(os.path.basename(avi_path))[0] + ".mp4"
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    save_path = filedialog.asksaveasfilename(
        parent=root, title="MP4の保存先", initialdir=os.path.dirname(avi_path) or DEFAULT_SAVE_DIR,
        initialfile=default_mp4, defaultextension=".mp4",
        filetypes=[("MP4ファイル", "*.mp4")],
    )
    root.destroy()
    if not save_path:
        return

    include_audio = ask_yes_no("MMDレック", "音声も含めますか？\n"
                                          "「いいえ」を選ぶと映像のみのMP4になります。")

    show_info("MMDレック", "変換を開始します。\n"
                          "進行状況（%）はコンソール画面に表示されます。\n\n"
                          "※ファイルサイズが大きいと5分以上かかることがあります。")
    try:
        convert_avi_to_mp4(avi_path, save_path, include_audio=include_audio)
    except subprocess.CalledProcessError as exc:
        show_error("MMDレック", f"変換に失敗しました:\n{exc}")
        return
    show_info("MMDレック", f"変換が完了しました:\n{save_path}")


def record_mode():
    try:
        mmd = find_mmd_window()
    except RuntimeError as exc:
        show_error("MMDレック", str(exc))
        return

    viewport_only = ask_yes_no("MMDレック", "画面（3Dビュー）だけを録画しますか？\n"
                                          "「いいえ」を選ぶとMMD全体（パネル込み）を録画します。")

    if viewport_only:
        try:
            resize_mmd_window(mmd.hwnd, FIXED_CLIENT_WIDTH, FIXED_CLIENT_HEIGHT)
            reset_viewport_splitter(mmd.hwnd)
        except Exception as exc:
            show_error("MMDレック", f"MMDのウィンドウサイズを調整できませんでした: {exc}")
            return

    lossless = ask_yes_no("MMDレック", "高画質モード（無圧縮/AviUtl編集用）にしますか？\n"
                                      "「いいえ」を選ぶと通常のMP4（軽量）になります。\n\n"
                                      "※高画質モードはファイルサイズがかなり大きくなります。")
    video_ext = ".avi" if lossless else ".mp4"

    include_audio = ask_yes_no("MMDレック", "音声（MMDの再生音）も録音しますか？")
    if include_audio:
        show_info("MMDレック", "音声ありの場合、録画終了後に音声を合成する処理が入るため、\n"
                              "完成するまで少し時間がかかります。")

    show_info("MMDレック", "MMDの再生ボタンを押してください。\n"
                          "押した瞬間に自動で録画が始まります。\n\n"
                          "※録画が始まったら、停止するまで他の操作はしないでください。\n"
                          "　（MMDのウィンドウを動かす/サイズ変更、他ウィンドウを前面に出す、など）")

    tmp_dir = tempfile.mkdtemp(prefix="mmd_recorder_")
    video_only_path = os.path.join(tmp_dir, f"video{video_ext}")
    audio_path = os.path.join(tmp_dir, "audio.wav")

    try:
        wait_for_play_state(mmd.hwnd, True, "再生開始")
        bring_to_front(mmd.hwnd)
        if viewport_only:
            left, top, width, height = get_viewport_rect_on_screen(mmd.hwnd)
        else:
            left, top, width, height = get_client_rect_on_screen(mmd.hwnd)
        print(f"[MMDレック] 録画開始: {width}x{height} @ ({left},{top}) "
              f"({'画面のみ' if viewport_only else '全体'})")

        video_proc = start_video_capture(left, top, width, height, video_only_path, lossless=lossless)
        audio_rec = None
        if include_audio:
            audio_rec = AudioRecorder(audio_path)
            audio_rec.start()

        wait_for_play_state(mmd.hwnd, False, "再生停止")
        print("[MMDレック] 録画停止")

        stop_video_capture(video_proc)
        if audio_rec is not None:
            audio_rec.stop()

    except RuntimeError as exc:
        show_error("MMDレック", f"録画を中断しました: {exc}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    final_path = video_only_path
    if include_audio and os.path.exists(audio_path):
        muxed_path = os.path.join(tmp_dir, f"muxed{video_ext}")
        try:
            mux_audio(video_only_path, audio_path, muxed_path, lossless=lossless)
            final_path = muxed_path
        except subprocess.CalledProcessError:
            show_info("MMDレック", f"音声の合成に失敗したため、映像のみの{video_ext}を使用します。")

    format_label = "AVI" if lossless else "MP4"
    keep = ask_yes_no("MMDレック", f"{format_label}ファイルを出力しますか？")
    if not keep:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    default_name = f"MMDレック_{datetime.now():%Y%m%d_%H%M%S}{video_ext}"
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    os.makedirs(DEFAULT_SAVE_DIR, exist_ok=True)
    save_path = filedialog.asksaveasfilename(
        parent=root, title=f"{format_label}の保存先", initialdir=DEFAULT_SAVE_DIR,
        initialfile=default_name, defaultextension=video_ext,
        filetypes=[(f"{format_label}ファイル", f"*{video_ext}")],
    )
    root.destroy()

    if not save_path:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    shutil.move(final_path, save_path)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    show_info("MMDレック", f"完成しました:\n{save_path}")

    if lossless:
        offer_mp4_export(save_path)


def main():
    args = sys.argv[1:]
    if args:
        convert_mode(args[0])
        return
    record_mode()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # last-resort net so the console shows what happened
        print(f"[MMDレック] エラー: {exc}")
        show_error("MMDレック", f"予期しないエラーが発生しました:\n{exc}")
