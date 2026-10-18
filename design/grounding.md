# Phase A grounding — dictation overlay

Existing system: `D:\vscode-dictation\dictate.py`, single file, ~1125 lines.
Written and debugged in this session. 22/22 automated checks pass across three
suites (`--selftest`, `--modetest`, `--injecttest`).

## What it does today

Global hotkey (`ctrl+alt+t`), hold to talk / tap to toggle, records via
sounddevice, transcribes with local faster-whisper `small`, pastes into the
window that was focused at record start via clipboard + SendInput Ctrl+V.
Audio never leaves the machine.

## Ownership map (subsystems by the knowledge they own)

| Subsystem | Lines | Owns |
|---|---|---|
| `Config` | 60-92 | config.json over DEFAULTS |
| Win32 input | 133-517 | ctypes structs, kernel32/user32 signatures, `RegisterHotKey`, low-level hook, focus, clipboard, `SendInput` |
| `Recorder` | 520-580 | PortAudio stream, queue → pump thread → float32 frames, tracks `peak` |
| `Transcriber` | 582-642 | lazy model load on background thread, `transcribe(audio) -> str` |
| `Dictation` | 645-784 | gesture state machine (idle/pending/hold/toggle), orchestration, paste |
| `run()` | 787-819 | wires listener → Dictation; main thread is a `while True: sleep(0.3)` loop |
| tests | 821-1085 | selftest / modetest / injecttest |

## Threading model — the binding constraint

- **main thread**: idle `sleep` loop. Free to be replaced.
- **hotkey thread**: `HotkeyListener` — `PeekMessageW` loop + `WH_KEYBOARD_LL` hook.
- **whisper-load thread**: one-shot model load.
- **pump thread**: one per recording, drains audio queue.
- **hotkey dispatch threads**: `threading.Thread` per press; `_stop_phase` runs
  transcription synchronously on that thread.
- **PortAudio callback thread**: `_callback` → `queue.put`. Must never block,
  never touch UI.

Anything UI-shaped must live on the main thread. Tk requires it outright.
Other toolkits can create their own thread, but then they cannot be the
main loop of this process.

## Hard constraints the design must honor

1. **The overlay must not steal focus.** Dictation captures the foreground
   window at record start and restores it before pasting. If the overlay
   activates, that whole mechanism breaks and text goes to the wrong window.
   Tk has no native "non-activating" window — it needs
   `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` via `SetWindowLong`.
2. **`WS_EX_NOACTIVATE` also means no keyboard focus**, so the overlay cannot
   receive the hotkey itself. Hotkey stays in the existing `HotkeyListener`.
3. Must not interfere with `restore_focus` (which uses `AttachThreadInput` to
   borrow the foreground thread's input queue).
4. Must not block the PortAudio callback or the transcription path.
5. Single-file delivery is the current shape. Splitting is allowed if justified.
6. Model is `small` on CPU int8. Measured: full 4.5 s utterance transcribes in
   roughly 2-4 s wall clock. Beam size 5 on the final pass.

## faster-whisper streaming — what is actually possible

- `model.transcribe()` returns a **generator of segments**, which can be
  iterated lazily — so segments can be emitted as they finalize *within* one
  call. It still requires all the audio up front.
- There is **no incremental/streaming API**. Feeding audio progressively is not
  supported.
- Practical consequence for live preview: re-transcribe a sliding tail of the
  accumulated audio on a timer (cheap settings: `beam_size=1`, `vad_filter=False`,
  `without_timestamps=True`) and display that as an approximate preview, then
  replace it with the authoritative full pass at the end.
- Cost control matters: naive re-transcription of the whole buffer is O(n²).
  A bounded tail window is the obvious lever.
- `condition_on_previous_text=False` is already set on the final pass to avoid
  hallucination loops (a real bug found during development — Whisper looped
  "we're going to use a little bit of the same thing" on garbage input).

## Audio level for a reactive blob

`Recorder` already computes `self.peak` per 100 ms frame on the pump thread.
A blob wants **RMS**, and it wants it as a monotonic value the UI can poll
without contending for the same lock the pump uses.

## The request being designed

Wispr Flow-style feedback:
1. a floating "blob" that appears while listening and reacts to microphone level
2. a distinct visual state while transcribing
3. a live transcription preview that updates as you speak

## Open question for Phase B

What is the right *shape* for this? The candidates must differ structurally,
not just in which widget library they reach for. Things worth arguing about:
where the overlay lives (in-process vs separate process), who owns the Tk/main
thread, how the level and preview streams reach the UI without leaking
PortAudio or Whisper types past a boundary, and what happens if the overlay
crashes while a recording is in flight.
