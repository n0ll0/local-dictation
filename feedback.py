"""Visual feedback for dictate.py: a listening blob, a thinking state, and a
live transcription preview.

Shape (architect Phase D, base = arena candidate 1):

  * The overlay is a pure function of an immutable `OverlayFrame`.
  * The whole cross-thread surface is four one-way setters plus `snapshot()`.
    It is a snapshot board, not an event queue: four producer threads would
    otherwise have to agree about arrival order, and they cannot.
  * Everything expensive (preview scheduling, envelope smoothing, wrapping,
    the blit) sits behind that snapshot or in a pure function downstream of it,
    so the whole visual language is testable with no window, mic or model.

Sections, in dependency order:

    domain types   - frozen, no platform imports, assertable from a unit test
    channel        - the snapshot board
    pure paint     - level->energy, envelope, blob, text layout
    surface        - the only module that calls gdi32/user32 for drawing
    preview        - approximate transcript, prefix-stability windowing
    overlay        - Tk clock + the Feedback facade the host talks to

The surface is Win32 layered rather than Tk widgets because Tk's deiconify and
-topmost both activate the window, and a stolen foreground window means the
transcript lands in the wrong field. Verified by design/spike_focus.py.
"""
from __future__ import annotations

import ctypes
import math
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

import numpy as np

Phase = Literal["hidden", "listening", "thinking", "delivered", "discarded"]
Position = Literal["screen-bottom", "caret"]

# Windows uninstalls a WH_KEYBOARD_LL hook whose callback cannot take the GIL
# within this budget (max 1000 ms on Win10 1709+, default 1000 ms). The failure
# is silent: push-to-talk stops working, tap-to-toggle keeps working. Measured
# frame cost is ~17 ms, so the render has ~1.7% of the budget; the watchdog in
# overlay.py exists to keep that true if text layout ever gets expensive.
HOOK_GIL_BUDGET_S = 1.0
FRAME_BUDGET_S = 1 / 60


# ======================================================================
# domain types - no numpy, no tkinter, no ctypes, no whisper
# ======================================================================

@dataclass(frozen=True, slots=True)
class WordTiming:
    """One word of an *approximate* transcript, on the recording's own clock.

    Invariant: 0.0 <= start_s <= end_s. Seconds from the first captured
    sample, not wall clock, so a pass starting at a window offset rebases by
    addition alone.
    """
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class PreviewText:
    """What the user reads under the blob.

    `provisional` marks the tail that has not yet survived two agreeing
    passes; the UI renders it dimmer so a word that later changes reads as
    "still settling" rather than "the tool retracted something".

    `dropped_head` means the stability window was re-baselined and the start of
    the utterance is no longer shown. The final pass is authoritative, so this
    is legibility, not correctness.
    """
    words: tuple[WordTiming, ...]
    provisional: bool
    dropped_head: bool

    def render(self, limit: int) -> str:
        text = " ".join(w.text for w in self.words).strip()
        return text if len(text) <= limit else "..." + text[-limit:]


@dataclass(frozen=True, slots=True)
class OverlayFrame:
    """The complete visual input for exactly one frame.

    Nothing downstream reads the Recorder, the Transcriber, or the clock.
    Given a frame and a time, the pixels are determined - which is exactly why
    the channel is a snapshot board and not a queue: a queue would make the
    image a function of arrival order, and arrival order is the one thing four
    producer threads do not agree on.
    """
    phase: Phase
    level: float          # 0..1, already enveloped
    preview: PreviewText | None
    detail: str           # only non-empty for discarded / error


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    enabled: bool = True
    position: Position = "screen-bottom"
    interval_ms: int = 16
    min_tail_s: float = 1.2
    max_tail_s: float = 8.0
    context_pad_s: float = 0.4
    preview_chars: int = 220
    overlay_preview: bool = False   # live transcript under the blob
    diameter: int = 60              # the overlay is a circle of this many pixels
    alpha: float = 0.62             # whole-window opacity; needs WS_EX_LAYERED
    gap: int = 40
    margin: int = 28
    delivered_ms: int = 1400
    fps_watchdog: bool = True


class LevelSink:
    """One-way, lock-free microphone-energy sink.

    Written by the audio pump at 10 Hz, read by the Tk thread at 60 Hz. A
    single attribute store of an immutable tuple is atomic under the GIL, so
    there is no lock and no contention with the buffer the pump is appending
    to. A reader may see a value one frame old; that is correct, because `at`
    says when it was true.

    Per separate-before-serializing-shared-state: the level stream and the
    audio buffer do not need to agree with each other, so they do not share a
    lock.
    """
    __slots__ = ("_v",)

    def __init__(self) -> None:
        self._v = (0.0, 0.0, 0)      # (rms, monotonic_at, generation)

    def publish(self, rms: float) -> None:
        self._v = (rms, time.monotonic(), self._v[2] + 1)

    def read(self) -> tuple[float, float, int]:
        return self._v

    def fresh(self, now: float, max_age_s: float = 0.4) -> float:
        rms, at, _ = self._v
        return rms if (now - at) <= max_age_s else 0.0


# ======================================================================
# channel - the snapshot board
# ======================================================================

class OverlayChannel:
    """One-way snapshot board from worker threads to the UI thread.

    Deliberately not an event queue. Producers never wake the consumer; the
    consumer samples `snapshot()` on its own tick. That removes every ordering
    question between the audio pump, the preview worker and the hotkey dispatch
    thread, and costs nothing, because a 60 Hz sampler cannot outrun any of
    them.

    One writer per field, one reader:
        level          <- the audio pump (via LevelSink)
        preview        <- PreviewWorker
        phase, detail  <- Dictation's dispatch thread

    Invariant: every write carries the session id it was made in, and a write
    for an already-ended session is dropped. A slow preview pass belonging to
    recording N can therefore never paint over recording N+1. This is the only
    ordering rule in the design.
    """
    __slots__ = ("_lock", "_session", "_phase", "_detail", "_preview", "_levels")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = 0
        self._phase: Phase = "hidden"
        self._detail = ""
        self._preview: PreviewText | None = None
        self._levels = LevelSink()

    def begin_session(self) -> int:
        with self._lock:
            self._session += 1
            self._phase = "listening"
            self._detail = ""
            self._preview = None
            return self._session

    @property
    def session(self) -> int:
        return self._session

    def attach_levels(self, levels: LevelSink) -> None:
        self._levels = levels

    def publish_state(self, phase: Phase, detail: str = "") -> None:
        with self._lock:
            self._phase = phase
            self._detail = detail

    def publish_preview(self, preview: PreviewText) -> None:
        with self._lock:
            self._preview = preview

    def close_session(self, phase: Phase, detail: str = "") -> None:
        """Latch a terminal state that survives until the next begin_session."""
        with self._lock:
            self._session += 1          # invalidate anything still in flight
            self._phase = phase
            self._detail = detail
            self._preview = None

    def snapshot(self) -> OverlayFrame:
        with self._lock:
            phase, detail, preview = self._phase, self._detail, self._preview
        return OverlayFrame(
            phase=phase,
            level=self._levels.fresh(time.monotonic()),
            preview=preview,
            detail=detail,
        )


# ======================================================================
# pure paint
# ======================================================================

SPEECH_FLOOR_RMS = 0.005


def level_to_energy(rms: float) -> float:
    """RMS to 0..1 on a dBFS curve.

    Linear RMS makes normal speech look tiny next to a shout; the ear does not
    work that way. Below the speech floor this lands near the resting radius so
    the blob still breathes on room noise instead of snapping to zero.
    """
    if rms <= 1e-6:
        return 0.0
    db = 20.0 * math.log10(rms)
    lo, hi = -60.0, -8.0
    return max(0.0, min(1.0, (db - lo) / (hi - lo)))


class LevelEnvelope:
    """Attack/release smoothing plus a little noise-gate hysteresis.

    Stateful on purpose: animation is. Everything else in the paint path is a
    pure function of a frame, and this is the documented exception.
    """
    __slots__ = ("_v", "_attack", "_release", "_gate")

    def __init__(self, attack: float = 0.35, release: float = 0.08,
                 gate: float = SPEECH_FLOOR_RMS) -> None:
        self._v = 0.0
        self._attack = attack
        self._release = release
        self._gate = gate

    def step(self, rms: float, dt: float) -> float:
        target = level_to_energy(rms)
        if rms < self._gate:
            target = min(target, 0.06)      # resting wobble, not a hard stop
        k = self._attack if target > self._v else self._release
        self._v += (target - self._v) * min(1.0, k * max(dt, 1e-3) * 12.0)
        return self._v


# Blob tint per phase. The colour is the only feedback left now that the text
# panel is gone, so "what is it doing" has to be readable at a glance.
TINTS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    #              resting colour          loud colour
    "listening": ((104.0, 78.0, 246.0), (168.0, 128.0, 255.0)),
    "thinking":  ((236.0, 132.0, 36.0), (255.0, 206.0, 96.0)),
    "delivered": ((38.0, 190.0, 140.0), (110.0, 226.0, 186.0)),
    "discarded": ((92.0, 96.0, 112.0), (128.0, 134.0, 152.0)),
    "hidden":    ((104.0, 78.0, 246.0), (168.0, 128.0, 255.0)),
}


def blob_rgba(size: int, energy: float, t: float, spin: float = 0.0,
              tint: tuple[tuple[float, float, float],
                          tuple[float, float, float]]
              = ((104.0, 78.0, 246.0), (168.0, 128.0, 255.0))) -> np.ndarray:
    """Premultiplied BGRA for a soft-edged reactive blob. Pure.

    Radius is bounded well inside `size / 2` so the shape is never clipped by
    its own bitmap at full energy - a blob that flattens against the top edge
    as you speak louder is worse than a small one. The lobe term is kept low:
    at high amplitude three lobes stop reading as organic and start reading as
    a Reuleaux triangle.

    `spin` rotates the lobes. At zero it is a slow breath; during the
    transcribing pass it is driven hard so the state reads as busy without any
    text to explain it. `tint` is (resting, loud) colours for the phase.
    """
    y, x = np.mgrid[0:size, 0:size]
    c = size / 2.0
    dx, dy = x - c, y - c
    r = np.hypot(dx, dy)
    ang = np.arctan2(dy, dx) + spin

    lobes = 1.0 + (0.018 + 0.026 * energy) * np.sin(3.0 * ang + t * 1.7)
    edge = (0.230 + 0.185 * energy) * size * lobes        # max 0.46 * size

    core = np.clip((edge - r) / (0.055 * size), 0.0, 1.0)
    core = core * core * (3.0 - 2.0 * core)

    # Halo *outside* the core edge, fading out before the bitmap boundary.
    # Computing it as a function of r alone would put all its energy inside
    # the blob, where `core` masks it and nothing shows.
    halo = np.clip((r - edge * 0.88) / (size * 0.16), 0.0, 1.0) ** 0.7
    halo *= np.clip(1.0 - r / (size * 0.48), 0.0, 1.0) ** 1.4

    a = np.clip(core + halo * (0.30 + 0.40 * energy), 0.0, 1.0)

    base = np.array(tint[0])
    hot = np.array(tint[1])
    rgb = base + (hot - base) * energy
    out = np.empty((size, size, 4), dtype=np.uint8)
    out[..., :3] = (rgb * a[..., None]).astype(np.uint8)
    out[..., 3] = (a * 255).astype(np.uint8)
    return out


def _panel_bgra(w: int, h: int, radius: int = 16) -> np.ndarray:
    """Rounded translucent card behind the preview text.

    Signed distance to a rounded rectangle: `d` is negative inside, and the
    half-pixel of slack in `0.5 - d` antialiases the edge instead of leaving
    the staircase you get from testing membership directly.
    """
    y, x = np.mgrid[0:h, 0:w]
    r = min(radius, w // 2, h // 2)
    dx = np.maximum(np.maximum(r - x, x - (w - 1 - r)), 0)
    dy = np.maximum(np.maximum(r - y, y - (h - 1 - r)), 0)
    d = np.hypot(dx, dy) - r
    a = np.clip(0.5 - d, 0.0, 1.0) * 0.82
    out = np.empty((h, w, 4), dtype=np.uint8)
    out[..., :3] = (np.array([18.0, 18.0, 26.0]) * a[..., None]).astype(np.uint8)
    out[..., 3] = (a * 255).astype(np.uint8)
    return out


def _over(dst: np.ndarray, src: np.ndarray, x: int, y: int) -> None:
    """Source-over composite of premultiplied BGRA into dst at (x, y).

    Everything is premultiplied, so the channels combine with the same
    `a = 255 - src_alpha` factor rather than needing a separate unpremultiply.
    """
    dh, dw = dst.shape[:2]
    sh, sw = src.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(dw, x + sw), min(dh, y + sh)
    if x0 >= x1 or y0 >= y1:
        return
    s = src[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.uint16)
    d = dst[y0:y1, x0:x1].astype(np.uint16)
    inv = (255 - s[..., 3])[..., None]
    dst[y0:y1, x0:x1, :3] = (s[..., :3] + d[..., :3] * inv // 255).astype(np.uint8)
    out_a = s[..., 3] + d[..., 3] * (255 - s[..., 3]) // 255
    dst[y0:y1, x0:x1, 3] = np.minimum(out_a, 255).astype(np.uint8)


class TextMeasure(Protocol):
    """Injected so layout is testable with no GDI and no font."""
    def width(self, text: str) -> int: ...
    def line_height(self) -> int: ...


def wrap_text(text: str, measure: TextMeasure, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if cur and measure.width(trial) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        lines.append(cur)
    return lines or [""]


def layout_preview(preview: PreviewText | None, cfg: OverlayConfig,
                   measure: TextMeasure) -> list[tuple[str, bool]]:
    """Preview text as (line, provisional) pairs. Pure given a TextMeasure."""
    if preview is None or not preview.words:
        return []
    text = preview.render(cfg.preview_chars)
    return [(ln, preview.provisional)
            for ln in wrap_text(text, measure, int(cfg.diameter * 0.66))]


# ======================================================================
# preview policy - pure half
# ======================================================================

def _norm(word: str) -> str:
    return "".join(ch for ch in word.casefold() if ch.isalnum())


def stable_prefix(previous: tuple[WordTiming, ...],
                  current: tuple[WordTiming, ...]) -> int:
    """Number of leading words two consecutive passes agree on.

    Compared casefolded and punctuation-stripped: Whisper is not stable about
    trailing commas or capitalisation, and disagreeing on every comma would
    defeat the whole scheme.

    Both sequences must describe the same audio window, which the worker
    guarantees by rebasing on words[n-1].end_s.
    """
    n = 0
    for a, b in zip(previous, current):
        if _norm(a.text) != _norm(b.text):
            break
        n += 1
    return n


class AudioSource(Protocol):
    """What the preview worker needs from a recording.

    Recorder satisfies this structurally; it is spelled out here so the worker
    never imports dictate.
    """
    def elapsed_s(self) -> float: ...
    def get_audio(self, start_s: float = 0.0) -> np.ndarray: ...


class PreviewWorker(threading.Thread):
    """Turns a growing recording into an approximate transcript, on its own
    thread, published into an OverlayChannel.

    Three decisions live here and nowhere else:
      * how much audio to re-transcribe - prefix-stability windowing
      * when to re-transcribe            - self-clocking, <=50% duty cycle
      * what to do when it stops agreeing - re-baseline, bounded

    Invariants:
      * At most one pass is in flight, ever.
      * The worker never runs the final pass, and the final pass waits on
        nothing the worker owns except one pass worth of model lock. That
        residual is real: ctranslate2 cannot cancel a pass in flight, so the
        stop latency carries up to one preview pass. It is bounded rather than
        eliminated, because there is no mechanism to eliminate it.
      * `close()` is idempotent and safe from the dispatch thread that is also
        stopping the recorder. Correctness does not depend on it finishing.
    """
    daemon = True

    def __init__(self, cfg: OverlayConfig, channel: OverlayChannel,
                 log: Callable[[str], None]) -> None:
        super().__init__(name="preview")
        self.cfg = cfg
        self.channel = channel
        self._log = log
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._source: AudioSource | None = None
        self._transcriber = None
        self._session = 0
        self._committed: tuple[WordTiming, ...] = ()
        self._prev: tuple[WordTiming, ...] = ()
        self._window_start_s = 0.0
        self._last_pass_end = 0.0
        self._last_pass_s = 0.0
        self._started_at = 0.0

    def open(self, source: AudioSource, transcriber) -> int:
        """Bind to a new recording. Any previous session closes first, so two
        recordings can never overlap."""
        self.close()
        with self._lock:
            self._source = source
            self._transcriber = transcriber
            self._committed = ()
            self._prev = ()
            self._window_start_s = 0.0
            self._last_pass_end = time.monotonic()
            self._last_pass_s = 0.0
            self._session = self.channel.begin_session()
            self._closing.clear()
        return self._session

    def close(self, join_timeout: float = 0.0) -> None:
        self._closing.set()
        with self._lock:
            self._source = None
            self._transcriber = None
        if join_timeout and self.is_alive():
            self.join(join_timeout)

    def run(self) -> None:                                   # pragma: no cover
        while not self._closing.is_set():
            with self._lock:
                source, transcriber, session = (
                    self._source, self._transcriber, self._session)
            if source is None:
                break

            now = source.elapsed_s()
            if now - self._window_start_s < self.cfg.min_tail_s:
                time.sleep(0.05)
                continue

            # Duty cycle: never start a pass sooner than the last one took, so
            # the preview owns at most ~half the wall time. This is what keeps a
            # 120 s dictation from doubling the final pass.
            since = time.monotonic() - self._last_pass_end
            if since < max(0.45, self._last_pass_s):
                time.sleep(0.05)
                continue

            pad = max(0.0, self._window_start_s - self.cfg.context_pad_s)
            try:
                audio = source.get_audio(pad)
            except Exception:
                break
            if audio is None or audio.size == 0:
                time.sleep(0.05)
                continue

            self._last_pass_end = time.monotonic()
            words = self._words(transcriber, audio, pad)
            self._last_pass_s = time.monotonic() - self._last_pass_end

            if session != self.channel.session or self._closing.is_set():
                break

            n = stable_prefix(self._prev, words)
            if n:
                self._committed += tuple(words[:n])
                self._window_start_s = words[n - 1].end_s
            self._prev = tuple(words[n:])

            if now - self._window_start_s > self.cfg.max_tail_s:
                # No agreement for max_tail_s: noisy audio or a long utterance.
                # Pay one bounded re-baseline instead of letting the window
                # grow, which is how a preview becomes an O(n^2) preview.
                self._committed = tuple(
                    w for w in self._committed if w.end_s < self._window_start_s)
                self._window_start_s = max(0.0, now - self.cfg.max_tail_s)
                self._prev = ()

            self.channel.publish_preview(PreviewText(
                words=self._committed + self._prev,
                provisional=bool(self._prev),
                dropped_head=bool(self._committed)
                and self._committed[0].start_s > 0.05,
            ))

    def _words(self, transcriber, audio: np.ndarray,
               offset_s: float) -> tuple[WordTiming, ...]:
        # Preview passes are latency-bound and cheap; no initial_prompt, which
        # on a 3 s fragment biases more than it helps.
        stamps = transcriber.transcribe_words(audio, use_prompt=False)
        floor = max(0.0, self._window_start_s - offset_s)
        return tuple(
            WordTiming(w.text, offset_s + w.start_s, offset_s + w.end_s)
            for w in stamps if w.start_s >= floor
        )


# ======================================================================
# surface - the only code that calls gdi32/user32 to draw
# ======================================================================

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
ULW_ALPHA = 0x00000002
LWA_ALPHA = 0x00000002
SRCCOPY = 0x00CC0020
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
HWND_TOPMOST = -1
BI_RGB = 0
DIB_RGB_COLORS = 0
TRANSPARENT_BK = 1
DT_NOPREFIX = 0x0008
DT_WORDBREAK = 0x00000010

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte),
                ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.SetWindowPos.argtypes = (
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.UpdateLayeredWindow.argtypes = (
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
    ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
user32.GetDpiForWindow.restype = wintypes.UINT
user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
user32.GetDC.argtypes = (wintypes.HWND,)
user32.GetDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = (
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD)
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
gdi32.CreateEllipticRgn.restype = ctypes.c_void_p
gdi32.CreateEllipticRgn.argtypes = (
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
user32.SetWindowRgn.argtypes = (wintypes.HWND, ctypes.c_void_p, wintypes.BOOL)
user32.SetWindowRgn.restype = ctypes.c_int
user32.GetWindowRgn.argtypes = (wintypes.HWND, ctypes.c_void_p)
user32.GetWindowRgn.restype = ctypes.c_int
gdi32.CreateFontW.restype = wintypes.HGDIOBJ
gdi32.CreateFontW.argtypes = (
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPCWSTR)
gdi32.SetBkMode.argtypes = (wintypes.HDC, ctypes.c_int)
gdi32.SetTextColor.argtypes = (wintypes.HDC, wintypes.DWORD)
gdi32.TextOutW.argtypes = (
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int)
gdi32.GetTextExtentPoint32W.argtypes = (
    wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p)
gdi32.StretchDIBits.restype = ctypes.c_int
gdi32.StretchDIBits.argtypes = (
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT, wintypes.DWORD)
user32.SetLayeredWindowAttributes.argtypes = (
    wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD)
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = (wintypes.HWND,)
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.InvalidateRect.argtypes = (wintypes.HWND, ctypes.c_void_p, wintypes.BOOL)
user32.InvalidateRect.restype = wintypes.BOOL
user32.DefWindowProcW.argtypes = (wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.FillRect.argtypes = (wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                            wintypes.HBRUSH)
user32.FillRect.restype = wintypes.BOOL
gdi32.CreateSolidBrush.argtypes = (wintypes.DWORD,)
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


class _Dib:
    """A 32bpp top-down DIB plus the memory DC that renders into it."""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.dc = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.dc)
        self.bmi = BITMAPINFO()
        self.bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        self.bmi.bmiHeader.biWidth = w
        self.bmi.bmiHeader.biHeight = -h
        self.bmi.bmiHeader.biPlanes = 1
        self.bmi.bmiHeader.biBitCount = 32
        self.bmi.bmiHeader.biCompression = BI_RGB
        self.bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(
            self.mem, ctypes.byref(self.bmi), DIB_RGB_COLORS,
            ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.mem, self.bmp)

    def view(self) -> np.ndarray:
        buf = (ctypes.c_ubyte * (self.w * self.h * 4)).from_address(
            self.bits.value)
        return np.frombuffer(buf, dtype=np.uint8).reshape(self.h, self.w, 4)

    def dispose(self) -> None:
        try:
            gdi32.SelectObject(self.mem, self.old)
            gdi32.DeleteObject(self.bmp)
            user32.ReleaseDC(None, self.dc)
        except Exception:
            pass


class GdiText:
    """Text rendered through GDI and returned as a premultiplied BGRA tile.

    GDI blends RGB but never writes the alpha channel of a 32bpp DIB, so text
    drawn straight into the overlay would come out fully transparent. Draw it
    white-on-black into a scratch DIB instead, take the glyph coverage from
    the colour channels, and composite it ourselves.
    """

    def __init__(self, px: int) -> None:
        self.font = gdi32.CreateFontW(
            -px, 0, 0, 0, 400,        # height, width, escapement, orient, weight
            0, 0, 0,                  # italic, underline, strikeout
            1,                        # DEFAULT_CHARSET
            0, 0,                     # out/clip precision
            5,                        # CLEARTYPE_QUALITY
            0, "Segoe UI")
        self.px = px
        self._probe = _Dib(8, 8)
        # One scratch surface for the process, grown on demand. Allocating a
        # DIB section per frame cost ~45 ms a frame and tripped the GIL
        # watchdog, which then throttled the overlay to 8 fps to protect the
        # hotkey hook - a self-inflicted stall.
        self._scratch: _Dib | None = None

    def line_height(self) -> int:
        return int(self.px * 1.32)

    def width(self, text: str) -> int:
        gdi32.SelectObject(self._probe.mem, self.font)
        size = wintypes.SIZE()
        gdi32.GetTextExtentPoint32W(self._probe.mem, text, len(text),
                                    ctypes.byref(size))
        return int(size.cx)

    def measure(self) -> TextMeasure:
        outer = self

        class _M:
            def width(self, text: str) -> int:
                return outer.width(text)

            def line_height(self) -> int:
                return outer.line_height()

        return _M()

    def render(self, lines: list[tuple[str, bool]]) -> np.ndarray | None:
        """(line, provisional) pairs -> BGRA tile, or None when there is
        nothing to draw."""
        if not any(t.strip() for t, _ in lines):
            return None
        lh = self.line_height()
        w = max(self.width(t) for t, _ in lines) + 8
        h = lh * len(lines) + 8
        dib = self._scratch_for(max(8, w), max(8, h))

        # Clear: a stale glyph from a longer previous frame would otherwise
        # linger in the tile.
        gdi32.SelectObject(dib.mem, dib.bmp)
        brush = gdi32.CreateSolidBrush(0x00000000)
        rc = wintypes.RECT(0, 0, dib.w, dib.h)
        user32.FillRect(dib.mem, ctypes.byref(rc), brush)
        gdi32.DeleteObject(brush)

        gdi32.SelectObject(dib.mem, self.font)
        gdi32.SetBkMode(dib.mem, TRANSPARENT_BK)
        for i, (text, provisional) in enumerate(lines):
            # Provisional tail is dimmer so a word that later changes
            # reads as still settling rather than as a retraction.
            gdi32.SetTextColor(dib.mem, 0xE8E8F0 if provisional else 0xFFFFFF)
            gdi32.TextOutW(dib.mem, 4, 4 + i * lh, text, len(text))
        raw = dib.view()[0:h, 0:w]
        cover = raw[..., :3].max(axis=2).astype(np.float32) / 255.0
        cover = np.clip(cover * 1.15, 0.0, 1.0)
        out = np.zeros((h, w, 4), dtype=np.uint8)
        col = np.array([235.0, 235.0, 245.0])
        out[..., :3] = (col * cover[..., None]).astype(np.uint8)
        out[..., 3] = (cover * 255).astype(np.uint8)
        return out

    def _scratch_for(self, w: int, h: int) -> _Dib:
        if self._scratch is None or self._scratch.w < w or self._scratch.h < h:
            if self._scratch is not None:
                self._scratch.dispose()
            self._scratch = _Dib(w, h)
        return self._scratch


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
    wintypes.WPARAM, wintypes.LPARAM)

GWLP_WNDPROC = -4
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


user32.BeginPaint.argtypes = (wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = (wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int,
                                     ctypes.c_void_p)
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND,
                                   ctypes.c_uint, wintypes.WPARAM,
                                   wintypes.LPARAM)
user32.CallWindowProcW.restype = ctypes.c_longlong
gdi32.BitBlt.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD)
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT)
gdi32.GetDIBits.restype = ctypes.c_int


class LayeredSurface:
    """A topmost window that cannot take focus, drawn two ways.

    The three ex-styles are load-bearing and not interchangeable:
      NOACTIVATE  - never becomes foreground, so the paste target is safe
      TRANSPARENT - clicks fall through to whatever is underneath, which is
                    the whole point of drawing next to the user's caret
      TOOLWINDOW  - stays out of the taskbar and Alt+Tab

    Per-pixel alpha (`UpdateLayeredWindow`) is what gives the blob a soft
    edge. It is not always available - it fails on some Windows builds and
    graphics stacks, returning FALSE with no error code set - so `open()`
    probes it and falls back to an opaque card. A rounded card is a worse
    picture than a feathered blob, but a visible one beats an invisible one.
    """

    def __init__(self, w: int, h: int, per_pixel: bool = True,
                 circle: bool = True, alpha: float = 1.0) -> None:
        self.w, self.h = w, h
        self.per_pixel = per_pixel
        self.alpha = alpha
        # Both paths are layered now. Per-pixel alpha is the soft-edged route
        # where it is available; where it is not, whole-window alpha still
        # gives translucency, and that is the one API that works reliably here.
        ex = (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT
              | WS_EX_LAYERED)
        self.hwnd = user32.CreateWindowExW(
            ex, "STATIC", "", WS_POPUP, 0, 0, w, h, None, None, None, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.dc = user32.GetDC(self.hwnd)
        self._dib = _Dib(w, h)
        self.visible = False
        self._proc = None
        self._old_proc = None
        self.paint_count = 0
        self.erase_suppressed = 0
        if not per_pixel:
            self._install_painter()
            user32.SetLayeredWindowAttributes(
                self.hwnd, 0, int(max(0.0, min(1.0, alpha)) * 255), LWA_ALPHA)
        if circle:
            self._clip_to_ellipse()

    def _install_painter(self) -> None:
        """Own WM_PAINT and WM_ERASEBKGND.

        Painting into the window DC from the tick loop looks like it works and
        does not: Windows sends WM_ERASEBKGND and WM_PAINT whenever the window
        is shown, moved or uncovered, and the default control-class procedure
        repaints over whatever was there. The window ends up blank or
        flickering because the only thing drawing it was a side effect that
        the paint cycle throws away.
        """
        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_ERASEBKGND:
                self.erase_suppressed += 1
                return 1                      # handled; do not erase
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                dc = user32.BeginPaint(hwnd, ctypes.byref(ps))
                if dc:
                    gdi32.BitBlt(dc, 0, 0, self.w, self.h, self._dib.mem,
                                 0, 0, SRCCOPY)
                    self.paint_count += 1
                user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if self._old_proc:
                return user32.CallWindowProcW(self._old_proc, hwnd, msg,
                                              wparam, lparam)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)
        self._old_proc = user32.SetWindowLongPtrW(
            self.hwnd, GWLP_WNDPROC,
            ctypes.cast(self._proc, ctypes.c_void_p))

    def invalidate(self) -> None:
        """Ask the window to repaint from its DIB."""
        if self.hwnd:
            user32.InvalidateRect(self.hwnd, None, False)

    def _clip_to_ellipse(self) -> bool:
        """Shape the window itself into a circle.

        This is what makes a circle possible without per-pixel alpha: the
        window manager clips the window to an elliptical region, so the corners
        are never composited at all. Where `UpdateLayeredWindow` is available
        the alpha channel would do this instead, and softly; a window region is
        a 1-bit mask, so the edge is hard rather than feathered.
        """
        gdi32.CreateEllipticRgn.restype = ctypes.c_void_p
        gdi32.CreateEllipticRgn.argtypes = (
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
        user32.SetWindowRgn.argtypes = (
            wintypes.HWND, ctypes.c_void_p, wintypes.BOOL)
        user32.SetWindowRgn.restype = ctypes.c_int
        rgn = gdi32.CreateEllipticRgn(0, 0, self.w, self.h)
        if not rgn:
            return False
        try:
            # Zero on success. The system owns the region once it is set.
            user32.SetWindowRgn(self.hwnd, rgn, True)
            self._rgn = rgn
            return True
        except Exception:
            gdi32.DeleteObject(rgn)
            return False

    _rgn = None

    @property
    def region_applied(self) -> bool:
        """Did SetWindowRgn report success?

        Deliberately not cross-checked with GetWindowRgn: on this machine
        GetWindowRgn returns 0 for every region type, rectangle included, so
        it reports nothing rather than reporting wrongly. Trusting the setter's
        own return is the only honest signal available.
        """
        return self._rgn is not None

    @classmethod
    def probe(cls, w: int = 8, h: int = 8) -> bool:
        """Can this machine do per-pixel alpha at all? Ask once, cheaply."""
        try:
            s = cls(w, h, per_pixel=True)
            ok = s.blit(np.full((h, w, 4), 200, dtype=np.uint8), 0, 0)
            s.dispose()
            return ok
        except Exception:
            return False

    def blit(self, frame: np.ndarray, x: int, y: int) -> bool:
        ctypes.memmove(self._dib.bits, frame.tobytes(), frame.nbytes)
        if not self.per_pixel:
            # The DIB is the source of truth; WM_PAINT blits it. Draw once
            # eagerly too so the very first show is not blank.
            gdi32.StretchDIBits(
                self.dc, 0, 0, self.w, self.h, 0, 0, self.w, self.h,
                self._dib.bits, ctypes.byref(self._dib.bmi), DIB_RGB_COLORS,
                SRCCOPY)
            self.invalidate()
            return True
        size = wintypes.SIZE(self.w, self.h)
        dst = wintypes.POINT(x, y)
        src = wintypes.POINT(0, 0)
        return bool(user32.UpdateLayeredWindow(
            self.hwnd, self.dc, ctypes.byref(dst), ctypes.byref(size),
            self._dib.mem, ctypes.byref(src), 0, None, ULW_ALPHA))

    def show(self, x: int, y: int) -> None:
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, self.w, self.h,
                            SWP_SHOWWINDOW | SWP_NOACTIVATE)
        self.visible = True

    def hide(self) -> None:
        if not self.visible:
            return
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_HIDEWINDOW | SWP_NOMOVE | SWP_NOSIZE
                            | SWP_NOACTIVATE)
        self.visible = False

    def dispose(self) -> None:
        self.hide()
        self._dib.dispose()


def choose_origin(focus_hwnd: int, w: int, h: int, cfg: OverlayConfig,
                  dpi: int = 96) -> tuple[int, int]:
    """Where to put the circle.

    Centred on the entire virtual display - every monitor, the full desktop
    rectangle - but resting on the *work area*, so it never covers the taskbar.
    It ignores the activated window completely. Only the "caret" position
    still anchors to a window.
    """
    if cfg.position == "caret" and focus_hwnd and user32.IsWindow(focus_hwnd):
        mon = user32.MonitorFromWindow(focus_hwnd, 2)   # MONITOR_DEFAULTTONEAREST
        work = _work_area(mon)
        if work is not None:
            rect = wintypes.RECT()
            if user32.GetWindowRect(focus_hwnd, ctypes.byref(rect)):
                gap = int(cfg.gap * dpi / 96)
                x = (rect.left + rect.right) // 2 - w // 2
                y = rect.top - h - gap
                if y < work.top:
                    y = min(rect.bottom + gap, work.bottom - h)
                return (max(work.left, min(x, work.right - w)),
                        max(work.top, y))

    # SM_*VIRTUALSCREEN describes every display as one rectangle. On a single
    # monitor this is the whole screen; on several it spans them all. The
    # circle is centred horizontally on that whole display.
    vx = user32.GetSystemMetrics(76)      # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)      # SM_YVIRTUALSCREEN
    vw = user32.GetSystemMetrics(78)      # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)      # SM_CYVIRTUALSCREEN
    if vw <= 0 or vh <= 0:                # metrics unavailable: fall back
        vx = vy = 0
        vw = user32.GetSystemMetrics(0) or 1920      # SM_CXSCREEN
        vh = user32.GetSystemMetrics(1) or 1080      # SM_CYSCREEN

    # Vertically it sits on the work area, not the raw screen, so it never
    # covers the taskbar. The work area of whichever monitor owns the bottom
    # centre of the virtual display, which is right on a multi-monitor setup.
    bottom = vy + vh
    pt = wintypes.POINT(vx + vw // 2, vy + vh - 1)
    work = _work_area(user32.MonitorFromPoint(pt, 2))   # NEAREST
    if work is not None:
        bottom = work.bottom

    return (vx + vw // 2 - w // 2, bottom - h)


def _work_area(mon) -> "wintypes.RECT | None":
    if not mon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return mi.rcWork
    return None


# ======================================================================
# overlay - Tk as clock, Win32 as canvas
# ======================================================================

class Overlay:
    """Owns the window and the tick. Tk is the clock; it is not the canvas.

    Crash containment is a first-class requirement, not a nicety: a paint
    exception must never be able to break dictation, because the overlay is
    decoration and the transcript is the product.
    """

    def __init__(self, cfg: OverlayConfig, channel: OverlayChannel,
                 log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self.channel = channel
        self._log = log
        self.surface: LayeredSurface | None = None
        self.text: GdiText | None = None
        self.envelope = LevelEnvelope()
        self.focus_hwnd = 0
        self._root = None
        self._t0 = time.monotonic()
        self._last = self._t0
        self._settled_at = 0.0
        self._slow_frames = 0
        self._interval = cfg.interval_ms
        self._failed = False
        self.flat = False

    # -- layout ----------------------------------------------------------
    # Everything is a fraction of the circle's diameter, so the whole thing
    # scales from one number. There is no text: the blob is the whole UI.
    @property
    def D(self) -> int:
        return self.cfg.diameter

    def blob_size(self) -> int:
        return int(self.D * 0.78)

    def blob_origin(self) -> tuple[int, int]:
        s = self.blob_size()
        return ((self.D - s) // 2, (self.D - s) // 2)

    def surface_height(self) -> int:
        """The surface is square because the window region is an ellipse."""
        return self.D

    def surface_height(self) -> int:
        """The surface is square because the window region is an ellipse."""
        return self.D

    # -- lifecycle -----------------------------------------------------
    def open(self) -> bool:
        import tkinter as tk
        try:
            self._root = tk.Tk()
            self._root.withdraw()
            self.text = GdiText(14)
            per_pixel = LayeredSurface.probe()
            self.flat = not per_pixel
            self.surface = LayeredSurface(self.D, self.surface_height(),
                                          per_pixel=per_pixel, circle=True,
                                          alpha=self.cfg.alpha)
            note = "" if per_pixel else " (no per-pixel alpha; circle via window region)"
            self._log(f"overlay up ({self.D}px circle, "
                      f"hwnd {int(self.surface.hwnd or 0)}){note}")
            return True
        except Exception as exc:                              # noqa: BLE001
            self._log(f"overlay unavailable ({exc}); continuing without it")
            self._close()
            self._failed = True
            return False

    def close(self) -> None:
        self._close()
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None

    def _close(self) -> None:
        if self.surface is not None:
            try:
                self.surface.dispose()
            except Exception:
                pass
            self.surface = None

    # -- the tick ------------------------------------------------------
    def _frame(self, now: float) -> np.ndarray:
        cfg = self.cfg
        snap = self.channel.snapshot()
        dt = max(1e-3, now - getattr(self, "_last", now))
        self._last = now
        energy = self.envelope.step(snap.level, dt) if snap.phase == "listening" else 0.0

        D = self.D
        canvas = np.zeros((D, D, 4), dtype=np.uint8)

        size = self.blob_size()
        bx, by = self.blob_origin()

        # The blob is the entire UI: it breathes with your voice while
        # listening, spins and turns amber while processing, settles green.
        spin, tint = 0.0, TINTS.get(snap.phase, TINTS["hidden"])
        if snap.phase == "thinking":
            spin = now * 2.6
            energy = max(energy, 0.35)
        elif snap.phase == "delivered":
            energy = max(energy, 0.25)
        elif snap.phase == "discarded":
            energy = 0.05

        _over(canvas, blob_rgba(size, energy, now - self._t0, spin, tint), bx, by)

        if self.flat:
            # No per-pixel alpha: the blob was drawn onto nothing. Composite
            # onto an opaque card. The window region clips this to a circle, so
            # the corners are never shown even though the buffer is rectangular.
            a = canvas[..., 3:4].astype(np.float32) / 255.0
            canvas[..., :3] = np.clip(
                canvas[..., :3].astype(np.float32)
                + np.array([22.0, 22.0, 30.0]) * (1.0 - a), 0, 255
            ).astype(np.uint8)
            canvas[..., 3] = 255
        return canvas

    def _tick(self) -> None:
        if self._root is None or self._failed:
            return
        t0 = time.monotonic()
        try:
            snap = self.channel.snapshot()
            if snap.phase == "hidden":
                if self.surface.visible:
                    self.surface.hide()
            else:
                now = time.monotonic()
                canvas = self._frame(now)
                x, y = choose_origin(self.focus_hwnd, canvas.shape[1],
                                     canvas.shape[0], self.cfg)
                self.surface.blit(canvas, x, y)
                self.surface.show(x, y)
                if snap.phase in ("delivered", "discarded"):
                    if not self._settled_at:
                        self._settled_at = now
                    elif (now - self._settled_at) * 1000 > self.cfg.delivered_ms:
                        self.channel.publish_state("hidden")
                        self._settled_at = 0.0
                else:
                    self._settled_at = 0.0
        except Exception as exc:                              # noqa: BLE001
            self._log(f"overlay paint failed ({exc}); disabling overlay")
            self._failed = True
            self._close()
            return

        # GIL watchdog: the WH_KEYBOARD_LL hook must keep getting the GIL.
        cost = time.monotonic() - t0
        if cost > FRAME_BUDGET_S:
            self._slow_frames += 1
            if self.cfg.fps_watchdog and self._slow_frames > 30:
                self._interval = max(33, self._interval * 2)
                self._slow_frames = 0
                self._log(f"overlay frame {cost * 1000:.0f}ms - "
                          f"slowing to {self._interval}ms to protect the "
                          f"hotkey hook")
        else:
            self._slow_frames = max(0, self._slow_frames - 1)

        if self._root is not None:
            self._root.after(self._interval, self._tick)

    def serve(self) -> None:
        """Block on the Tk loop. This is the whole contract."""
        if self._failed or self._root is None:
            while True:
                time.sleep(0.3)
        self._root.after(10, self._tick)
        self._root.mainloop()


# ======================================================================
# Feedback - the facade Dictation talks to
# ======================================================================

class Feedback:
    """What the host calls. Additive: a headless instance makes every method a
    no-op, which is exactly the behaviour dictate.py had before the overlay.

    The ordering rule that matters: `capture_ended()` must be called BEFORE
    the recorder is stopped. It (a) stops the preview worker from reading a
    recorder that is being torn down and (b) switches the blob to thinking.
    Only then is the buffer the recorder returns final and complete.
    """

    def __init__(self, cfg: OverlayConfig, log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self._log = log
        self.channel = OverlayChannel()
        self.preview = PreviewWorker(cfg, self.channel, log)
        self.overlay = Overlay(cfg, self.channel, log)
        self._live = False

    def open(self) -> bool:
        self._live = self.overlay.open()
        if self._live:
            self.preview.start()
        return self._live

    def serve(self) -> None:
        self.overlay.serve()

    def close(self) -> None:
        self.preview.close(0.25)
        self.overlay.close()

    def levels(self) -> LevelSink:
        return LevelSink()

    def set_focus_window(self, hwnd: int) -> None:
        self.overlay.focus_hwnd = hwnd

    def dictation_started(self, recorder, transcriber) -> None:
        self.channel.attach_levels(recorder.levels)
        if self.cfg.overlay_preview:
            self.preview.open(recorder, transcriber)
        else:
            # The preview is off by default. Running it anyway would be pure
            # waste: it re-transcribes the recording on a timer and holds the
            # model lock, which directly delays the final pass that actually
            # gets pasted.
            self.channel.begin_session()

    def capture_ended(self) -> None:
        # Non-blocking on purpose: a pass already in flight cannot be cancelled,
        # and correctness does not depend on this returning before the final
        # pass starts - only the freshness of the last preview does.
        if self.cfg.overlay_preview:
            self.preview.close(0.0)
        self.channel.publish_state("thinking")

    def dictation_delivered(self, text: str) -> None:
        self.channel.close_session("delivered")

    def dictation_discarded(self, reason: str) -> None:
        self.channel.close_session("discarded", reason)


def _text_to_words(text: str) -> tuple[WordTiming, ...]:
    """Flat timings for a final transcript. The preview renderer only ever
    sorts and wraps, so exact offsets carry no meaning here."""
    out, t = [], 0.0
    for w in text.split():
        out.append(WordTiming(w, t, t + 0.2))
        t += 0.2
    return tuple(out)


class NullFeedback:
    """Every method is a no-op. Keeps headless runs and `modetest` unchanged."""

    def levels(self) -> LevelSink:
        return LevelSink()

    def set_focus_window(self, hwnd: int) -> None: ...
    def dictation_started(self, recorder, transcriber) -> None: ...
    def capture_ended(self) -> None: ...
    def dictation_delivered(self, text: str) -> None: ...
    def dictation_discarded(self, reason: str) -> None: ...

    def open(self) -> bool:
        return False

    def serve(self) -> None:
        while True:
            time.sleep(0.3)

    def close(self) -> None: ...


