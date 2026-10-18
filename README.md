# Local dictation

Push-to-talk dictation for Windows that is **fully local**. Hold a hotkey,
speak, let go. The transcript is transcribed on your own CPU and pasted into
whatever text field was focused when you started talking. Audio never leaves
the machine.

No cloud, no API keys, no per-minute charges. A Fluent-ish blob at the bottom
of your screen tells you what the transcription is doing.

![The overlay in its listening state](design/overlay-frames/listening-loud.png)

## Features

- **Push-to-talk and toggle on one key.** Hold `Ctrl+Alt+T` to dictate short
  snippets; tap it to toggle continuous dictation for longer passages. The tap
  threshold (350 ms) is configurable. `Ctrl+Alt+X` cancels a recording.
- **0\.3 GB model, 2× realtime on CPU.** Defaults to
  [TalTechNLP/whisper-large-v3-turbo-et-verbatim](https://huggingface.co/TalTechNLP/whisper-large-v3-turbo-et-verbatim),
  a fine-tune of OpenAI's Whisper large-v3-turbo on ~1400 h of Estonian, run
  through [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) in
  int8 on plain CPU.
- **Any text field.** Works system-wide: terminals, editors, browsers, chat
  apps. Wherever a caret is.
- **Unobtrusive overlay.** A circle that swells with your voice while
  recording, spins while transcribing, settles when delivered. It never takes
  focus, sits outside the taskbar and Alt+Tab, and clicks pass through it.
- **Model-agnostic.** One `config.json` entry is enough to switch to any
  other Faster-Whisper model (including multilingual base models).

## Quick start

If the command line is not your thing, follow
[INSTALL.md](INSTALL.md). It walks the same process with double-clicks: a
`setup.cmd` button creates the environment, `start.cmd` starts dictating,
and `add-to-startup.vbs` optionally hooks it to logon.

For everyone else: requirements are **Windows 10/11**, **Python 3.10 to
3.12**, a microphone. The default model download is **~460 MB**.

```bat
git clone https://github.com/<you>/local-dictation.git
cd local-dictation

python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Run it in a visible console the first time, so you can see it load the model
and grab the hotkey:

```bat
start.cmd
```

Then in any app: hold `Ctrl+Alt+T`, speak, let go.

To also launch it at logon with no console window:

```bat
REM creates a Startup-menu shortcut that runs startup.vbs hidden
"%SystemRoot%\system32\wscript.exe" //H:hidden   (once, optional)
```

or simply copy a shortcut to `startup.vbs` (`background.cmd`'s hidden
launcher) into
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`.

## Usage

| | |
|---|---|
| `start.cmd` | visible console |
| `background.cmd` | hidden, logs to `dictate.log` |
| `design\demo_overlay.py listening` | shows the overlay without dictating |

Then anywhere in Windows:

| Gesture | What happens |
|---|---|
| **Hold** `Ctrl+Alt+T` | records while held, transcribes and types on release |
| **Tap** `Ctrl+Alt+T` | records until you press it again (for longer passages) |
| Press `Ctrl+Alt+X` | discards the recording |

A press starts out undecided; if you are still holding it after 350 ms it
becomes push-to-talk, otherwise it becomes toggle.

Starting it twice is harmless: the second instance loses the race for the
hotkey, says so in the log, and exits.

## Which model, and why

Default is
**TalTechNLP/whisper-large-v3-turbo-et-verbatim**, a Whisper fine-tune
from [Tallinn University of Technology](https://taltech.ee/en/et/)
(TalTechNLP, ~1400 h of Estonian), used in their own Estonian ASR work.
Measured word error rate on 60 s of Estonian broadcast speech on this
machine (int8, CPU):

| model | Estonian WER | 60 s audio | speed |
|---|---|---|---|
| `base` (OpenAI) | 77.8% | 3.8 s | 15.7× realtime |
| `small` (OpenAI) | 67.5% | 10.8 s | 5.5× realtime |
| **TalTech turbo-et** (default) | **24.6%** | 28.9 s | 2.1× realtime |

Vanilla Whisper is not usable for Estonian. `base` got roughly **five words
in six wrong**. The fine-tune is a ~3× error reduction against `small`, while
still transcribing English correctly, so mixed Estonian/English dictation
works.

The tradeoff is real: 2.1× realtime means a 5-second utterance takes ~2.3 s
to transcribe. That is what the spinning "transcribing" state is for. If you
prefer speed to accuracy, set `"model": "base"` in `config.json`.

If you mostly dictate another language, any faster-whisper model id works the
same way (check the [HF model list](https://huggingface.co/models?search=whisper)
for fine-tunes of your language. Most languages have a stronger community
model than vanilla Whisper).

To reproduce the table:

```
.venv\Scripts\python.exe design\bench_models.py
```

## Configuration

`config.json` next to the script is optional; delete any key to fall back to
the built-in default.

```json
{
  "hotkey": "ctrl+alt+t",
  "cancel_hotkey": "ctrl+alt+x",
  "model": "TalTechNLP/whisper-large-v3-turbo-et-verbatim:ct2",
  "language": "et",
  "tap_ms": 350,
  "max_seconds": 120,
  "input_device": null,
  "overlay": true,
  "overlay_position": "screen-bottom",
  "diameter": 300,
  "overlay_preview": false
}
```

- `language` is `"auto"` by default, which detects each utterance. Pin it
  to `"et"` or `"en"` when you mostly dictate one language. Pinning skips
  Whisper's language-detection pass, roughly 2× faster.
- `overlay_position` puts the circle at `"screen-bottom"`, centred on the
  whole display, or `"caret"`, floating above the window you are typing in.
- `diameter` is the overlay circle's size in pixels.
- `overlay_preview` set to true shows a live approximate transcript in the
  overlay. It is off by default because it delays the final paste; see
  *Notes and limits*.
- `input_device` set to null uses the system default. If dictation picks the
  wrong microphone (a stereo-mix device shows up as the default on some
  machines), set it to an exact device name from `--selftest`.

Command-line overrides: `--model`, `--language`, `--device`, `--config`.

## Verifying it works

```bat
.venv\Scripts\python.exe dictate.py --selftest     :: mic, model, ASR, clipboard
.venv\Scripts\python.exe dictate.py --modetest     :: hold-vs-tap gesture logic
.venv\Scripts\python.exe dictate.py --injecttest   :: paste into a real text field
python design\overlaytest.py                       :: the visual layer, headless
python design\e2e_overlay.py                       :: the real app, real hotkey
```

`overlaytest.py` renders every overlay state to PNG in `design\overlay-frames`
so you can see the visual language without a microphone.

## Notes and limits

- **Not admin.** The hotkey uses `RegisterHotKey` plus a low-level keyboard
  hook, neither of which needs elevation. Injection uses `SendInput`, which
  is blocked by UIPI if the target runs elevated. The tool logs a failure
  rather than pretending it worked.
- **Per-pixel alpha may be unavailable.** The soft-edged blob needs
  `UpdateLayeredWindow`, which fails on some Windows builds and graphics
  stacks. The tool probes for it at startup and falls back to a hard-edged
  window clipped with `SetWindowRgn`; if the log says `no per-pixel alpha`,
  that is what you are seeing, and the overlay is still fully functional.
- **Clipboard.** Only `CF_UNICODETEXT` is saved and restored. If you had an
  image or a copied file list on the clipboard, that is lost.
- **Focus.** The window focused at record start is restored before pasting,
  so you can click elsewhere while talking.
- **Language mixing.** Whisper code-switches mid-sentence poorly. For long
  single-language passages pin `language` instead of using `auto`.
- **The preview is approximate.** It re-transcribes a bounded tail of the
  recording on a timer, because faster-whisper has no streaming API. The
  final pass replaces it and is the only thing that gets pasted.

## Project layout

| Path | What |
|---|---|
| `dictate.py` | hotkeys, capture, transcription, paste. The app |
| `feedback.py` | the overlay: blob, preview, window |
| `design/` | spike scripts, tests, the model benchmark, rendered frames |
| `background.cmd` / `start.cmd` / `startup.vbs` | hidden / console / logon launchers |

Everything (model cache, venv, logs) lives beside the checkout and is
resolved relative to the scripts, so the repo can be checked out or renamed
on any drive.

## Credits

- [TalTechNLP/whisper-large-v3-turbo-et-verbatim](https://huggingface.co/TalTechNLP/whisper-large-v3-turbo-et-verbatim)
  is the Estonian Whisper fine-tune, by the TalTech NLP group.
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) /
  [CTranslate2](https://github.com/OpenNMT/CTranslate2) is the inference engine.
- [OpenAI Whisper](https://github.com/openai/whisper) is the base.
- [sounddevice](https://python-sounddevice.readthedocs.io/) is audio capture.

## License

MIT. See [LICENSE](LICENSE). The model weights follow the licenses published
by their upstream authors (Whisper is MIT; check the model card for any
dataset restrictions).
