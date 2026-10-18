"""Global push-to-talk dictation for Windows.

Press the hotkey to start recording, press it again to stop. The transcript is
transcribed by a local faster-whisper model and pasted into whatever text field
was focused when recording started.

    python dictate.py            # run
    python dictate.py --selftest # prove the pipeline end to end

Everything is local: audio never leaves the machine.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import numpy as np
import sounddevice as sd

from feedback import Feedback, LevelSink, NullFeedback, OverlayConfig, WordTiming

SAMPLE_RATE = 16000
FRAME_MS = 100
# Below this a recording is treated as silence and never transcribed.
SILENCE_FLOOR = 0.005

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Keep the ~460 MB model cache beside the app instead of in the user profile.
# Must be set before faster_whisper is imported, so it happens at module load.
os.environ.setdefault("HF_HOME", os.path.join(APP_DIR, "huggingface"))

CFG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "hotkey": "ctrl+alt+t",
    "cancel_hotkey": "ctrl+alt+x",
    # Estonian. Measured WER on TalTech's own Estonian demo audio (60 s of
    # broadcast speech, word error rate, this machine, int8 on CPU):
    #
    #   base                        77.8%   3.8 s   15.7x realtime
    #   small                       67.5%  10.8 s    5.5x realtime
    #   TalTech large-v3-turbo-et   24.6%  28.9 s    2.1x realtime
    #
    # Vanilla Whisper is unusable for Estonian. TalTech (Tallinn University of
    # Technology) fine-tuned this on ~1400 h of Estonian and it also still
    # transcribes English correctly, so it covers mixed dictation. The price is
    # real: a 5 s utterance takes ~2.4 s, which is what the "transcribing..."
    # state exists for. Use "base" or "small" in config.json if you would
    # rather have the speed.
    "model": "TalTechNLP/whisper-large-v3-turbo-et-verbatim:ct2",
    # Pinned rather than "auto": it skips the language-detection pass, which is
    # worth ~2x on this model, and the fine-tune reads English correctly anyway.
    "language": "et",
    "device": "cpu",
    "compute_type": "int8",
    "max_seconds": 120,
    "tap_ms": 350,
    "input_device": None,
    "overlay": True,
    "overlay_position": "screen-bottom",
    "overlay_interval_ms": 16,
    "preview_min_tail_s": 1.2,
    "preview_max_tail_s": 8.0,
    "initial_prompt": (
        "Tallinna Tehnikaulikooli loengud ja arendajavestlused. "
        "Terminid: repository, commit, branch, build, deployment, API, "
        "machine learning, neural network, TypeScript, Python."
    ),
}


@dataclass
class Config:
    hotkey: str = DEFAULTS["hotkey"]
    cancel_hotkey: str = DEFAULTS["cancel_hotkey"]
    model: str = DEFAULTS["model"]
    device: str = DEFAULTS["device"]
    compute_type: str = DEFAULTS["compute_type"]
    language: str = DEFAULTS["language"]
    max_seconds: int = DEFAULTS["max_seconds"]
    tap_ms: int = DEFAULTS["tap_ms"]
    input_device: object = DEFAULTS["input_device"]
    overlay: bool = DEFAULTS["overlay"]
    overlay_position: str = DEFAULTS["overlay_position"]
    overlay_interval_ms: int = DEFAULTS["overlay_interval_ms"]
    preview_min_tail_s: float = DEFAULTS["preview_min_tail_s"]
    preview_max_tail_s: float = DEFAULTS["preview_max_tail_s"]
    initial_prompt: str = DEFAULTS["initial_prompt"]
    extra: dict = field(default_factory=dict)

    def overlay_config(self) -> OverlayConfig:
        """Parse the overlay knobs once, at the process boundary. Everything
        downstream takes the typed value and trusts it (boundary-discipline)."""
        return OverlayConfig(
            enabled=self.overlay,
            position=self.overlay_position,      # type: ignore[arg-type]
            interval_ms=self.overlay_interval_ms,
            min_tail_s=self.preview_min_tail_s,
            max_tail_s=self.preview_max_tail_s,
        )

    @classmethod
    def load(cls, path: str = CFG_PATH) -> "Config":
        data = dict(DEFAULTS)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data.update(json.load(fh))
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.extra = {k: v for k, v in data.items() if k not in known}
        return cfg


# Estonian transcripts are full of non-cp1252 characters; the default Windows
# console codec would raise UnicodeEncodeError the moment we echo one.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def log(msg: str) -> None:
    print(f"[dictate] {msg}", flush=True)


# --------------------------------------------------------------------------
# Windows input: hotkeys, focus, clipboard, synthetic keystrokes
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
WM_HOTKEY = 0x0312
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
VK_CONTROL, VK_V, VK_ESCAPE = 0x11, 0x56, 0x1B

SW_RESTORE = 9

# Push-to-talk needs to know when the chord is released, and RegisterHotKey
# only ever reports presses. A low-level keyboard hook supplies the release.
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
VK_SHIFT, VK_MENU = 0x10, 0x12
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LMENU, VK_RMENU = 0xA4, 0xA5
MODIFIER_KEYS = {
    VK_SHIFT, VK_LSHIFT, VK_RSHIFT,
    VK_CONTROL, VK_LCONTROL, VK_RCONTROL,
    VK_MENU, VK_LMENU, VK_RMENU,
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = wintypes.BOOL
user32.SendMessageTimeoutW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
)
user32.SendMessageTimeoutW.restype = wintypes.LPARAM

# Without explicit 64-bit-safe signatures ctypes defaults every pointer return
# to a 32-bit int, which silently truncates GlobalLock's address on x64.
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalFree.restype = wintypes.HGLOBAL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.CloseClipboard.argtypes = ()
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.BringWindowToTop.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = (
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.SetWindowsHookExW.argtypes = (
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = (
    ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short

VK_NAMES = {
    **{chr(c).lower(): c for c in range(0x41, 0x5B)},
    **{chr(c): c for c in range(0x30, 0x3A)},
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "space": 0x20, "escape": 0x1B, "esc": 0x1B, "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+t' -> (modifier mask, virtual-key code)."""
    mods, vk = 0, None
    for part in spec.lower().split("+"):
        part = part.strip()
        if part == "alt":
            mods |= MOD_ALT
        elif part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in VK_NAMES:
            vk = VK_NAMES[part]
        else:
            raise ValueError(f"unknown hotkey part: {part!r} in {spec!r}")
    if vk is None:
        raise ValueError(f"hotkey {spec!r} needs a non-modifier key")
    return mods | MOD_NOREPEAT, vk


class HotkeyListener(threading.Thread):
    """System-wide hotkeys via RegisterHotKey, plus a low-level keyboard hook
    that reports modifier releases.

    RegisterHotKey needs no admin rights and only fires on the exact combo, but
    it never tells us when the chord is let go - which push-to-talk requires.
    The hook runs on this same thread, which owns the message loop it needs.
    """

    def __init__(self, bindings: dict[int, tuple[str, int, int]],
                 on_modifier_up=None):
        super().__init__(daemon=True, name="hotkeys")
        self.bindings = bindings
        self.on_modifier_up = on_modifier_up
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._quit = threading.Event()
        self._hook = None
        # Keep a reference: if this is collected the hook is uninstalled.
        self._hook_proc = None

    def _install_hook(self) -> None:
        def proc(code, wparam, lparam):
            if code == HC_ACTION and self.on_modifier_up:
                if wparam in (WM_KEYUP, WM_SYSKEYUP):
                    info = ctypes.cast(
                        lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if info.vkCode in MODIFIER_KEYS:
                        self.on_modifier_up(int(info.vkCode))
            return user32.CallNextHookEx(self._hook, code, wparam, lparam)

        self._hook_proc = HOOKPROC(proc)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, None, 0)
        if not self._hook:
            log("warning: keyboard hook unavailable, push-to-talk disabled "
                "(tap-to-toggle still works)")

    def run(self) -> None:
        try:
            for hotkey_id, (spec, _mods, _vk) in self.bindings.items():
                mods, vk = parse_hotkey(spec)
                if not user32.RegisterHotKey(None, hotkey_id, mods, vk):
                    err = ctypes.get_last_error()
                    raise RuntimeError(
                        f"RegisterHotKey failed for {spec!r} (error {err}). "
                        "Another app likely already owns that combination."
                    )
        except Exception as exc:  # noqa: BLE001
            # Never let this thread die quietly: a dead listener looks exactly
            # like a working app that simply ignores the hotkey.
            self._error = exc if isinstance(exc, RuntimeError) else RuntimeError(str(exc))
            self._ready.set()
            return
        self._install_hook()
        self._ready.set()

        msg = wintypes.MSG()
        while not self._quit.is_set():
            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)  # PM_REMOVE
            if got:
                if msg.message == WM_HOTKEY:
                    spec = self.bindings.get(msg.wParam)
                    if spec:
                        threading.Thread(
                            target=spec[1], args=(spec[2],), daemon=True
                        ).start()
            else:
                time.sleep(0.005)

    def wait_ready(self) -> None:
        self._ready.wait(5)
        if self._error:
            raise self._error

    def stop(self) -> None:
        self._quit.set()
        for hotkey_id in self.bindings:
            user32.UnregisterHotKey(None, hotkey_id)
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def restore_focus(hwnd: int) -> bool:
    """Bring the window that was focused at record time back to the front.

    A bare SetForegroundWindow is routinely refused when the calling process is
    not the one the user last interacted with, which would send the paste to
    whatever window happened to be in front. Borrowing the foreground thread's
    input queue is what actually makes it stick.
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.SendMessageTimeoutW(hwnd, 0x0111, SW_RESTORE, 0, 0x0002, 1000, None)

    current = int(kernel32.GetCurrentThreadId())
    fg = user32.GetForegroundWindow()
    fg_thread = int(user32.GetWindowThreadProcessId(fg, None) or 0)
    attached = False
    if fg_thread and fg_thread != current:
        attached = bool(user32.AttachThreadInput(current, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(current, fg_thread, False)
    return ok or int(user32.GetForegroundWindow() or 0) == hwnd


class Clipboard:
    """Put text on the clipboard and put back what was there.

    Only CF_UNICODETEXT is preserved; any non-text format on the clipboard is
    lost when we take ownership. Restoring arbitrary formats would mean
    serialising them, which is not worth it for dictation.
    """

    def __init__(self) -> None:
        self._saved_text: str | None = None

    def _open(self) -> None:
        for _ in range(10):
            if user32.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise RuntimeError("could not open clipboard")

    def save(self) -> None:
        self._open()
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if handle:
                ptr = kernel32.GlobalLock(handle)
                if ptr:
                    try:
                        self._saved_text = ctypes.c_wchar_p(ptr).value
                    finally:
                        kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def set_text(self, text: str) -> None:
        self._open()
        try:
            user32.EmptyClipboard()
            size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                raise RuntimeError("GlobalAlloc failed")
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                raise RuntimeError("GlobalLock failed")
            try:
                ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                raise RuntimeError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()

    def restore(self) -> None:
        # The saved text is a Python str copied out before we took ownership.
        # The original handle is deliberately not kept: EmptyClipboard() frees
        # the memory behind it, so handing it back to SetClipboardData later
        # would write into freed heap and corrupt the process.
        if self._saved_text is None:
            # Nothing text-shaped was there before. Clear it so the dictated
            # text does not linger on the clipboard.
            self._open()
            try:
                user32.EmptyClipboard()
            finally:
                user32.CloseClipboard()
            return
        self.set_text(self._saved_text)

    def __enter__(self) -> "Clipboard":
        self.save()
        return self

    def __exit__(self, *_exc) -> None:
        try:
            self.restore()
        except Exception:
            pass


def _key_event(code: int, up: bool) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(
        ki=KEYBDINPUT(wVk=code, wScan=0,
                      dwFlags=KEYEVENTF_KEYUP if up else 0,
                      time=0, dwExtraInfo=None)))


def send_keys(*vk: int) -> bool:
    """Send a key chord: every modifier down first, then released in reverse.

    Releasing each key before pressing the next turns Ctrl+V into a literal
    'v', which is exactly what the target field would receive.
    """
    events = [_key_event(code, up=False) for code in vk]
    events += [_key_event(code, up=True) for code in reversed(vk)]
    sent = user32.SendInput(len(events), (INPUT * len(events))(*events),
                            ctypes.sizeof(INPUT))
    return sent == len(events)


def paste_text(text: str, restore_focus_to: int = 0, hold_ms: int = 450,
               pump=None) -> bool:
    """Paste into the focused field via the clipboard.

    The clipboard route handles Unicode and large transcripts in one shot;
    per-character typing would be slow and would mangle non-ASCII text.

    The clipboard is only safe to restore once the target has actually read it.
    A target that is busy, or whose event loop is not spinning, will otherwise
    paste whatever we put back afterwards. Hence the hold, and ``pump`` for
    callers that can service their own loop while waiting.
    """
    if restore_focus_to:
        restore_focus(restore_focus_to)
        time.sleep(0.08)
    with Clipboard() as clip:
        clip.set_text(text)
        time.sleep(0.05)
        ok = send_keys(VK_CONTROL, VK_V)
        waited = 0.0
        step = 0.05
        while waited < hold_ms / 1000.0:
            time.sleep(step)
            waited += step
            if pump is not None:
                pump()
    return ok


# --------------------------------------------------------------------------
# Audio capture
# --------------------------------------------------------------------------

class Recorder:
    """Microphone capture into a growable float32 buffer.

    `levels` is a one-way sink written by the pump and read by the overlay's
    tick. It is deliberately not `self.frames`: the two readers do not need to
    agree with each other, so they do not share a lock (per
    separate-before-serializing-shared-state).
    """

    def __init__(self, device=None, levels=None) -> None:
        self.device = device
        self.levels = levels if levels is not None else LevelSink()
        self.q: queue.Queue[np.ndarray] = queue.Queue()
        self.stream = None
        self.frames: list[np.ndarray] = []
        self._samples = 0
        self.started = 0.0
        self._stop = threading.Event()

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            log(f"audio status: {status}")
        # RawInputStream hands over a cffi buffer, which has no .copy().
        self.q.put(bytes(indata))

    def start(self) -> None:
        self._stop.clear()
        self.frames = []
        self._samples = 0
        while not self.q.empty():
            self.q.get_nowait()
        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=int(SAMPLE_RATE * FRAME_MS / 1000),
            dtype="int16", channels=1, device=self.device, callback=self._callback,
        )
        self.stream.start()
        self.started = time.monotonic()
        threading.Thread(target=self._pump, daemon=True, name="pump").start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                block = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            samples = np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768.0
            if samples.size:
                self.frames.append(samples)
                self._samples += samples.size
                # Published last: a reader can only ever see a slightly stale
                # level, never a level for audio that is not in the buffer yet.
                self.levels.publish(
                    float(np.sqrt(np.mean(samples * samples))))

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    def get_audio(self, start_s: float = 0.0) -> np.ndarray:
        """Audio from `start_s` onward. Lock-free by construction: the pump is
        the only writer, it only ever appends, and `self._samples` is published
        after the list grows, so a reader that sees N samples sees a consistent
        prefix of them."""
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        skip = int(start_s * SAMPLE_RATE)
        if skip <= 0:
            return np.concatenate(self.frames)
        chunks, seen = [], 0
        for f in self.frames:
            if seen + f.size <= skip:
                seen += f.size
                continue
            offset = max(0, skip - seen)
            chunks.append(f[offset:])
            seen += f.size
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)

    def stop(self) -> np.ndarray:
        self._stop.set()
        time.sleep(0.15)
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        return self.get_audio()


# --------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------

def resolve_model(spec: str) -> str:
    """Turn a model spec into something WhisperModel can open.

    Three forms, because different models live in different places:
      "base"                                  a size faster-whisper knows
      "D:/path/to/ct2"                        a local CTranslate2 directory
      "org/name:subfolder"                    a Hugging Face repo + subfolder

    The repo form matters because the Estonian fine-tunes publish CTranslate2
    weights under `ct2/` rather than at the repo root, and WhisperModel only
    takes a path.
    """
    if os.path.isdir(spec) or "/" not in spec or spec.startswith("."):
        return spec
    repo, _, sub = spec.partition(":")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return spec
    patterns = [f"{sub}/*"] if sub else None
    local = snapshot_download(repo, allow_patterns=patterns)
    return os.path.join(local, sub) if sub else local


class Transcriber:
    """Lazily-loaded faster-whisper model. Loads off the hotkey path so the
    first press after startup is not several seconds slower than the rest."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()
        self._loading = False
        self._ready = threading.Event()
        threading.Thread(target=self._load, daemon=True, name="whisper-load").start()

    def _load(self) -> None:
        try:
            from faster_whisper import WhisperModel
            spec = self.cfg.model
            log(f"loading whisper model '{spec}' on {self.cfg.device} "
                f"({self.cfg.compute_type}) - first run downloads it")
            t0 = time.monotonic()
            self._model = WhisperModel(
                resolve_model(spec), device=self.cfg.device,
                compute_type=self.cfg.compute_type,
            )
            log(f"model ready in {time.monotonic() - t0:.1f}s")
        except Exception as exc:  # noqa: BLE001
            log(f"MODEL LOAD FAILED: {exc}")
        finally:
            self._ready.set()

    def wait_ready(self, timeout: float = 300) -> bool:
        return self._ready.wait(timeout)

    @property
    def ready(self) -> bool:
        return self._model is not None

    def transcribe(self, audio: np.ndarray) -> str:
        if self._model is None:
            self.wait_ready(300)
        if self._model is None:
            raise RuntimeError("whisper model failed to load")
        language = None if self.cfg.language == "auto" else self.cfg.language
        with self._lock:
            segments, info = self._model.transcribe(
                audio,
                language=language,
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                initial_prompt=self.cfg.initial_prompt or None,
                condition_on_previous_text=False,
            )
            parts = [s.text.strip() for s in segments]
        text = " ".join(p for p in parts if p).strip()
        detected = getattr(info, "language", "?")
        log(f"transcribed {len(audio) / SAMPLE_RATE:.1f}s audio "
            f"(lang={detected}) -> {len(text)} chars")
        return text

    def transcribe_words(self, audio: np.ndarray,
                         use_prompt: bool = False) -> tuple:
        """Approximate, cheap, timestamped words for the live preview.

        Two operations rather than one with a `preview=True` flag, because the
        two differ in three settings that the caller must not have to know:
        beam 1 vs 5, no VAD vs VAD, timestamps vs none. Only this method's
        signature knows that; no faster-whisper Segment escapes it.

        Times are relative to the start of `audio`; rebasing onto the
        recording's clock is the preview worker's job, not this one.
        """
        if self._model is None:
            self.wait_ready(300)
        if self._model is None:
            raise RuntimeError("whisper model failed to load")
        language = None if self.cfg.language == "auto" else self.cfg.language
        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=language,
                beam_size=1,
                vad_filter=False,
                word_timestamps=True,
                initial_prompt=(self.cfg.initial_prompt or None) if use_prompt else None,
                condition_on_previous_text=False,
            )
            out = []
            for seg in segments:
                for w in (getattr(seg, "words", None) or ()):
                    text = (w.word or "").strip()
                    if text:
                        out.append(WordTiming(text, float(w.start), float(w.end)))
        return tuple(out)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

class Dictation:
    """Turns hotkey presses into dictation.

    Two gestures share one key:

    * press and hold  - records while held, transcribes on release
    * tap             - starts recording, keeps going until the next press

    Which one you meant is only known once the chord is either still held or
    already let go, so a press starts out 'pending' and settles after
    ``tap_ms``.
    """

    def __init__(self, cfg: Config, feedback=None) -> None:
        self.cfg = cfg
        self.transcriber = Transcriber(cfg)
        self.recorder: Recorder | None = None
        self.recording = False
        self.cancelled = False
        self.focus_hwnd = 0
        self.mode = "idle"  # idle | pending | hold | toggle
        # Additive: a headless instance makes every feedback call a no-op,
        # which is exactly the behaviour this class had before the overlay.
        self.feedback = feedback if feedback is not None else NullFeedback()
        self._lock = threading.Lock()
        self._tap_timer: threading.Timer | None = None
        self._max_timer: threading.Timer | None = None

    def beep(self, kind: str) -> None:
        try:
            import winsound
            winsound.Beep(880 if kind == "start" else 660, 120)
        except Exception:
            pass

    def chord_held(self) -> bool:
        """Is any modifier of the hotkey still physically down?"""
        for part in self.cfg.hotkey.lower().split("+"):
            part = part.strip()
            vk = None
            if part in ("ctrl", "control"):
                vk = VK_CONTROL
            elif part == "alt":
                vk = VK_MENU
            elif part == "shift":
                vk = VK_SHIFT
            if vk is not None and user32.GetAsyncKeyState(vk) & 0x8000:
                return True
        return False

    def on_modifier_up(self, _vk: int) -> None:
        """Fired by the keyboard hook when a modifier is released."""
        if self.mode == "hold":
            threading.Thread(target=self._finish_hold, daemon=True).start()

    def _finish_hold(self) -> None:
        with self._lock:
            if self.mode == "hold":
                log("key released")
                self._stop_phase()

    def _settle_press(self) -> None:
        with self._lock:
            if self.mode != "pending":
                return
            if self.chord_held():
                self.mode = "hold"
                log("push-to-talk: release to finish")
            else:
                self.mode = "toggle"
                log("toggle mode: press again to finish")

    def toggle(self, _hotkey_id: int = 0) -> None:
        with self._lock:
            if self.mode in ("hold", "toggle"):
                self._stop_phase()
                return
            if self.mode == "pending":
                return
            self._start_phase()
            self.mode = "pending"
        self._tap_timer = threading.Timer(
            self.cfg.tap_ms / 1000.0, self._settle_press)
        self._tap_timer.daemon = True
        self._tap_timer.start()

    def cancel(self, _hotkey_id: int = 0) -> None:
        with self._lock:
            if self.recording:
                self.cancelled = True
                self._stop_phase()

    def _start_phase(self) -> None:
        self.focus_hwnd = foreground_window()
        self.cancelled = False
        self.feedback.set_focus_window(self.focus_hwnd)
        self.recorder = Recorder(self.cfg.input_device, levels=self.feedback.levels())
        try:
            self.recorder.start()
        except Exception as exc:  # noqa: BLE001
            log(f"could not open microphone: {exc}")
            log(f"available inputs: {[d['name'] for d in sd.query_devices() if d['max_input_channels'] > 0]}")
            self.recorder = None
            return
        self.recording = True
        self.feedback.dictation_started(self.recorder, self.transcriber)
        self.beep("start")
        # The cap used to be logged and never enforced, so a forgotten
        # push-to-talk could record until the process died.
        self._max_timer = threading.Timer(self.cfg.max_seconds, self._force_stop)
        self._max_timer.daemon = True
        self._max_timer.start()
        log(f"recording (max {self.cfg.max_seconds}s, "
            f"{self.cfg.cancel_hotkey} cancels)")

    def _force_stop(self) -> None:
        with self._lock:
            if self.recording:
                log(f"hit the {self.cfg.max_seconds}s cap, stopping")
                self._stop_phase()

    def _stop_phase(self) -> None:
        """Tear down under the lock, finish outside it.

        Transcription takes 2-4 s. Holding the lock across it made `cancel()`
        and the next press block for the whole of it - invisible when the only
        feedback was a beep, and unusable once the overlay shows a "thinking"
        state the user will want to interrupt.
        """
        for timer in (self._tap_timer, self._max_timer):
            if timer:
                timer.cancel()
        self._tap_timer = self._max_timer = None
        self.mode = "idle"
        if not self.recording or self.recorder is None:
            self.recording = False
            return
        self.recording = False

        # ORDER IS LOAD-BEARING: stop the preview worker reading a recorder we
        # are about to seal, and show the thinking state, before the buffer is
        # finalised.
        self.feedback.capture_ended()
        audio = self.recorder.stop()
        self.recorder = None
        self.beep("stop")
        threading.Thread(target=self._finish, daemon=True, name="finish",
                         args=(audio, self.cancelled, self.focus_hwnd)).start()

    def _finish(self, audio: np.ndarray, cancelled: bool, focus_hwnd: int) -> None:
        if cancelled:
            log("discarded")
            self.feedback.dictation_discarded("discarded")
            return
        if audio.size == 0:
            log("no audio captured")
            self.feedback.dictation_discarded("no audio captured")
            return
        if self.recorder_peak(audio) < SILENCE_FLOOR:
            log("captured only silence, nothing typed")
            self.feedback.dictation_discarded("only silence")
            return
        try:
            text = self.transcriber.transcribe(audio)
        except Exception as exc:  # noqa: BLE001
            log(f"transcription failed: {exc}")
            self.feedback.dictation_discarded("transcription failed")
            return
        if not text:
            log("transcription was empty, nothing typed")
            self.feedback.dictation_discarded("nothing heard")
            return
        if paste_text(text, focus_hwnd):
            self.feedback.dictation_delivered(text)
            log(f"typed: {text!r}")
        else:
            log("failed to send Ctrl+V (target may be running elevated)")
            self.feedback.dictation_discarded("paste refused (target elevated?)")

    @staticmethod
    def recorder_peak(audio: np.ndarray) -> float:
        return float(np.abs(audio).max()) if audio.size else 0.0


def run(cfg: Config) -> int:
    feedback = NullFeedback()
    if cfg.overlay:
        feedback = Feedback(cfg.overlay_config(), log)
        if not feedback.open():
            feedback = NullFeedback()
    app = Dictation(cfg, feedback=feedback)
    listener = HotkeyListener(
        {
            1: (cfg.hotkey, app.toggle, 0),
            2: (cfg.cancel_hotkey, app.cancel, 0),
        },
        on_modifier_up=app.on_modifier_up,
    )
    listener.start()
    try:
        listener.wait_ready()
    except RuntimeError as exc:
        log(str(exc))
        feedback.close()
        return 2

    log(f"hold  {cfg.hotkey}  -> record while held, types on release")
    log(f"tap   {cfg.hotkey}  -> record until you press it again")
    log(f"press {cfg.cancel_hotkey} -> discard")
    log("model loads in the background; first hotkey press may take a moment")
    try:
        feedback.serve()          # Tk main loop, or a sleep loop if headless
    except KeyboardInterrupt:
        log("stopping")
    finally:
        listener.stop()
        feedback.close()
    return 0


# --------------------------------------------------------------------------
# Self-test: proves capture, ASR and injection without needing a human
# --------------------------------------------------------------------------

def selftest(cfg: Config) -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" - {detail}" if detail else ""))

    log("1. input devices")
    inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    check("at least one input device", bool(inputs),
          ", ".join(d["name"] for d in inputs) or "none found")

    log("2. microphone capture")
    peak = 0.0
    try:
        sink = LevelSink()
        rec = Recorder(cfg.input_device, levels=sink)
        rec.start()
        time.sleep(1.5)
        audio = rec.stop()
        peak = Dictation.recorder_peak(audio)
        check("captures audio frames", audio.size > 0,
              f"{len(audio) / SAMPLE_RATE:.2f}s captured")
        # Assert the *path* is live, not that the room is loud: a silent room
        # legitimately yields peak 0.0, and the level sink is the thing that
        # proves the callback is running end to end.
        gen = sink.read()[2]
        check("audio callback is running end to end", gen >= 10,
              f"{gen} frames delivered to the level sink in 1.5s, peak={peak:.4f}")
    except Exception as exc:  # noqa: BLE001
        check("microphone capture", False, str(exc))

    log("3. speech-to-text round trip")
    wav = os.path.join(APP_DIR, "_selftest.wav")
    phrase = "The deployment pipeline builds the repository and runs the tests."
    made = _synthesise(wav, phrase)
    check("synthesised a speech sample", made, wav if made else "no SAPI voice")
    if made:
        tr = Transcriber(cfg)
        if not tr.wait_ready(600):
            check("whisper model loads", False, "timed out")
        else:
            check("whisper model loads", True, cfg.model)
            decoded = _read_wav(wav)
            # Pin the language: auto-detect on synthetic TTS audio picks
            # nonsense (it reported 'nn' for a plain English sentence), which
            # would make this assertion flaky. Auto-detect stays the default
            # for real dictation.
            saved = tr.cfg.language
            tr.cfg.language = "en"
            try:
                said = tr.transcribe(decoded)
            finally:
                tr.cfg.language = saved
            words = [w for w in phrase.lower().split() if len(w) > 3]
            hits = sum(1 for w in words if w.strip(".,") in said.lower())
            ratio = hits / len(words) if words else 0.0
            check("transcript matches the spoken phrase", ratio >= 0.6,
                  f"{hits}/{len(words)} keywords, got {said!r}")
    if os.path.exists(wav):
        os.remove(wav)

    log("4. clipboard round trip")
    probe = "tere ÃµÃ¤Ã¶Ã¼ 1234"
    with Clipboard() as clip:
        clip.set_text(probe)
        check("writes unicode to the clipboard", _read_clipboard() == probe, probe)
        clip.restore()

    passed = sum(1 for _, ok, _ in results if ok)
    log(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


def modetest(cfg: Config) -> int:
    """Check the hold-vs-tap decision without a microphone or a model."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" - {detail}" if detail else ""))

    events: list[str] = []

    class FakeRecorder:
        def __init__(self, device=None, levels=None) -> None:
            self.started = False
            self.levels = levels

        def start(self) -> None:
            self.started = True
            events.append("record-start")

        def stop(self) -> np.ndarray:
            events.append("record-stop")
            return np.full(SAMPLE_RATE, 0.5, dtype=np.float32)

    class FakeTranscriber:
        def __init__(self, _cfg) -> None:
            pass

        def transcribe(self, _audio) -> str:
            return "dictated words"

    class RecordingFeedback(NullFeedback):
        """Records the order the host publishes in, so the mode test can assert
        that capture_ended() really does precede the recorder being sealed."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def capture_ended(self) -> None:
            self.calls.append("capture_ended")

        def dictation_delivered(self, text: str) -> None:
            self.calls.append("delivered")
            events.append("deliver")

        def dictation_discarded(self, reason: str) -> None:
            self.calls.append(f"discarded:{reason}")
            events.append("discard")

    saved = {n: globals()[n] for n in ("Recorder", "Transcriber", "paste_text")}
    globals()["Recorder"] = FakeRecorder
    globals()["Transcriber"] = FakeTranscriber
    globals()["paste_text"] = lambda *a, **k: events.append("paste") or True
    try:
        cfg.tap_ms = 120
        fb = RecordingFeedback()
        app = Dictation(cfg, feedback=fb)
        app.beep = lambda _kind: None
        settle = cfg.tap_ms / 1000.0 + 0.10
        # _finish runs on its own thread now, so let it land before asserting.
        drained = settle + 0.3

        # --- quick tap ---
        held = False
        app.chord_held = lambda: held
        app.toggle()
        check("a press starts recording", app.recording and app.mode == "pending",
              f"mode={app.mode}")
        time.sleep(settle)
        check("a quick tap settles into toggle", app.mode == "toggle",
              f"mode={app.mode}")
        app.toggle()
        check("the next press stops it", not app.recording and app.mode == "idle",
              f"mode={app.mode}")
        time.sleep(drained)
        check("the transcript is pasted once", events.count("paste") == 1,
              f"events={events}")
        check("capture_ended precedes the sealed buffer",
              fb.calls[:1] == ["capture_ended"], f"calls={fb.calls}")

        # --- press and hold ---
        events.clear()
        app.toggle()
        held = True
        time.sleep(settle)
        check("a held chord settles into push-to-talk", app.mode == "hold",
              f"mode={app.mode}")
        app.on_modifier_up(VK_CONTROL)
        time.sleep(0.3)
        check("releasing the key stops and pastes",
              not app.recording and events.count("paste") == 1,
              f"mode={app.mode} events={events}")

        # --- a second release must not fire twice ---
        app.on_modifier_up(VK_CONTROL)
        time.sleep(0.2)
        check("a stray release does nothing", events.count("paste") == 1,
              f"events={events}")

        # --- cancel ---
        events.clear()
        app.toggle()
        held = False
        time.sleep(settle)
        app.cancel()
        time.sleep(drained)
        check("cancel stops without pasting",
              not app.recording and "paste" not in events, f"events={events}")
        check("cancel is reported to the overlay",
              any(c.startswith("discarded") for c in fb.calls),
              f"calls={fb.calls[-2:]}")

        # --- the recording cap actually fires ---
        cfg.max_seconds = 0
        events.clear()
        app.toggle()
        time.sleep(drained)
        check("max_seconds cap stops a stuck recording",
              not app.recording and "record-stop" in events,
              f"events={events}")
        cfg.max_seconds = 120
    finally:
        globals().update(saved)

    passed = sum(1 for _, ok, _ in results if ok)
    log(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


def injecttest() -> int:
    """Prove the paste path against a real Win32 text field.

    Kept out of --selftest because it needs to put a window on screen; run it
    by hand whenever the injection path changes.
    """
    import tkinter as tk

    probe = "tere ÃµÃ¤Ã¶Ã¼ â€” dictation works, 1234"
    root = tk.Tk()
    root.title("dictate inject test")
    root.geometry("560x180+140+140")
    box = tk.Text(root, font=("Segoe UI", 14), wrap="word")
    box.pack(fill="both", expand=True)
    root.update()

    # Take foreground the same way the real dictation path does, so the test
    # exercises restore_focus() rather than assuming the window is already up.
    hwnd = int(root.winfo_id())
    log(f"test window hwnd={hwnd}, focus restored={restore_focus(hwnd)}")
    box.focus_set()
    root.update()
    for _ in range(20):
        root.update()
        time.sleep(0.05)
    live = int(user32.GetForegroundWindow() or 0)
    if live not in (hwnd, int(root.wm_frame(), 0)):
        log(f"warning: foreground is {live}, not the test window {hwnd}")

    log(f"sending {probe!r} via clipboard + Ctrl+V")
    sent = paste_text(probe, pump=root.update)
    time.sleep(0.6)
    root.update()
    got = box.get("1.0", "end-1c")
    root.destroy()

    log(f"SendInput reported success: {sent}")
    log(f"field now contains: {got!r}")
    if got == probe:
        log("PASS  text was pasted into the focused field verbatim")
        return 0
    log("FAIL  the field did not receive the expected text")
    return 1


def _synthesise(wav_path: str, text: str) -> bool:
    """Use the Windows SAPI voice as a stand-in for a human talking."""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav_path}'); "
        f"$s.Speak('{text}'); $s.Dispose()"
    )
    os.system(f'powershell -NoProfile -Command "{script}" >NUL 2>&1')
    return os.path.exists(wav_path) and os.path.getsize(wav_path) > 1000


def _read_wav(path: str) -> np.ndarray:
    """Decode a wav to mono 16 kHz float32.

    SAPI writes 22.05 kHz, but Whisper's filterbank is built for 16 kHz.
    Feeding the raw samples makes the audio sound slowed down, which is enough
    to send Whisper into a hallucination loop.

    Resampling is done here rather than via faster_whisper.audio.decode_audio
    because that helper passes metadata_errors= to av.open, which the installed
    PyAV rejects.
    """
    import av

    chunks: list[np.ndarray] = []
    with av.open(path) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(
            format="s16", layout="mono", rate=SAMPLE_RATE
        )
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                chunks.append(
                    resampled.to_ndarray().astype(np.float32).reshape(-1) / 32768.0
                )
        for resampled in resampler.resample(None):  # flush the tail
            chunks.append(
                resampled.to_ndarray().astype(np.float32).reshape(-1) / 32768.0
            )
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def _read_clipboard() -> str | None:
    user32.OpenClipboard(None)
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true",
                        help="verify capture, ASR and clipboard, then exit")
    parser.add_argument("--injecttest", action="store_true",
                        help="paste text into a scratch window and read it back")
    parser.add_argument("--modetest", action="store_true",
                        help="check the hold-vs-tap gesture logic")
    parser.add_argument("--config", default=CFG_PATH)
    parser.add_argument("--model", help="override whisper model size")
    parser.add_argument("--language", help="auto, or a code like 'et'/'en'")
    parser.add_argument("--device", help="whisper device: cpu or cuda")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    if args.model:
        cfg.model = args.model
    if args.language:
        cfg.language = args.language
    if args.device:
        cfg.device = args.device

    # Fail before loading a 460 MB model over a typo in the config.
    try:
        parse_hotkey(cfg.hotkey)
        parse_hotkey(cfg.cancel_hotkey)
    except ValueError as exc:
        log(str(exc))
        return 2

    if args.injecttest:
        return injecttest()
    if args.modetest:
        return modetest(cfg)
    return selftest(cfg) if args.selftest else run(cfg)


if __name__ == "__main__":
    sys.exit(main())
