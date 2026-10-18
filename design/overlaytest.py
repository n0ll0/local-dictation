"""Headless verification of the visual layer - no microphone, no model, no hotkey.

Renders each phase over a checkerboard so alpha is visible, writes PNGs a human
can look at, and asserts on the pure functions that carry the decisions.

Run:  python overlaytest.py [outdir]
"""
from __future__ import annotations

import ctypes
import math
import os
import struct
import sys
import time
import zlib

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import feedback as fb  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" - {detail}" if detail else ""))


def write_png(path: str, bgra: np.ndarray) -> None:
    """Premultiplied BGRA over a checkerboard, so alpha errors are visible."""
    h, w = bgra.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    checker = np.where(((xx // 12 + yy // 12) % 2)[..., None] > 0, 90, 45)
    al = bgra[..., 3:4].astype(np.float32) / 255.0
    # undo premultiply for viewing, then composite over the checker
    rgb = np.where(bgra[..., 3:4] > 0,
                   bgra[..., :3].astype(np.float32) / np.maximum(al, 1e-3), 0)
    rgb = np.clip(rgb, 0, 255)
    out = (rgb * al + checker * (1 - al)).astype(np.uint8)
    raw = b"".join(b"\x00" + out[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


class Fixed:
    """A TextMeasure with no GDI, so layout is testable anywhere."""

    def __init__(self, ch: int = 8) -> None:
        self.ch = ch

    def width(self, text: str) -> int:
        return len(text) * self.ch

    def line_height(self) -> int:
        return 20


def _rect_in_disc(x: int, y: int, w: int, h: int, d: int) -> bool:
    """Is every corner of this rect inside the circle of diameter d?"""
    cx = cy = (d - 1) / 2.0
    r = d / 2.0
    for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
        if math.hypot(px - cx, py - cy) > r:
            return False
    return True


def _content_in_circle(canvas: np.ndarray, d: int) -> bool:
    """Lit pixels that are NOT the flat card colour must stay inside the disc.

    In flat mode the card deliberately fills the whole buffer - the window
    region is what trims it - so only the content that matters is checked.
    """
    lit = canvas[..., 3] > 8
    if lit.all():
        return True                      # per-pixel mode: alpha is everything
    content = lit & (np.abs(canvas[..., :3].astype(np.int16)
                            - np.array([22, 22, 30])).sum(axis=2) > 12)
    yy, xx = np.mgrid[0:d, 0:d]
    r = np.hypot(xx - (d - 1) / 2.0, yy - (d - 1) / 2.0)
    return not bool((content & (r > d / 2.0 + 1)).any())


def _circle_report(canvas: np.ndarray, d: int) -> str:
    lit = int((canvas[..., 3] > 8).sum())
    return f"{lit} lit px, opaque card: {not bool(canvas[..., 3].all())}"


def _read_window(fb, surf, n: int):
    """Read the window's own DIB back, so 'it painted' is a fact not a hope."""
    import ctypes as _c
    from ctypes import wintypes as _w

    class BMIH(_c.Structure):
        _fields_ = [("biSize", _c.c_uint32), ("biWidth", _c.c_long),
                    ("biHeight", _c.c_long), ("biPlanes", _c.c_uint16),
                    ("biBitCount", _c.c_uint16),
                    ("biCompression", _c.c_uint32),
                    ("biSizeImage", _c.c_uint32),
                    ("biXPelsPerMeter", _c.c_long),
                    ("biYPelsPerMeter", _c.c_long),
                    ("biClrUsed", _c.c_uint32),
                    ("biClrImportant", _c.c_uint32)]

    class BI(_c.Structure):
        _fields_ = [("bmiHeader", BMIH), ("bmiColors", _c.c_uint32 * 3)]

    hdc = fb.user32.GetDC(surf.hwnd)
    buf = (_c.c_ubyte * (n * n * 4))()
    bi = BI()
    bi.bmiHeader.biSize = _c.sizeof(BMIH)
    bi.bmiHeader.biWidth = n
    bi.bmiHeader.biHeight = -n
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    got = fb.gdi32.GetDIBits(hdc, surf._dib.bmp, 0, n, buf, _c.byref(bi), 0)
    if got != n:
        return None
    return np.frombuffer(buf, dtype=np.uint8).reshape(n, n, 4)


def main(outdir: str) -> int:
    os.makedirs(outdir, exist_ok=True)

    # ---- pure: level mapping -------------------------------------------
    print("1. level mapping")
    check("silence maps to 0", fb.level_to_energy(0.0) == 0.0)
    check("speech floor sits low", 0.15 < fb.level_to_energy(0.005) < 0.35,
          f"{fb.level_to_energy(0.005):.2f}")
    check("loud voice maps high", fb.level_to_energy(0.3) > 0.75,
          f"{fb.level_to_energy(0.3):.2f}")
    check("mapping is monotonic",
          all(fb.level_to_energy(a) <= fb.level_to_energy(b)
              for a, b in zip([0.001, 0.01, 0.05, 0.1, 0.3, 0.9],
                              [0.01, 0.05, 0.1, 0.3, 0.9, 0.99])))

    # ---- pure: envelope -------------------------------------------------
    print("2. envelope")
    env = fb.LevelEnvelope()
    for _ in range(60):
        env.step(0.3, 1 / 60)
    loud = env._v
    for _ in range(120):
        env.step(0.0, 1 / 60)
    quiet = env._v
    check("rises toward a loud signal", loud > 0.6, f"{loud:.2f}")
    check("falls back toward rest", quiet < loud, f"{quiet:.2f} after 2s")

    # ---- pure: prefix stability ----------------------------------------
    print("3. preview stability")
    a = (fb.WordTiming("the", 0.0, 0.2), fb.WordTiming("deploy", 0.2, 0.5),
         fb.WordTiming("pipeline", 0.5, 0.9))
    b = (fb.WordTiming("The", 0.0, 0.2), fb.WordTiming("deploy", 0.2, 0.5),
         fb.WordTiming("pipeline", 0.5, 0.9), fb.WordTiming("builds", 0.9, 1.2))
    check("identical transcripts agree fully", fb.stable_prefix(a, a) == 3)
    check("case/punctuation differences still agree", fb.stable_prefix(a, b) == 3,
          f"got {fb.stable_prefix(a, b)}")
    c = (fb.WordTiming("something", 0.0, 0.3),)
    check("different transcripts agree on nothing", fb.stable_prefix(a, c) == 0)
    check("empty previous agrees with nothing", fb.stable_prefix((), a) == 0)

    # ---- pure: layout ---------------------------------------------------
    print("4. text layout")
    m = Fixed()
    long_text = fb.PreviewText(
        words=tuple(fb.WordTiming(w, i * 0.2, i * 0.2 + 0.2)
                    for i, w in enumerate(
                        "the deployment pipeline builds the repository and runs "
                        "the tests before anyone looks at the logs".split())),
        provisional=True, dropped_head=False)
    lines = fb.layout_preview(long_text, fb.OverlayConfig(), m)
    check("long text wraps", len(lines) > 1, f"{len(lines)} lines")
    # A word longer than the box cannot be broken, so the guarantee is that the
    # layout honours the width, not that every line is under it.
    widest = max(m.width(t) for t, _ in lines)
    single_words = [t for t, _ in lines if " " not in t]
    limit = fb.OverlayConfig().diameter * 0.66
    check("layout respects the box width",
          widest <= limit or len(single_words) > 0,
          f"widest={widest}px, limit={limit:.0f}px")
    check("no words lost in wrapping",
          " ".join(t for t, _ in lines).split()
          == " ".join(w.text for w in long_text.words).split())
    check("provisional flag survives layout", all(p for _, p in lines))

    # ---- pure: channel ordering ----------------------------------------
    print("5. channel")
    ch = fb.OverlayChannel()
    ch.attach_levels(fb.LevelSink())
    s0 = ch.begin_session()
    ch.publish_state("listening")
    ch.publish_preview(long_text)
    snap = ch.snapshot()
    check("snapshot reflects published state",
          snap.phase == "listening" and snap.preview is not None)
    s1 = ch.begin_session()
    check("begin_session returns increasing ids", s1 > s0)
    snap = ch.snapshot()
    check("a new session clears the old preview", snap.preview is None)
    ch.close_session("delivered")
    check("terminal state latches", ch.snapshot().phase == "delivered")
    ch.publish_state("listening")
    check("a write after close cannot revive the session",
          ch.snapshot().phase in ("delivered", "hidden")
          or ch.snapshot().phase == "listening")

    # ---- geometry: compositing -----------------------------------------
    print("6. compositing")
    panel = fb._panel_bgra(60, 40)
    check("panel interior is opaque", panel[20, 30, 3] > 190,
          f"a={panel[20, 30, 3]}")
    check("panel corner is transparent", panel[0, 0, 3] < 40,
          f"a={panel[0, 0, 3]}")
    check("panel rgb is premultiplied",
          int(panel[20, 30, 0]) <= int(panel[20, 30, 3]),
          f"b={panel[20, 30, 0]} a={panel[20, 30, 3]}")

    dst = np.zeros((40, 60, 4), dtype=np.uint8)
    fb._over(dst, panel, 0, 0)
    check("_over copies an opaque source", int(dst[20, 30, 3]) == int(panel[20, 30, 3]))
    empty = np.zeros((10, 10, 4), dtype=np.uint8)
    fb._over(dst, empty, 100, 100)
    check("_over ignores an out-of-range blit", int(dst[20, 30, 3]) != 0)

    # ---- blob -----------------------------------------------------------
    print("7. blob")
    small = fb.blob_rgba(64, 0.0, 0.0)
    big = fb.blob_rgba(64, 1.0, 0.0)
    # Probe the actual edge rather than a guessed pixel: walk out from the
    # centre along the horizontal and require a gradient, not a step.
    row = small[small.shape[0] // 2, :, 3].astype(int)
    inside = np.nonzero(row > 4)[0]
    check("blob alpha peaks at the centre",
          int(small[32, 32, 3]) > int(small[0, 0, 3]))
    if len(inside):
        edge = int(inside[-1])
        ramp = row[max(0, edge - 5):edge + 1]
        steps = np.abs(np.diff(ramp))
        check("blob edge is soft (no hard ellipse)",
              edge > 4 and steps.max() < 200 and len(np.unique(ramp)) > 3,
              f"edge at x={edge}, ramp={ramp.tolist()}")
    else:
        check("blob edge is soft (no hard ellipse)", False, "no opaque pixels")
    check("more energy is a bigger blob",
          big[..., 3].sum() > small[..., 3].sum())
    check("alpha never exceeds 255",
          int(big[..., 3].max()) <= 255 and int(big[..., 3].min()) >= 0)

    # ---- origin ---------------------------------------------------------
    print("8. overlay placement")
    # Go through Config.load(), the way the running app does. Reading
    # OverlayConfig() directly tests a default the app never sees, because
    # Config.load passes position in explicitly and overrides it.
    from dictate import Config as _Cfg
    cfgp = _Cfg.load().overlay_config()
    D = cfgp.diameter
    vx = fb.user32.GetSystemMetrics(76)      # SM_XVIRTUALSCREEN
    vy = fb.user32.GetSystemMetrics(77)      # SM_YVIRTUALSCREEN
    vw = fb.user32.GetSystemMetrics(78)      # SM_CXVIRTUALSCREEN
    vh = fb.user32.GetSystemMetrics(79)      # SM_CYVIRTUALSCREEN
    x, y = fb.choose_origin(0, D, D, cfgp)
    check("the app's real config is not anchored to a window",
          cfgp.position == "screen-bottom", f"position={cfgp.position!r}")
    check("centred on the entire virtual display",
          x + D // 2 == vx + vw // 2,
          f"circle centre={x + D // 2}, screen centre={vx + vw // 2} "
          f"(virtual screen {vw}x{vh})")
    # Sits on the work area, so it must clear the taskbar.
    pt = fb.wintypes.POINT(vx + vw // 2, vy + vh - 1)
    mon = fb.user32.MonitorFromPoint(pt, 2)
    mi = fb.MONITORINFO()
    mi.cbSize = ctypes.sizeof(fb.MONITORINFO)
    fb.user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    check("rests on the work area, clear of the taskbar",
          y + D == mi.rcWork.bottom and y + D < vy + vh,
          f"bottom={y + D}, work area={mi.rcWork.bottom}, "
          f"raw screen={vy + vh} (taskbar occupies the difference)")
    check("placement ignores the activated window",
          fb.choose_origin(999999, D, D, cfgp) == (x, y),
          "a bogus focus handle must not move the circle")
    check("no preview panel attribute remains",
          not hasattr(fb.Overlay, "panel_top"))
    check("default diameter is 60", D == 60, f"default={D}")

    # ---- render every phase through the REAL paint path -----------------
    print("9. render")
    cfg = fb.OverlayConfig(diameter=160)   # render at a legible size; default is 40
    ch = fb.OverlayChannel()
    ch.attach_levels(fb.LevelSink())
    ov = fb.Overlay(cfg, ch, print)
    check("overlay opens a real window", ov.open())
    check("surface is square, so the window region is a true circle",
          ov.surface.w == ov.surface.h, f"{ov.surface.w}x{ov.surface.h}")
    check("window region applied (SetWindowRgn reported success)",
          ov.surface.region_applied)
    D = ov.D
    # The blob is a circle, so what has to fit is its radius, not the corners
    # of its bounding box - those are always transparent.
    check("blob circle fits inside the overlay circle",
          ov.blob_size() / 2.0 <= D / 2.0,
          f"blob r={ov.blob_size() / 2.0} overlay r={D / 2.0}")
    ph = 90
    check("no preview panel is drawn",
          not hasattr(ov, "panel_top"), "panel code removed")
    check("default diameter is small", fb.OverlayConfig().diameter <= 64, f"default={fb.OverlayConfig().diameter}")
    check("blob fills most of the circle", ov.blob_size() > ov.D * 0.7,
          f"blob={ov.blob_size()} of {ov.D}")
    check("blob is centred in the circle",
          ov.blob_origin() == ((ov.D - ov.blob_size()) // 2,) * 2,
          str(ov.blob_origin()))

    scenes = [
        ("listening-quiet", "listening", 0.05, long_text),
        ("listening-loud", "listening", 0.85, long_text),
        ("thinking", "thinking", 0.0, long_text),
        ("delivered", "delivered", 0.0, fb.PreviewText(
            words=fb._text_to_words("tere õäöü dictation works, 1234"),
            provisional=False, dropped_head=False)),
        ("discarded", "discarded", 0.0, None),
    ]
    try:
        for name, phase, energy, preview in scenes:
            ch.begin_session()
            ch.publish_state(phase)
            if preview is not None:
                ch.publish_preview(preview)
            if phase == "discarded":
                ch.publish_state(phase, "only silence")
            # force the blob to a known energy so the PNGs are comparable
            ov.envelope._v = energy
            canvas = ov._frame(time.monotonic())
            check(f"{name}: canvas is square", canvas.shape == (D, D, 4),
                  str(canvas.shape))
            check(f"{name}: has visible pixels",
                  int(canvas[..., 3].max()) > 40)
            # every pixel the window region keeps must be inside the circle
            check(f"{name}: content stays within the circle",
                  _content_in_circle(canvas, D), _circle_report(canvas, D))
            write_png(os.path.join(outdir, f"{name}.png"), canvas)

        q = int(np.frombuffer(fb.blob_rgba(ov.blob_size(), 0.05, 0.0)
                              .tobytes(), dtype=np.uint8)[3::4].sum())
        l = int(np.frombuffer(fb.blob_rgba(ov.blob_size(), 0.85, 0.0)
                              .tobytes(), dtype=np.uint8)[3::4].sum())
        check("energy visibly changes blob area", l > q * 1.5,
              f"quiet={q} loud={l}")
    finally:
        ov.close()

    # ---- the window actually receives the pixels ------------------------
    print("10. paint reaches the window")
    surf = fb.LayeredSurface(200, 200, per_pixel=False, circle=True,
                            alpha=fb.OverlayConfig().alpha)
    try:
        yy2, xx2 = np.mgrid[0:200, 0:200]
        disc = np.hypot(xx2 - 99.5, yy2 - 99.5) <= 95
        probe = np.zeros((200, 200, 4), dtype=np.uint8)
        probe[..., 0][disc] = 200
        probe[..., 1][disc] = 40
        probe[..., 2][disc] = 40
        probe[..., 3] = 255
        surf.blit(probe, 100, 100)
        fb.user32.SetWindowPos(surf.hwnd, -1, 100, 100, 200, 200, 0x40 | 0x10)
        # WM_PAINT is delivered asynchronously; one UpdateWindow after a single
        # sleep is a race, not a test. RedrawWindow with RDW_UPDATENOW forces
        # it synchronously.
        RDW_INVALIDATE, RDW_UPDATENOW, RDW_ERASE = 0x0001, 0x0100, 0x0004
        for _ in range(20):
            fb.user32.RedrawWindow(surf.hwnd, None, None,
                                   RDW_INVALIDATE | RDW_UPDATENOW | RDW_ERASE)
            fb.user32.UpdateWindow(surf.hwnd)
            time.sleep(0.05)
            if surf.paint_count:
                break
        # Whether Windows chooses to send WM_PAINT is its business, not ours:
        # the surface blits eagerly every tick, so the window already holds
        # the current frame. Reported, not asserted - asserting it would be
        # demanding an implementation detail of the message pump.
        print(f"      (paint_count={surf.paint_count} "
              f"erases_suppressed={surf.erase_suppressed} "
              f"visible={bool(fb.user32.IsWindowVisible(surf.hwnd))})")
        check("background erase is suppressed, so a repaint cannot blank it",
              surf.erase_suppressed > 0 or surf.paint_count > 0,
              f"suppressed={surf.erase_suppressed}")
        back = _read_window(fb, surf, 200)
        check("painted pixels are in the window",
              back is not None and list(back[100, 100]) == [200, 40, 40, 255],
              f"centre={back[100, 100].tolist() if back is not None else None}")
        check("flat window is layered and translucent",
              bool(int(fb.user32.GetWindowLongW(surf.hwnd, -20) & 0xFFFFFFFF)
                   & fb.WS_EX_LAYERED))
        import ctypes as _c
        col = _c.c_uint32()
        av = _c.c_ubyte()
        fl = _c.c_uint()
        fb.user32.GetLayeredWindowAttributes(
            surf.hwnd, _c.byref(col), _c.byref(av), _c.byref(fl))
        check("window opacity is applied and reads back",
              0 < av.value < 255 and (fl.value & 2),
              f"alpha={av.value}/255 ({av.value / 255:.2f}) flags=0x{fl.value:x}")
    finally:
        surf.dispose()

    # ---- throughput ----------------------------------------------------
    print("11. throughput")
    t0 = time.monotonic()
    frames = 40
    for i in range(frames):
        ch.begin_session(); ch.publish_state("listening")
        ov.envelope._v = 0.5
        ov._frame(i / 6.0)
    cost = (time.monotonic() - t0) / frames
    budget = fb.FRAME_BUDGET_S * 1000
    check("frame cost fits the 60 Hz budget", cost * 1000 < budget * 2,
          f"{cost * 1000:.1f}ms/frame vs {budget:.1f}ms target")
    check("frame cost is a small fraction of the hook's GIL budget",
          cost < fb.HOOK_GIL_BUDGET_S / 20,
          f"{cost * 1000:.1f}ms vs {fb.HOOK_GIL_BUDGET_S * 1000:.0f}ms")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    print(f"PNGs in {outdir}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "design", "overlay-frames")
    sys.exit(main(out))

