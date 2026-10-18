# v0.1.0 — first public release

Push-to-talk dictation for Windows that is fully local: hold a hotkey, speak,
let go. The audio never leaves your machine — transcription runs on your own
CPU.

## Highlights

- **One key, two gestures** — hold `Ctrl+Alt+T` to dictate a snippet, tap it
  to toggle continuous dictation; `Ctrl+Alt+X` cancels.
- **Estonian that actually works** — defaults to
  [TalTechNLP/whisper-large-v3-turbo-et-verbatim](https://huggingface.co/TalTechNLP/whisper-large-v3-turbo-et-verbatim)
  (~3× lower WER than vanilla Whisper on Estonian; 24.6 % vs 77.8 % for
  `base` on the same machine). Still transcribes English correctly when mixed
  in. Any other faster-whisper model is one config entry away.
- **Works in any text field** — the transcript is typed via `SendInput` into
  the window that had the caret; the window you start in is restored before
  pasting, so you can click elsewhere mid-recording.
- **Unobtrusive feedback** — a screen-bottom blob: swells with your voice
  while listening, spins while transcribing, settles when delivered. No
  focus, no taskbar entry, clicks pass through.

## Install

Requirements: Windows 10/11, Python 3.10–3.12, a microphone. The first run
downloads the ~460 MB model.

```bat
git clone https://github.com/n0ll0/local-dictation.git
cd local-dictation
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
start.cmd
```

Full instructions, configuration reference, and selftests: see the
[README](https://github.com/n0ll0/local-dictation#readme).

## Verify before/at first run

```bat
.venv\Scripts\python.exe dictate.py --selftest     :: mic, model, ASR, clipboard
.venv\Scripts\python.exe dictate.py --modetest     :: hold-vs-tap gesture logic
.venv\Scripts\python.exe dictate.py --injecttest   :: paste into a real text field
```

## Known limits

- `UpdateLayeredWindow` (soft feathered edges) fails on some Windows builds;
  the tool probes for it and falls back to a hard-edged window clipped with
  `SetWindowRgn`.
- Injecting into an **elevated** target is blocked by UIPI by design — logged
  as a failure, not silently typing.
- Only `CF_UNICODETEXT` is saved/restored on the clipboard.
- Whisper code-switches mid-sentence poorly; pin `language` for long
  single-language passages.

## Credits

TalTechNLP (Tallinn University of Technology) for the Estonian Whisper
fine-tune; SYSTRAN for [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper);
OpenAI for the [Whisper](https://github.com/openai/whisper) base models.
MIT license.
