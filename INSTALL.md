# Install guide, no commands to type

Everything here is double-clicks in normal Windows. You create folders,
run two lazy buttons, and wait a bit. The only window that looks like a
terminal is `setup.cmd`, and you never type anything into it.

What you are installing: push-to-talk dictation that runs entirely on this
computer. Audio never leaves the machine. Internet is used once, to download
a speech model of about 460 MB, during the first start of the app itself.

## Step 1. Install Python

1. Open www.python.org/downloads in your browser and click the yellow
   Download Python button.
2. Run the downloaded installer.
3. On the first screen, before clicking Install Now, tick the small box that
   says Add python.exe to PATH. This one box is the step people miss. Leave
   it unticked and step 3 will say Python was not found.
4. Click Install Now and let it finish. Close the installer.

## Step 2. Get the app

1. Go to github.com/n0ll0/local-dictation.
2. Click the green Code button, then Download ZIP.
3. Find local-dictation-main.zip in your Downloads folder. Right-click it,
   Extract All. Pick a destination with a few gigabytes free; C: or D:
   does not matter. The model downloads into this folder later, so do not
   pick a tiny USB stick.
4. Open the extracted local-dictation-main folder and keep it handy. This
   folder is the whole application.

## Step 3. Double-click setup.cmd

A black window opens and types a wall of text at you. That is normal. It
creates a private Python environment in the folder and installs the speech
packages, and it never asks for anything. Wait until it says Done. This
takes one or three minutes on most internet.

If it says Python was not found, do Step 1 again, remember the PATH box, and
run setup.cmd again. Everything here is safe to run more than once; it
skips what is already done.

## Step 4. Double-click start.cmd and talk

A console window opens. First start downloads the model, roughly 460 MB,
one time. The window looks frozen while it works. It is fine. Give it
minutes, not seconds. You will see lines like `model ready in ...` when the
transcriber is up.

Then, with the console sitting wherever is convenient:

1. Open any place that accepts text. Notepad is perfect.
2. Hold `Ctrl+Alt+T`, speak, let go.
3. Your words appear where the text cursor was.

Tap the same combination to switch dictation on without holding, tap again
to stop. `Ctrl+Alt+X` throws away the current recording.

You are done. If you want dictation to start by itself at every logon, do
Step 5. If not, keep using the two buttons above whenever you like.

## Step 5, optional. Start it with Windows

Double-click `add-to-startup.vbs`. A popup confirms it, and from then on
dictation starts hidden at every Windows logon, with no window at all.

To undo, press Win+R, type `shell:startup`, press Enter, and delete the
`Local dictation.lnk` in that folder.

## Making it yours

Create a plain text file named `config.json` next to dictate.py, with any of
these settings. Delete the file and every setting returns to default.

```json
{
  "hotkey": "ctrl+alt+t",
  "model": "base",
  "diameter": 300,
  "overlay_preview": false
}
```

- `hotkey` and `cancel_hotkey` change the key combinations. Formats like
  `ctrl+shift+d` work in the same way.
- `model: base` transcribes three times faster but makes many more mistakes
  in Estonian. The TalTech Estonian fine-tune is the accuracy default.
- `diameter` is the listening blob's size on screen, in pixels.
- `overlay_preview: true` shows the words in the blob while you speak, at
  the cost of slower final transcription.

## If something does not work

**Python was not found.** Step 1 was done without the Add python.exe to PATH
box, or Python is genuinely missing. Re-run the installer, tick the box,
then run setup.cmd again.

**setup.cmd complains about Permission denied.** A running copy of dictation
is holding the folder. Press Ctrl+Shift+Esc to open Task Manager, find every
python.exe process and end it, then run setup.cmd again.

**The console says something but nothing gets typed.** Two common causes.
The text field's app runs as administrator, and Windows blocks typing into
it on purpose; start dictation from an administrator console for that rare
case, or write into something un-elevated. Otherwise the microphone may be
the wrong one; see the next item.

**It transcribes the wrong voice.** Windows may point the app at the wrong
input, a Stereo Mix device for example. Open `dictate.log` next to
dictate.py, scroll to a line that lists available inputs, and set the one
you want as `"input_device": "its exact name"` in config.json.

**The blob has a hard edge.** Windows refused soft translucent windows on
this machine, so the circle is clipped instead. Function is unchanged. The
log confirms it with a no per-pixel alpha line.

**Everything gone wrong.** dictate.log beside dictate.py holds every detail.
Open it and read the last lines; they usually say exactly what to fix. If not,
paste them into this repository's Discussions tab and ask for help.

## Removing it

Delete the app folder, and the `Local dictation.lnk` from Step 5's Undo
section. Nothing was written anywhere else in Windows except those two
places. The Step 5 shortcut exists only if you enabled it.
