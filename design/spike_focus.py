"""Phase D1 spike: can a layered NOACTIVATE overlay be shown without stealing
focus, and does restore_focus still work afterwards?

This is the one assumption that would break the *paste* rather than the
picture. Everything else in the design is downstream of these assertions.

Run:  python spike_focus.py
Exit: 0 = safe to build on, 1 = the design is wrong, scrap and rethink.
"""
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
ULW_ALPHA = 0x00000002
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
HWND_TOPMOST = -1
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
AC_SRC_OVER = 0x00

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte),
                ("AlphaFormat", ctypes.c_byte)]


user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.SetWindowPos.argtypes = (
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.UpdateLayeredWindow.argtypes = (
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
gdi32.CreateDIBSection.restype = wintypes.HBITMAP


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


W, H = 220, 220

# 64-bit-safe signatures; without these ctypes truncates handles to 32 bits.
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


def blob_frame(energy: float, phase: float) -> np.ndarray:
    """Premultiplied BGRA for a soft-edged radial blob. Pure, numpy only."""
    y, x = np.mgrid[0:H, 0:W]
    cx = cy = W / 2.0
    r = np.hypot(x - cx, y - cy)
    base = 52 + 46 * energy
    ring = 1.6 * np.sin(6 * np.arctan2(y - cy, x - cx) + phase)
    edge = base + ring
    alpha = np.clip((edge - r) / 6.0, 0.0, 1.0)
    alpha = alpha * alpha * (3 - 2 * alpha)          # smoothstep
    alpha *= 0.94
    rgb = np.stack([
        np.full_like(alpha, 120 + 100 * energy),
        np.full_like(alpha, 90),
        np.full_like(alpha, 255),
    ], axis=-1)
    out = np.empty((H, W, 4), dtype=np.uint8)
    out[..., :3] = (rgb * alpha[..., None]).astype(np.uint8)   # premultiply
    out[..., 3] = (alpha * 255).astype(np.uint8)
    return out


class LayeredBlob:
    def __init__(self) -> None:
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
            "STATIC", "", WS_POPUP, 0, 0, W, H, None, None, None, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.dc = user32.GetDC(self.hwnd)
        self.mem_dc = gdi32.CreateCompatibleDC(self.dc)
        self.bmi = BITMAPINFO()
        self.bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        self.bmi.bmiHeader.biWidth = W
        self.bmi.bmiHeader.biHeight = -H          # top-down
        self.bmi.bmiHeader.biPlanes = 1
        self.bmi.bmiHeader.biBitCount = 32
        self.bmi.bmiHeader.biCompression = BI_RGB
        self.bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(self.bmi),
                                         DIB_RGB_COLORS, ctypes.byref(self.bits),
                                         None, 0)
        if not self.bmp:
            raise ctypes.WinError(ctypes.get_last_error())
        gdi32.SelectObject(self.mem_dc, self.bmp)
        self.visible = False

    def blit(self, energy: float, phase: float) -> None:
        frame = blob_frame(energy, phase)
        ctypes.memmove(self.bits, frame.tobytes(), frame.nbytes)
        size = wintypes.SIZE(W, H)
        pos = wintypes.POINT(200, 160)
        src = wintypes.POINT(0, 0)
        user32.UpdateLayeredWindow(self.hwnd, self.dc, ctypes.byref(pos),
                                   ctypes.byref(size), self.mem_dc,
                                   ctypes.byref(src), 0, None, ULW_ALPHA)

    def show(self) -> None:
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 200, 160, W, H,
                            SWP_SHOWWINDOW | SWP_NOACTIVATE)
        self.visible = True

    def hide(self) -> None:
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_HIDEWINDOW | SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.visible = False


def main() -> int:
    import tkinter as tk
    sys.path.insert(0, ROOT)
    from dictate import restore_focus  # the real function under test

    root = tk.Tk()
    root.title("spike target")
    root.geometry("640x300+120+120")
    box = tk.Text(root, font=("Segoe UI", 14))
    box.pack(fill="both", expand=True)
    root.update()

    target = int(root.winfo_id())
    ok_focus = restore_focus(target)
    for _ in range(10):
        root.update()
        time.sleep(0.02)
    baseline = int(user32.GetForegroundWindow() or 0)
    print(f"restore_focus(target)={ok_focus}  foreground={baseline}")

    blob = LayeredBlob()
    failures = []
    samples = 0
    energy = 0.0
    t0 = time.monotonic()
    try:
        for i in range(200):
            energy = 0.5 + 0.5 * (0.5 + 0.5 * __import__("math").sin(i / 6.0))
            blob.blit(energy, i / 5.0)
            if i % 4 < 2:
                blob.show()
            else:
                blob.hide()
            root.update()                    # pump the "user app"
            fg = int(user32.GetForegroundWindow() or 0)
            samples += 1
            if fg not in (baseline, target):
                failures.append((i, fg))
            if fg != baseline:
                failures.append((i, fg))
            time.sleep(0.008)
    finally:
        blob.hide()
        elapsed = time.monotonic() - t0

    root.update()
    still_ok = int(user32.GetForegroundWindow() or 0) == baseline
    refocus = restore_focus(target)
    for _ in range(10):
        root.update()
        time.sleep(0.02)
    final_fg = int(user32.GetForegroundWindow() or 0)

    print(f"cycles=200 samples={samples} in {elapsed:.1f}s "
          f"({samples / elapsed:.0f} show+blit/s)")
    print(f"foreground never changed during overlay: {not failures}")
    if failures:
        print(f"  first 5 steals: {failures[:5]}")
    print(f"foreground intact after hide: {still_ok}")
    print(f"restore_focus still works afterwards: {refocus} (fg={final_fg})")

    verdict = (not failures) and still_ok and (final_fg == baseline)
    print("\nVERDICT:", "SAFE - build the overlay" if verdict
          else "UNSAFE - the layered NOACTIVATE premise is wrong")
    root.destroy()
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
