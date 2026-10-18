"""End-to-end: the real app, the real hotkey, the real overlay window.

Everything except the voice. Launches dictate.py, presses ctrl+alt+t with
SendInput so the app's own RegisterHotKey path fires, and watches the
WS_EX_LAYERED | WS_EX_NOACTIVATE window the app reports for itself.

Window discovery reads the hwnd the app logs rather than scanning with
EnumWindows: several unrelated apps own layered NOACTIVATE windows too, and a
scan attributes them to the wrong process.

Run:  python e2e_overlay.py
"""
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ROOT)
from dictate import send_keys, VK_CONTROL, log  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
VK_MENU, VK_T = 0x12, 0x54
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
GWL_EXSTYLE = -20

user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongW.restype = ctypes.c_long

PY = ROOT + r'\.venv\Scripts\python.exe'
APP = ROOT + r'\dictate.py'
LOG = ROOT + r'\_e2e.log'

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" - {detail}" if detail else ""))


def read_log() -> str:
    try:
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def press() -> None:
    send_keys(VK_CONTROL, VK_MENU, VK_T)


def chord_down() -> None:
    """Modifiers only, left held. send_keys() presses and releases in one
    call, which the app correctly reads as a tap - a real push-to-talk needs
    the modifiers to stay down across the tap_ms settle window."""
    for vk in (VK_CONTROL, VK_MENU):
        _key(vk, down=True)


def chord_up() -> None:
    for vk in (VK_T, VK_MENU, VK_CONTROL):
        _key(vk, down=False)


def _key(vk: int, down: bool) -> None:
    import ctypes as _c
    from dictate import INPUT, _INPUTUNION, KEYBDINPUT, KEYEVENTF_KEYUP, \
        INPUT_KEYBOARD, user32
    ev = INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(
        wVk=vk, wScan=0, dwFlags=0 if down else KEYEVENTF_KEYUP,
        time=0, dwExtraInfo=None)))
    user32.SendInput(1, _c.byref(ev), _c.sizeof(INPUT))


def wait_until(pred, timeout: float, step: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def main() -> int:
    # Pre-flight: this test needs to own ctrl+alt+t. A running instance would
    # silently turn every later check into a false failure, so say so plainly.
    if subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*dictate.py*' }).Count"],
            capture_output=True, text=True).stdout.strip() not in ("", "0"):
        print("REFUSING TO RUN: a dictate.py instance is already running and "
              "owns ctrl+alt+t.\nStop it first "
              "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -like '*dictate.py*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }).")
        return 2
    if os.path.exists(LOG):
        os.remove(LOG)
    env = dict(os.environ, HF_HOME=ROOT + r'\huggingface')
    proc = subprocess.Popen([PY, APP], stdout=open(LOG, "w"),
                            stderr=subprocess.STDOUT, env=env)
    hwnd = 0
    try:
        print("1. startup")
        check("app started and loaded its model",
              wait_until(lambda: "model ready" in read_log(), 180))
        m = re.search(r"hwnd (\d+)", read_log())
        check("overlay window was created", bool(m), m.group(0) if m else "no hwnd")
        hwnd = int(m.group(1)) if m else 0
        if not hwnd:
            return 1
        check("handle is a real window", bool(user32.IsWindow(hwnd)))
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        check("WS_EX_NOACTIVATE set", bool(style & WS_EX_NOACTIVATE),
              f"0x{style:08x}")
        check("WS_EX_TOOLWINDOW set (stays out of the taskbar)",
              bool(style & WS_EX_TOOLWINDOW), f"0x{style:08x}")
        check("WS_EX_TRANSPARENT set (clicks fall through)",
              bool(style & WS_EX_TRANSPARENT), f"0x{style:08x}")
        # Layered is only correct when per-pixel alpha is in use; on the flat
        # path the window is deliberately opaque and shaped by SetWindowRgn.
        log(f"exstyle 0x{style:08x} layered={bool(style & WS_EX_LAYERED)}")
        check("overlay is hidden while idle",
              not user32.IsWindowVisible(hwnd))

        print("2. tap the hotkey -> record until pressed again")
        press()
        check("overlay became visible",
              wait_until(lambda: bool(user32.IsWindowVisible(hwnd)), 20))
        check("app is recording",
              wait_until(lambda: "recording (max" in read_log(), 10))
        time.sleep(1.5)
        check("overlay stays visible for the whole recording",
              bool(user32.IsWindowVisible(hwnd)))

        print("3. press again -> transcribe and finish")
        before = read_log()
        press()
        check("a transcription pass ran",
              wait_until(lambda: ("transcribed" in read_log()[len(before):])
                         or ("typed:" in read_log()[len(before):])
                         or ("only silence" in read_log()[len(before):]), 120),
              _tail())
        check("app survived the round trip", proc.poll() is None)
        check("overlay retires itself after the result",
              wait_until(lambda: not user32.IsWindowVisible(hwnd), 15))

        print("4. push-to-talk: hold the chord, then release")
        before = len(read_log())
        chord_down()
        time.sleep(0.2)
        _key(VK_T, down=True)
        time.sleep(0.15)
        _key(VK_T, down=False)
        visible_while_held = wait_until(
            lambda: bool(user32.IsWindowVisible(hwnd)), 15)
        time.sleep(0.6)          # past tap_ms, so it settles into 'hold'
        chord_up()
        check("push-to-talk shows the overlay while held", visible_while_held)
        check("push-to-talk stops on release",
              wait_until(lambda: "key released" in read_log()[before:], 20),
              _tail(3))
        check("push-to-talk ran a transcription pass",
              wait_until(lambda: ("transcribed" in read_log()[before:])
                         or ("only silence" in read_log()[before:]), 120),
              _tail(3))
        check("app still healthy after two round trips", proc.poll() is None)

        print("5. cancel path")
        press()
        wait_until(lambda: bool(user32.IsWindowVisible(hwnd)), 15)
        before = len(read_log())
        send_keys(VK_CONTROL, VK_MENU, 0x58)      # ctrl+alt+x
        check("cancel discards without typing",
              wait_until(lambda: "discarded" in read_log()[before:], 20)
              and "typed:" not in read_log()[before:], _tail(3))
        check("overlay retires after cancel",
              wait_until(lambda: not user32.IsWindowVisible(hwnd), 15))
        check("app healthy after cancel", proc.poll() is None)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()

    print("\n--- app log ---")
    print(_tail(24))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


def _tail(n: int = 6) -> str:
    lines = [l for l in read_log().strip().splitlines() if l]
    return " | ".join(lines[-n:])


if __name__ == "__main__":
    sys.exit(main())
