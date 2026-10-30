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
from collections import deque
from ctypes import wintypes
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from tkinter import filedialog, messagebox

import numpy as np

from . import engine, store
from .cli import _engine, transcribe_path

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
SR = engine.SR
CONFIG = engine.HOME / "config.json"
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

class Overlay:
    """Floating pill at the bottom of the screen: live waveform + status. Never takes focus or clicks."""
    KEY, BG, BARS = "#010203", "#1d1d21", 18

    def __init__(self, root):
        s = root.winfo_fpixels("1i") / 96
        self.s, self.W, self.H = s, int(300 * s), int(40 * s)
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True, "-transparentcolor", self.KEY, "-alpha", 0.0)
        x = (root.winfo_screenwidth() - self.W) // 2
        y = root.winfo_screenheight() - self.H - int(90 * s)
        self.win.geometry(f"{self.W}x{self.H}+{x}+{y}")
        c = self.canvas = tk.Canvas(self.win, width=self.W, height=self.H, bg=self.KEY, highlightthickness=0)
        c.pack()
        h, w = self.H, self.W
        for shape in (c.create_oval(0, 0, h, h), c.create_oval(w - h, 0, w, h), c.create_rectangle(h / 2, 0, w - h / 2, h)):
            c.itemconfig(shape, fill=self.BG, outline=self.BG)
        self.dot = c.create_oval(h * 0.4, h * 0.4, h * 0.6, h * 0.6, fill="#ff453a", outline="")
        self.bar_x = [h * 0.85 + i * 5 * s for i in range(self.BARS)]
        self.bars = [c.create_rectangle(x, h / 2 - 1, x + 3 * s, h / 2 + 1, fill="#e5e5ea", outline="") for x in self.bar_x]
        self.text = c.create_text(self.bar_x[-1] + 14 * s, h / 2, anchor="w", fill="white", font=("Segoe UI", 10))
        self.levels = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.hide_job = None
        self.win.update_idletasks()
        hwnd = user32.GetParent(self.win.winfo_id())
        # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_LAYERED
        user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x08000000 | 0x80 | 0x20 | 0x80000)
        self.win.deiconify()

    def show(self, text, color="#ff453a", bars=True, hide_after=None):
        if self.hide_job:
            self.win.after_cancel(self.hide_job)
            self.hide_job = None
        self.canvas.itemconfig(self.text, text=text)
        self.canvas.coords(self.text, (self.bar_x[-1] + 14 * self.s) if bars else self.H * 0.85, self.H / 2)
        self.canvas.itemconfig(self.dot, fill=color)
        for bar in self.bars:
            self.canvas.itemconfig(bar, state="normal" if bars else "hidden")
        if bars:
            self.levels.extend([0.0] * self.BARS)
            self._draw()
        self.win.attributes("-alpha", 0.94)
        if hide_after:
            self.hide_job = self.win.after(hide_after, self.hide)

    def hide(self):
        self.hide_job = None
        self.win.attributes("-alpha", 0.0)

    def level(self, value):
        self.levels.append(min(1.0, (value * 14) ** 0.6))
        self._draw()

    def _draw(self):
        h, s = self.H, self.s
        for bar, x, v in zip(self.bars, self.bar_x, self.levels):
            half = max(1.0, v * h * 0.32)
            self.canvas.coords(bar, x, h / 2 - half, x + 3 * s, h / 2 + half)


# ---------- the app ----------

class App:
    def __init__(self):
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.loads(CONFIG.read_text("utf-8")))
        except (OSError, ValueError):
            pass
        self.calls = queue.Queue()
        self.files = queue.Queue()
        self.mic = None  # Capture while dictating
        self.transcribing = False
        self.call = None
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
        CONFIG.write_text(json.dumps(self.cfg, indent=2), "utf-8")
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
            return self.offer_download()
        import soundcard as sc

        cap = Capture(sc.default_microphone(), on_level=lambda v: self.ui(self.overlay.level, v))
        cap.start()
        cap.opened.wait(3)
        if cap.error or not cap.is_alive():
            self.overlay.show("Microphone unavailable", "#ff9f0a", bars=False, hide_after=3000)
            self.notify("Could not open the microphone. Check Settings › Privacy › Microphone.")
            return
        self.mic = cap
        self.overlay.show("Listening")
        self.refresh()

    def stop_dictation(self):
        cap, self.mic = self.mic, None
        self.transcribing = True
        self.overlay.show("Transcribing…", "#ffd60a", bars=False)
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
            self.ui(self.overlay.show, "Saved, but could not insert", "#ff9f0a", False, 3000)
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
            return self.offer_download()
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
                saved = store.save_file_transcript(path, transcribe_path(path, diarize))
                self.notify(f"Transcript saved: {saved.parent.name}")
                subprocess.Popen(["explorer", "/select,", str(saved)])
            except Exception as e:
                log.exception("file transcription failed")
                self.notify(f"Could not transcribe {Path(path).name}: {e}")

    # models
    def offer_download(self):
        if getattr(self, "downloading", False):
            return self.notify("The speech model is still downloading…")
        if messagebox.askyesno("Voicely", "Download the speech model Parakeet V3 and the diarization models "
                               "(about 520 MB)? Everything then runs offline.", parent=self.root):
            self.downloading = True
            threading.Thread(target=self._download, daemon=True).start()

    def _download(self):
        try:
            self.notify("Downloading the speech model…")
            _engine.load()
            self.notify(f"Voicely is ready. Press {combo_label(self.cfg['hotkey'])} to dictate.")
        except Exception as e:
            log.exception("model download failed")
            self.notify(f"Model download failed: {e}")
        self.downloading = False

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

        menu = Menu(
            M(lambda _: f"{'Stop Dictation' if self.mic else 'Dictate'}  ({combo_label(self.cfg['hotkey'])})",
              lambda: self.ui(self.toggle_dictation)),
            M(call_text, lambda: self.ui(self.toggle_call)),
            M("Transcribe File...", lambda: self.ui(self.transcribe_files)),
            Menu.SEPARATOR,
            M(lambda _: f"Hotkey: {combo_label(self.cfg['hotkey'])}", Menu(
                *(hotkey_item(n, c) for n, c in HOTKEY_PRESETS), Menu.SEPARATOR,
                M("Record Custom...", lambda: self.ui(self.record_hotkey)))),
            M(f"Model: {engine.MODEL_NAME}", None, enabled=False),
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
                            "Speech: NVIDIA Parakeet TDT 0.6B v3 (CC-BY-4.0)\n"
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
            def warm_up():
                _engine.load()
                self.recover_calls()
            threading.Thread(target=warm_up, daemon=True).start()
        else:
            self.root.after(500, self.offer_download)
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
