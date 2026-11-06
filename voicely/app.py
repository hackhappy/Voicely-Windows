"""Voicely tray app: hotkey dictation into any app, call recording, file transcription."""
import ctypes
import json
import logging
import os
import queue
import re
import struct
import subprocess
import threading
import time
import tkinter as tk
import warnings
from ctypes import wintypes
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from tkinter import filedialog, messagebox

import numpy as np
import win32api
import win32con
import win32gui

from . import engine, store
from .cli import _engine, model_name, transcribe_path

user32, kernel32, gdi32 = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.gdi32
SR = engine.SR
LOGS = engine.HOME / "Logs"
DEFAULTS = {"hotkey": "alt+space", "output": "cursor"}
HOTKEY_PRESETS = [("Alt+Space (default)", "alt+space"), ("Ctrl+Space", "ctrl+space")]
OUTPUTS = {"cursor": "Insert at Cursor", "clipboard": "Copy to Clipboard"}
# Apps that take typed text better than a paste.
TERMINALS = {"windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
             "mintty.exe", "wezterm-gui.exe", "alacritty.exe", "putty.exe", "kitty.exe", "tabby.exe"}
NO_TARGET = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow"}
warnings.filterwarnings("ignore", message=".*discontinuity.*")
log = logging.getLogger("voicely")


# ---------- hotkey ----------

MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
NAMED_VK = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "pause": 0x13, "insert": 0x2D,
            "home": 0x24, "end": 0x23, "prior": 0x21, "next": 0x22, "scroll_lock": 0x91}


def parse_combo(combo):
    mods, vk = 0, None
    for part in combo.lower().split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in NAMED_VK:
            vk = NAMED_VK[part]
        elif re.fullmatch(r"f([1-9]|1\d|2[0-4])", part):
            vk = 0x6F + int(part[1:])
        elif re.fullmatch(r"[a-z0-9]", part):
            vk = ord(part.upper())  # VK codes of letters and digits are their ASCII codes, whatever the layout
        else:
            raise ValueError(f"unknown key '{part}'")
    if vk is None:
        raise ValueError("no key in hotkey")
    return mods, vk


def combo_label(combo):
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in combo.split("+"))


class Hotkey(threading.Thread):
    """RegisterHotKey on a thread with its own message loop; the combo can be swapped at runtime."""
    WM_HOTKEY, WM_REREGISTER = 0x0312, 0x8001

    def __init__(self, combo, on_press, on_error):
        super().__init__(daemon=True)
        self.combo, self.on_press, self.on_error = combo, on_press, on_error
        self.ready = threading.Event()

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create the queue before anyone posts to it
        self.ready.set()
        self._register()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                self.on_press()
            elif msg.message == self.WM_REREGISTER:
                user32.UnregisterHotKey(None, 1)
                self._register()

    def _register(self):
        mods, vk = parse_combo(self.combo)
        if not user32.RegisterHotKey(None, 1, mods | 0x4000, vk):  # 0x4000 = MOD_NOREPEAT
            self.on_error(self.combo)

    def set(self, combo):
        self.combo = combo
        self.ready.wait()
        user32.PostThreadMessageW(self.tid, self.WM_REREGISTER, 0, 0)


# ---------- audio ----------

class Capture(threading.Thread):
    """Records one soundcard device in 50 ms blocks until stopped, into memory or a WAV file."""

    def __init__(self, device, wav=None, on_level=None):
        super().__init__(daemon=True)
        self.device, self.wav, self.on_level = device, wav, on_level
        self.stop_flag, self.opened = threading.Event(), threading.Event()
        self.blocks, self.error = [], None

    def run(self):
        import soundfile as sf

        try:
            sink = sf.SoundFile(self.wav, "w", SR, 1, "PCM_16") if self.wav else None
            try:
                with self.device.recorder(samplerate=SR, channels=1, blocksize=SR // 20) as rec:
                    self.opened.set()
                    while not self.stop_flag.is_set():
                        block = rec.record(numframes=SR // 20)[:, 0].astype(np.float32)
                        if sink:
                            sink.write(block)
                        else:
                            self.blocks.append(block)
                        if self.on_level:
                            self.on_level(float(np.sqrt(np.mean(block ** 2))))
            finally:
                if sink:
                    sink.close()
        except Exception as e:
            log.exception("capture failed")
            self.error = e
        self.opened.set()

    def stop(self):
        self.stop_flag.set()
        self.join()
        return np.concatenate(self.blocks) if self.blocks else np.zeros(0, np.float32)


def read_pcm16(path):
    """Our own 16-bit mono WAVs; tolerates a header left unfinished by a crash."""
    raw = Path(path).read_bytes() if Path(path).exists() else b""
    i = raw.find(b"data")
    if i < 0:
        return np.zeros(0, np.float32)
    data = raw[i + 8:]
    return np.frombuffer(data[:len(data) // 2 * 2], np.int16).astype(np.float32) / 32768


class CallRecording:
    """Microphone + system audio (WASAPI loopback) into calls\\<stamp>\\mic.wav and system.wav."""

    def __init__(self):
        import soundcard as sc

        self.folder = store.BASE / "calls" / store.stamp()
        self.folder.mkdir(parents=True)
        self.started = time.time()
        speaker = sc.default_speaker()
        self.mic = Capture(sc.default_microphone(), self.folder / "mic.wav")
        self.system = Capture(sc.get_microphone(id=str(speaker.name), include_loopback=True), self.folder / "system.wav")
        # Loopback delivers nothing while the output is silent, which would desync the two tracks.
        self.silence = threading.Thread(target=self._play_silence, args=(speaker,), daemon=True)
        for t in (self.silence, self.mic, self.system):
            t.start()

    def _play_silence(self, speaker):
        try:
            with speaker.player(samplerate=SR, channels=1) as player:
                while not self.mic.stop_flag.is_set():
                    player.play(np.zeros(SR // 10, np.float32))
        except Exception:
            log.exception("silence player failed")

    def stop(self):
        self.mic.stop()
        self.system.stop()
        self.silence.join(timeout=2)
        return self.folder


def identify_you(turns, mic, system):
    """The diarized voice that is mostly heard on the microphone while system audio is quiet."""
    hop = SR // 10
    n = min(len(mic), len(system)) // hop
    if not n or not turns:
        return None
    m = np.sqrt((mic[:n * hop].reshape(n, hop) ** 2).mean(1))
    s = np.sqrt((system[:n * hop].reshape(n, hop) ** 2).mean(1))
    mic_only = (m > 0.005) & (m > 2 * s)
    best, best_score = None, 0.3
    for spk in {k for *_, k in turns}:
        frames = [i for a, b, k in turns if k == spk for i in range(int(a * 10), min(n, int(b * 10)))]
        score = mic_only[frames].mean() if frames else 0
        if score > best_score:
            best, best_score = spk, score
    return best


def process_call(folder):
    """mic.wav + system.wav -> mix.wav, transcript.md, transcript.jsonl."""
    import soundfile as sf

    mic, system = read_pcm16(folder / "mic.wav"), read_pcm16(folder / "system.wav")
    n = max(len(mic), len(system))
    mic, system = np.pad(mic, (0, n - len(mic))), np.pad(system, (0, n - len(system)))

    def level(x):  # bring both sides to a similar loudness; leave a silent side alone
        peak = np.abs(x).max() if len(x) else 0
        return x * (0.5 / peak) if peak > 1e-3 else x

    mix = np.clip(level(mic) + level(system), -1, 1).astype(np.float32)
    sf.write(folder / "mix.wav", mix, SR, subtype="PCM_16")
    segments = _engine.transcribe(mix)
    if segments:
        turns = _engine.diarize(mix)
        engine.label_speakers(segments, turns, identify_you(turns, mic, system))
    started = datetime.strptime(folder.name[:19], "%Y-%m-%d_%H-%M-%S")
    return store.save_call(folder, segments, started)


# ---------- delivery ----------

def foreground():
    """(hwnd, process image name, window class) of the foreground window."""
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            name = Path(buf.value).name
        kernel32.CloseHandle(handle)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    return hwnd, name, cls.value


def _open_clipboard():
    import win32clipboard as wc

    for _ in range(40):
        try:
            wc.OpenClipboard()
            return wc
        except Exception:
            time.sleep(0.025)
    raise RuntimeError("clipboard is busy")


def clipboard_save():
    wc = _open_clipboard()
    try:
        saved, fmt = [], 0
        while fmt := wc.EnumClipboardFormats(fmt):
            try:
                saved.append((fmt, wc.GetGlobalMemory(wc.GetClipboardDataHandle(fmt))))
            except Exception:
                pass  # GDI handle formats (bitmap, metafile); Windows re-synthesizes them from CF_DIB
        return saved
    finally:
        wc.CloseClipboard()


def clipboard_set(text, transient=False):
    wc = _open_clipboard()
    try:
        wc.EmptyClipboard()
        wc.SetClipboardData(wc.CF_UNICODETEXT, text)
        if transient:  # keep the transcript out of Win+V history, cloud clipboard and clipboard managers
            wc.SetClipboardData(wc.RegisterClipboardFormat("ExcludeClipboardContentFromMonitorProcessing"), b"\0")
            wc.SetClipboardData(wc.RegisterClipboardFormat("CanIncludeInClipboardHistory"), struct.pack("<I", 0))
            wc.SetClipboardData(wc.RegisterClipboardFormat("CanUploadToCloudClipboard"), struct.pack("<I", 0))
    finally:
        wc.CloseClipboard()
    return user32.GetClipboardSequenceNumber()


def clipboard_restore(saved):
    wc = _open_clipboard()
    try:
        wc.EmptyClipboard()
        for fmt, data in saved:
            try:
                wc.SetClipboardData(fmt, data)
            except Exception:
                pass
    finally:
        wc.CloseClipboard()


def deliver(text, output):
    """Put text where the caret is. Returns the route taken: typed | pasted | clipboard."""
    import keyboard

    hwnd, exe, cls = foreground()
    if output == "clipboard" or not hwnd or cls in NO_TARGET:
        clipboard_set(text)
        return "clipboard"
    deadline = time.time() + 1.5  # the user may still be holding the hotkey's modifier
    while time.time() < deadline and any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
        time.sleep(0.02)
    if exe.lower() in TERMINALS:
        keyboard.write(text)
        return "typed"
    saved = clipboard_save()
    seq = clipboard_set(text, transient=True)
    for vk, flags in ((0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2)):  # Ctrl+V by virtual key: works on any layout
        user32.keybd_event(vk, 0, flags, 0)
    time.sleep(0.5)  # shortcut: fixed wait for the app to read the clipboard; Windows gives no "pasted" signal
    if user32.GetClipboardSequenceNumber() == seq:  # don't clobber something the user copied meanwhile
        clipboard_restore(saved)
    return "pasted"


def journal(app, route, chars):
    with open(LOGS / "insertion.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": store.iso(), "app": app, "route": route, "chars": chars}) + "\n")


# ---------- overlay ----------

def _over(dst, colour, alpha):
    """Composite a colour with per-pixel alpha onto premultiplied RGBA, in place."""
    a = alpha[..., None]
    dst[..., :3] = dst[..., :3] * (1 - a) + np.asarray(colour, np.float32) * a
    dst[..., 3] = dst[..., 3] * (1 - alpha) + alpha


class PillArt:
    """The pill as pixels: a light glass capsule with water inside, antialiased through distance fields."""
    # surface colour, depth colour, opacity, share of the water level, drift speed, wave number, phase
    LAYERS = (
        ((0.72, 0.92, 1.00), (0.58, 0.86, 0.99), 0.50, 1.00, 1.1, 0.050, 0.0),
        ((0.50, 0.83, 0.99), (0.36, 0.74, 0.97), 0.60, 0.80, -1.6, 0.075, 1.7),
        ((0.38, 0.76, 0.98), (0.20, 0.62, 0.94), 0.75, 0.60, 2.3, 0.100, 3.1),
    )
    GLASS, WHITE, RIM, INK = (0.97, 0.985, 1.0), (1.0, 1.0, 1.0), (0.72, 0.80, 0.88), (0.07, 0.19, 0.30)
    GLASS_OPACITY = 0.78

    def __init__(self, scale=1.0):
        self.s = s = scale
        self.m, self.h = int(14 * s), int(32 * s)
        self.fw, self.fh = int(172 * s) + 2 * self.m, self.h + 2 * self.m
        self.x = np.arange(self.fw, dtype=np.float32)
        self.y = np.arange(self.fh, dtype=np.float32)[:, None]
        radius, self.mid = self.h / 2, self.fh / 2
        spine = np.clip(self.x, self.m + radius, self.fw - self.m - radius)
        edge = np.hypot(self.x - spine, self.y - self.mid) - radius
        self.inside = np.clip(0.5 - edge, 0, 1)
        shadow = 0.16 * np.exp(-(np.maximum(np.hypot(self.x - spine, self.y - self.mid - 3 * s) - radius, 0) / (6 * s)) ** 2)
        self.base = np.zeros((self.fh, self.fw, 4), np.float32)
        _over(self.base, self.INK, shadow)
        _over(self.base, self.GLASS, self.inside * self.GLASS_OPACITY)
        self.sheen = self.inside * np.clip(1 - (self.y - self.m) / (self.h * 0.5), 0, 1) ** 2 * 0.25
        self.rim = np.clip(1.2 - np.abs(edge + 0.7), 0, 1) * 0.55
        self.box = np.s_[self.m:-self.m, self.m:-self.m]  # the capsule: all per-frame work stays in here
        self.bx, self.by, self.mask = self.x[self.m:-self.m], self.y[self.m:-self.m], self.inside[self.box]
        fall = np.clip((self.by - self.m - 0.3 * self.h) / (0.7 * self.h), 0, 1)[..., None]
        self.tints = [np.asarray(top, np.float32) * (1 - fall) + np.asarray(deep, np.float32) * fall
                      for top, deep, *_ in self.LAYERS]
        self.label("")

    def label(self, text):
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype("seguisb.ttf", int(12 * self.s))
        start = (self.fw - font.getlength(text) + 14 * self.s) / 2  # dot + text centred as one group
        canvas = Image.new("L", (self.fw, self.fh))
        ImageDraw.Draw(canvas).text((start, self.mid), text, fill=255, font=font, anchor="lm")
        glass = np.zeros((self.fh, self.fw, 4), np.float32)
        _over(glass, self.WHITE, self.sheen)
        _over(glass, self.RIM, self.rim)
        _over(glass, self.INK, np.asarray(canvas, np.float32) / 255)
        self.glass = glass[self.box]
        self.dot_x = int(start - 11 * self.s)

    def frame(self, t, level, dot):
        """Premultiplied BGRA for UpdateLayeredWindow. level 0..1 is the voice; dot is an (r, g, b) status colour."""
        out = self.base.copy()
        water = out[self.box]
        bottom = self.m + self.h
        for tint, (*_, opacity, share, speed, k, phase) in zip(self.tints, self.LAYERS):
            x = self.bx / self.s * k
            wave = 0.6 * np.sin(x + t * speed + phase) + 0.4 * np.sin(1.9 * x - 1.4 * t * speed + 2 * phase)
            depth = self.by - (bottom - (0.30 + 0.38 * level) * self.h * share - (0.03 + 0.17 * level) * self.h * wave)
            _over(water, tint, np.clip(depth + 0.5, 0, 1) * self.mask * opacity)
        _over(water, self.WHITE, np.clip(1 - np.abs(depth) / (1.2 * self.s), 0, 1) * self.mask * 0.5)  # glint on the front wave
        water[:] = self.glass + water * (1 - self.glass[..., 3:])
        r, cy = int(6 * self.s), int(self.mid)
        spot = np.s_[cy - r:cy + r, self.dot_x - r:self.dot_x + r]
        radius = (3.3 + 0.5 * np.sin(t * 5)) * self.s
        _over(out[spot], dot, np.clip(radius - np.hypot(self.x[spot[1]] - self.dot_x, self.y[spot[0]] - self.mid) + 0.5, 0, 1))
        return (out[..., [2, 1, 0, 3]] * 255).astype(np.uint8, order="C")  # raw memory for SetBitmapBits


class Overlay:
    """Floating pill on the monitor under the cursor. Never takes focus or clicks; draws only while shown."""
    FRAME_MS = 33

    def __init__(self, root):
        self.art = PillArt(root.winfo_fpixels("1i") / 96)
        self.size = self.art.fw, self.art.fh
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry("{}x{}".format(*self.size))
        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id())
        # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_LAYERED
        user32.SetWindowLongW(self.hwnd, -20, user32.GetWindowLongW(self.hwnd, -20) | 0x08000000 | 0x80 | 0x20 | 0x80000)
        self.win.deiconify()
        win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
        gdi32.CreateBitmap.restype = ctypes.c_void_p
        gdi32.SetBitmapBits.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
        self.bitmap = gdi32.CreateBitmap(*self.size, 1, 32, None)
        self.dc = win32gui.CreateCompatibleDC(0)
        win32gui.SelectObject(self.dc, self.bitmap)
        self.drawing, self.hide_job = False, None
        self.t = self.voice = self.target = self.fade = self.fade_to = 0.0

    def _place(self):
        """Bottom centre of the monitor under the cursor, re-raised every time:
        after days of uptime the window kept its topmost bit yet sat behind ordinary windows."""
        left, _, right, bottom = win32api.GetMonitorInfo(win32api.MonitorFromPoint(win32api.GetCursorPos(), 2))["Work"]
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, (left + right - self.size[0]) // 2,
                              bottom - self.size[1] - int(24 * self.art.s), 0, 0,
                              win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)

    def show(self, text, color="#ff453a", live=True, hide_after=None):
        if self.hide_job:
            self.win.after_cancel(self.hide_job)
            self.hide_job = None
        self.art.label(text)
        self.dot = tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        self.live, self.fade_to, self.target = live, 1.0, 0.0
        self._place()
        if not self.drawing:
            self.drawing, self.clock = True, time.monotonic()
            self._tick()
        if hide_after:
            self.hide_job = self.win.after(hide_after, self.hide)

    def hide(self):
        self.hide_job = None
        self.fade_to = 0.0

    def level(self, value):
        self.target = min(1.0, (value * 14) ** 0.6)

    def _tick(self):
        now = time.monotonic()
        self.t += (now - self.clock) * (1.0 if self.live else 0.45)  # water calms down once you stop talking
        self.clock = now
        self.voice += (self.target - self.voice) * (0.35 if self.target > self.voice else 0.10)
        self.fade += (self.fade_to - self.fade) * 0.3
        if not self.fade_to and self.fade < 0.03:
            self.fade, self.drawing = 0.0, False
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
            return
        frame = self.art.frame(self.t, self.voice, self.dot)
        gdi32.SetBitmapBits(self.bitmap, frame.nbytes, frame.ctypes.data)
        win32gui.UpdateLayeredWindow(self.hwnd, None, None, self.size, self.dc, (0, 0), 0, (0, 0, int(self.fade * 255), 1), 2)
        self.win.after(self.FRAME_MS, self._tick)


# ---------- the app ----------

class App:
    def __init__(self):
        self.cfg = {**DEFAULTS, **engine.read_config(), "model": engine.selected_model()}
        self.calls = queue.Queue()
        self.files = queue.Queue()
        self.mic = None  # Capture while dictating
        self.transcribing = False
        self.call = None
        self.download_status = None  # what the Model menu shows while a download runs
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = Overlay(self.root)

    # thread hops: everything Tk runs on the main thread
    def ui(self, fn, *args):
        self.calls.put((fn, args))

    def _pump(self):
        while not self.calls.empty():
            fn, args = self.calls.get()
            try:
                fn(*args)
            except Exception:
                log.exception("ui call failed")
        self.root.after(30, self._pump)

    def save_config(self):
        engine.CONFIG.write_text(json.dumps(self.cfg, indent=2), "utf-8")
        self.icon.update_menu()

    def notify(self, message, title="Voicely"):
        try:
            self.icon.notify(message, title)
        except Exception:
            log.exception("notify failed")

    # dictation
    def toggle_dictation(self):
        if self.mic:
            return self.stop_dictation()
        if self.transcribing:
            return
        if not engine.models_ready():
            return self.pick_model(self.cfg["model"])
        import soundcard as sc

        cap = Capture(sc.default_microphone(), on_level=lambda v: self.ui(self.overlay.level, v))
        cap.start()
        cap.opened.wait(3)
        if cap.error or not cap.is_alive():
            self.overlay.show("Microphone unavailable", "#ff9f0a", live=False, hide_after=3000)
            self.notify("Could not open the microphone. Check Settings › Privacy › Microphone.")
            return
        self.mic = cap
        self.overlay.show("Listening")
        self.refresh()
        threading.Thread(target=_engine.load, daemon=True).start()  # the model loads while you speak

    def stop_dictation(self):
        cap, self.mic = self.mic, None
        self.transcribing = True
        self.overlay.show("Transcribing…", "#f2a900", live=False)
        self.refresh()
        threading.Thread(target=self._finish_dictation, args=(cap,), daemon=True).start()

    def _finish_dictation(self, cap):
        audio = cap.stop()
        try:
            text = " ".join(s["text"] for s in _engine.transcribe(audio)).strip()
        except Exception:
            log.exception("dictation transcription failed")
            kept = self._keep_audio(audio)
            self.ui(self.overlay.show, "Transcription failed", "#ff9f0a", False, 3000)
            self.notify(f"Transcription failed. Audio kept at {kept}")
            text = None
        try:
            if text:
                _, exe, _ = foreground()
                store.save_dictation(text, Path(exe).stem or "unknown")
                route = deliver(text, self.cfg["output"])
                journal(exe, route, len(text))
                if route == "clipboard":
                    self.ui(self.overlay.show, "Copied — press Ctrl+V", "#30d158", False, 2500)
                else:
                    self.ui(self.overlay.hide)
            elif text is not None:
                self.ui(self.overlay.show, "No speech detected", "#8e8e93", False, 1500)
        except Exception:
            log.exception("delivery failed")
            self.ui(self.overlay.show, "Saved, not inserted", "#ff9f0a", False, 3000)
        finally:
            self.transcribing = False
            self.refresh()

    def _keep_audio(self, audio):
        import soundfile as sf

        folder = engine.HOME / "failed-dictations"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{store.stamp()}.wav"
        sf.write(path, audio, SR, subtype="PCM_16")
        return path

    # calls
    def toggle_call(self):
        if self.call:
            folder, self.call = self.call.stop(), None
            self.refresh()
            self.notify("Call recorded. Transcribing…")
            threading.Thread(target=self._finish_call, args=(folder,), daemon=True).start()
            return
        if not engine.models_ready():
            return self.pick_model(self.cfg["model"])
        try:
            self.call = CallRecording()
        except Exception as e:
            log.exception("call recording failed to start")
            self.notify(f"Could not start call recording: {e}")
            return
        self.refresh()
        self.notify("Recording call: microphone + system audio.")

    def _finish_call(self, folder):
        try:
            path = process_call(folder)
            self.notify(f"Call transcript saved: {path.parent.name}")
            subprocess.Popen(["explorer", "/select,", str(path)])
        except Exception as e:
            log.exception("call processing failed")
            self.notify(f"Call transcription failed ({e}). Audio is kept in {folder}")

    def recover_calls(self):
        """Transcribe calls whose audio was saved but never processed (crash, quit mid-call)."""
        for folder in sorted((store.BASE / "calls").glob("*")):
            if folder.is_dir() and not (folder / "transcript.md").exists() and (folder / "mic.wav").exists():
                log.info("recovering call %s", folder.name)
                self._finish_call(folder)

    # files
    def transcribe_files(self):
        self.root.attributes("-topmost", True)
        paths = filedialog.askopenfilenames(parent=self.root, title="Transcribe File", filetypes=[
            ("Audio / video", "*.mp3 *.m4a *.wav *.mp4 *.mov *.mkv *.webm *.ogg *.opus *.flac *.aac *.wma *.avi"),
            ("All files", "*.*")])
        if paths:
            diarize = messagebox.askyesno("Voicely", "Identify speakers?", parent=self.root)
            for p in paths:
                self.files.put((p, diarize))
        self.root.attributes("-topmost", False)

    def _file_worker(self):
        while True:
            path, diarize = self.files.get()
            try:
                self.notify(f"Transcribing {Path(path).name}…")
                saved = store.save_file_transcript(path, transcribe_path(path, diarize), model_name())
                self.notify(f"Transcript saved: {saved.parent.name}")
                subprocess.Popen(["explorer", "/select,", str(saved)])
            except Exception as e:
                log.exception("file transcription failed")
                self.notify(f"Could not transcribe {Path(path).name}: {e}")

    # models
    def ask(self, question):
        self.root.attributes("-topmost", True)
        answer = messagebox.askyesno("Voicely", question, parent=self.root)
        self.root.attributes("-topmost", False)
        return answer

    def pick_model(self, model_id):
        """A downloaded model is a switch; only a download needs a yes."""
        if engine.models_ready(model_id):
            return self.set_model(model_id)
        if self.download_status:
            return self.notify("Another model is still downloading…")
        m = engine.CATALOG[model_id]
        if self.ask(f"Download {m['name']} ({m['size']})? Everything then runs offline."):
            self.download_status = f"Downloading {m['name']}…"
            self.refresh()
            threading.Thread(target=self._download, args=(model_id,), daemon=True).start()

    def set_model(self, model_id):
        self.cfg["model"] = model_id
        self.save_config()

    def _download(self, model_id):
        name = engine.CATALOG[model_id]["name"]

        def progress(text):
            self.download_status = text
            self.refresh()

        try:
            self.notify(f"Downloading {name}…")
            engine.ensure_models(model_id, progress)
            self.ui(self.set_model, model_id)
            self.notify(f"{name} is ready. Press {combo_label(self.cfg['hotkey'])} to dictate.")
        except Exception as e:
            log.exception("model download failed")
            self.notify(f"Could not download {name}: {e}")
        self.download_status = None
        self.refresh()

    def delete_model(self, model_id):
        m = engine.CATALOG[model_id]
        if self.ask(f"Delete {m['name']}? This frees {m['size']} of disk space. You can download it again from the Model menu."):
            _engine.delete(model_id)
            self.refresh()

    # settings
    def set_hotkey(self, combo):
        self.cfg["hotkey"] = combo
        self.hotkey.set(combo)
        self.save_config()

    def hotkey_failed(self, combo):
        self.notify(f"{combo_label(combo)} is taken by another app. Pick another hotkey in the Voicely menu.")

    def record_hotkey(self):
        win = tk.Toplevel(self.root)
        win.title("Voicely hotkey")
        win.attributes("-topmost", True)
        tk.Label(win, text="Press the new shortcut with Ctrl, Alt or Shift\n(Esc to cancel)", padx=30, pady=20,
                 font=("Segoe UI", 10)).pack()

        def on_key(e):
            if e.keysym == "Escape":
                return win.destroy()
            if e.keysym.split("_")[0] in ("Shift", "Control", "Alt", "Win", "Caps"):
                return
            mods = [m for m, bit in (("ctrl", 0x4), ("alt", 0x20000), ("shift", 0x1)) if e.state & bit]
            combo = "+".join(mods + [e.keysym.lower()])
            try:
                parse_combo(combo)
            except ValueError:
                return
            if mods or re.fullmatch(r"f\d+", e.keysym.lower()):
                win.destroy()
                self.set_hotkey(combo)

        win.bind("<KeyPress>", on_key)
        win.focus_force()

    # tray
    def refresh(self):
        if hasattr(self, "icon"):
            self.icon.icon = self.icons[bool(self.mic or self.call)]
            self.icon.update_menu()

    def build_icon(self):
        import pystray
        from PIL import Image, ImageDraw

        base = Image.open(Path(__file__).with_name("icon.png")).convert("RGBA").resize((64, 64), Image.LANCZOS)
        live = base.copy()
        ImageDraw.Draw(live).ellipse((38, 38, 62, 62), fill="#ff3b30", outline="white", width=3)
        self.icons = {False: base, True: live}
        M, Menu = pystray.MenuItem, pystray.Menu

        def call_text(_):
            if not self.call:
                return "Record Call"
            sec = int(time.time() - self.call.started)
            return f"Stop Call Recording ({sec // 60:02d}:{sec % 60:02d})"

        def hotkey_item(name, combo):
            return M(name, lambda: self.ui(self.set_hotkey, combo), checked=lambda _: self.cfg["hotkey"] == combo, radio=True)

        def output_item(key):
            def pick():
                self.cfg["output"] = key
                self.save_config()
            return M(OUTPUTS[key], pick, checked=lambda _: self.cfg["output"] == key, radio=True)

        def model_item(model_id):
            m = engine.CATALOG[model_id]
            return M(lambda _: f"{m['name']} · {m['size']} · {m['hint']}" + ("  [downloaded]" if engine.model_ready(model_id) else ""),
                     lambda: self.ui(self.pick_model, model_id), checked=lambda _: self.cfg["model"] == model_id, radio=True)

        def delete_item(model_id):
            m = engine.CATALOG[model_id]
            return M(f"{m['name']} · {m['size']}", lambda: self.ui(self.delete_model, model_id),
                     visible=lambda _: engine.model_ready(model_id) and self.cfg["model"] != model_id)

        menu = Menu(
            M(lambda _: f"{'Stop Dictation' if self.mic else 'Dictate'}  ({combo_label(self.cfg['hotkey'])})",
              lambda: self.ui(self.toggle_dictation)),
            M(call_text, lambda: self.ui(self.toggle_call)),
            M("Transcribe File...", lambda: self.ui(self.transcribe_files)),
            Menu.SEPARATOR,
            M(lambda _: f"Hotkey: {combo_label(self.cfg['hotkey'])}", Menu(
                *(hotkey_item(n, c) for n, c in HOTKEY_PRESETS), Menu.SEPARATOR,
                M("Record Custom...", lambda: self.ui(self.record_hotkey)))),
            M(lambda _: self.download_status or f"Model: {engine.CATALOG[self.cfg['model']]['name']}", Menu(
                *(model_item(i) for i in engine.CATALOG), Menu.SEPARATOR,
                M("Delete Downloaded Model", Menu(*(delete_item(i) for i in engine.CATALOG))))),
            M(lambda _: f"Output: {OUTPUTS[self.cfg['output']]}", Menu(*(output_item(k) for k in OUTPUTS))),
            Menu.SEPARATOR,
            M("Open Transcripts", lambda: (store.BASE.mkdir(parents=True, exist_ok=True), os.startfile(store.BASE))),
            M("Microphone Permissions...", lambda: os.startfile("ms-settings:privacy-microphone")),
            Menu.SEPARATOR,
            M("About Voicely", lambda: self.ui(self.about)),
            Menu.SEPARATOR,
            M("Quit Voicely", lambda: self.ui(self.quit)),
        )
        self.icon = pystray.Icon("Voicely", base, "Voicely", menu)

    def about(self):
        messagebox.showinfo("About Voicely", f"Voicely for Windows {version('voicely')}\n\n"
                            "Offline dictation, call and file transcription.\n"
                            "Windows port of github.com/hackhappy/Voicely (MIT).\n\n"
                            f"Speech: {model_name()}\n"
                            "Parakeet V3: NVIDIA (CC-BY-4.0), Parakeet Ultra: moondream (CC-BY-4.0),\n"
                            "GigaAM: SberDevices (MIT), Omnilingual: Meta (Apache-2.0), Whisper: OpenAI (MIT)\n"
                            "Diarization: pyannote segmentation 3.0 + WeSpeaker\n"
                            "Runtime: sherpa-onnx, fully on-device.", parent=self.root)

    def quit(self):
        if self.call:  # keep the audio; the next launch transcribes it
            self.call.stop()
        self.icon.stop()
        self.root.destroy()

    def run(self):
        LOGS.mkdir(parents=True, exist_ok=True)
        self.build_icon()
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.hotkey = Hotkey(self.cfg["hotkey"], lambda: self.ui(self.toggle_dictation), self.hotkey_failed)
        self.hotkey.start()
        threading.Thread(target=self._file_worker, daemon=True).start()
        if engine.models_ready():
            threading.Thread(target=self.recover_calls, daemon=True).start()
        else:
            self.root.after(500, self.pick_model, self.cfg["model"])
        self.root.after(30, self._pump)
        self.root.mainloop()


def main():
    engine.HOME.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOGS / "voicely.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    threading.excepthook = lambda a: log.error("thread crashed", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    kernel32.CreateMutexW(None, False, "Local\\VoicelyTraySingleton")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        messagebox.showinfo("Voicely", "Voicely is already running — look for its icon in the system tray.")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    log.info("Voicely %s starting", version("voicely"))
    App().run()
