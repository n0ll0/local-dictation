"""Drives the real Overlay through a full dictation with a realistic clock and
leaves each state on screen long enough to photograph.

Not a unit test - it opens the actual layered window and publishes through the
actual channel, so what you see is what the user sees. The clock is compressed
because a real transcription would otherwise take seconds per frame.

Run:  python demo_overlay.py <state>
       states: listening | thinking | delivered | discarded
"""
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from __future__ import annotations

import sys
import time

sys.path.insert(0, ROOT)
import feedback as fb  # noqa: E402

PREVIEW = (
    "tere õäöü dictation works and this is the live preview "
    "updating as you speak into the microphone"
)
FINAL = "tere õäöü dictation works, 1234"


def main(state: str) -> int:
    cfg = fb.OverlayConfig(width=460, blob_px=190)
    fb.log = print
    ch = fb.OverlayChannel()
    levels = fb.LevelSink()
    ch.attach_levels(levels)
    overlay = fb.Overlay(cfg, ch, print)
    if not overlay.open():
        print("overlay failed to open")
        return 1

    # Anchor to whatever is actually in front, the way the real tool does.
    overlay.focus_hwnd = int(fb.user32.GetForegroundWindow() or 0)
    hold_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 20000

    ch.begin_session()
    if state == "listening":
        ch.publish_state("listening")
        words = fb._text_to_words(PREVIEW)
        ch.publish_preview(fb.PreviewText(words=words[:-3], provisional=False,
                                          dropped_head=False))
        # breathe the blob so the level response is visible
        t0 = time.monotonic()
        while time.monotonic() - t0 < 900:
            e = 0.5 + 0.45 * __import__("math").sin((time.monotonic() - t0) * 6)
            levels.publish(0.004 + 0.30 * e)
            time.sleep(0.05)
    elif state == "thinking":
        ch.publish_state("thinking")
        words = fb._text_to_words(PREVIEW)
        ch.publish_preview(fb.PreviewText(words=words, provisional=False,
                                          dropped_head=False))
    elif state == "delivered":
        ch.publish_state("listening")
        time.sleep(0.3)
        ch.close_session("delivered")
        ch.publish_preview(fb.PreviewText(words=fb._text_to_words(FINAL),
                                          provisional=False, dropped_head=False))
    elif state == "discarded":
        ch.publish_state("listening")
        time.sleep(0.3)
        ch.close_session("discarded", "only silence")

    print(f"holding '{state}' for {hold_ms}ms - focus_hwnd={overlay.focus_hwnd}")
    try:
        overlay._root.after(10, overlay._tick)   # serve() does this; demo must too
        overlay._root.after(hold_ms, overlay._root.quit)
        overlay._root.mainloop()
    except Exception:
        time.sleep(hold_ms / 1000)
    overlay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "listening"))
