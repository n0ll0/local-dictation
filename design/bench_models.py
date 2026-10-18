"""Which model should dictate Estonian on THIS machine?

Measures word error rate against the model's own demo transcript, and wall
clock, on real Estonian broadcast speech. No guessing from leaderboards.

Run:  python bench_models.py
"""
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from __future__ import annotations

import os
import re
import sys
import time

import numpy as np

os.environ.setdefault("HF_HOME", ROOT + r'\huggingface')
sys.path.insert(0, ROOT)

from faster_whisper import WhisperModel          # noqa: E402
from dictate import _read_wav, SAMPLE_RATE       # noqa: E402
from dictate import log                        # noqa: E402

import glob

SNAP = glob.glob(os.path.join(
    ROOT + r'\huggingface\hub',
    "models--TalTechNLP--whisper-large-v3-turbo-et-verbatim",
    "snapshots", "*"))[0]
# The demo filename has non-ASCII characters; glob it.
WAV = glob.glob(os.path.join(SNAP, "demo", "*.wav"))[0]
REF = glob.glob(os.path.join(SNAP, "demo", "*.txt"))[0]
ET_MODEL = os.path.join(SNAP, "ct2")

ENGLISH = ("The deployment pipeline builds the repository and runs the tests "
           "before anyone looks at the logs in production.")


def norm(s: str) -> list[str]:
    s = s.lower().replace("â€™", "'")
    s = re.sub(r"[^a-zÃµÃ¤Ã¶Ã¼Å¡Å¾0-9' ]+", " ", s)
    return s.split()


def wer(ref: list[str], hyp: list[str]) -> float:
    """Word error rate. Edit distance over words, standard 1 - substitutions."""
    if not ref:
        return 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return (prev[len(hyp)] / len(ref)) * 100.0


def load_audio(seconds: float = 60.0) -> np.ndarray:
    a = _read_wav(WAV)
    return a[: int(seconds * SAMPLE_RATE)]


def main() -> int:
    audio = load_audio()
    ref_text = open(REF, encoding="utf-8", errors="replace").read()
    ref = norm(ref_text)
    log(f"Estonian sample: {len(audio) / SAMPLE_RATE:.1f}s, "
        f"{len(ref)} reference words")

    # English probe, synthesised, for the code-switching check.
    from dictate import _synthesise
    en_wav = r"_en.wav"
    _synthesise(en_wav, ENGLISH)
    en_audio = _read_wav(en_wav)
    en_ref = norm(ENGLISH)

    candidates = [
        ("base (current default)", "base", None),
        ("small", "small", None),
        ("TalTech large-v3-turbo-et", ET_MODEL, "et"),
    ]

    print("\n" + "=" * 74)
    print(f"{'model':<30}{'lang':<6}{'et WER':<10}{'en WER':<10}"
          f"{'et s':<8}{'speed':<8}")
    print("=" * 74)
    rows = []
    for label, path, lang in candidates:
        try:
            t0 = time.monotonic()
            m = WhisperModel(path, device="cpu", compute_type="int8")
            load = time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001
            print(f"{label:<30} FAILED: {exc}")
            continue

        def run(a, forced):
            t = time.monotonic()
            segs, _ = m.transcribe(
                a, language=forced, beam_size=1, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segs)
            return text, time.monotonic() - t

        et_text, et_dt = run(audio, lang)
        # Estonian-only models cannot read English; give them "et" and let the
        # honest failure show up rather than flattering them with auto-detect.
        en_text, _ = run(en_audio, lang)
        et_wer = wer(ref, norm(et_text))
        en_wer = wer(en_ref, norm(en_text))
        rt = (len(audio) / SAMPLE_RATE) / et_dt
        rows.append((label, et_wer, en_wer, et_dt, rt))
        print(f"{label:<30}{str(lang or 'auto'):<6}{et_wer:>6.1f}%  "
              f"{en_wer:>7.1f}%  {et_dt:>6.1f}{rt:>7.1f}x   "
              f"(load {load:.0f}s)")

    print("=" * 74)
    best = min(rows, key=lambda r: r[1])
    log(f"\nbest Estonian: {best[0]}  WER {best[1]:.1f}%  "
        f"({best[3]:.1f}s for 60s audio)")
    for label, et_wer, en_wer, et_dt, rt in rows:
        log(f"  {label:<30} et {et_wer:>5.1f}%   en {en_wer:>5.1f}%   "
            f"{et_dt:>5.1f}s  {rt:>4.1f}x realtime")
    if os.path.exists(r"_en.wav"):
        os.remove(r"_en.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
