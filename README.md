# Sound Lab

Brings voice recordings made on a MacBook's built-in mic up to social media / podcast standard
(default **-16 LUFS, -1.5 dBTP**).

Runs on **macOS** (recording uses CoreAudio) and **Windows**, and needs **ffmpeg**. Edits audio and
video (mp4/mov) files.

## Quick Start: Launch GUI by OS

The web GUI runs at [http://127.0.0.1:8765](http://127.0.0.1:8765) (opens in your default browser automatically).

### Windows
```powershell
# PowerShell (direct):
.\.venv\Scripts\soundlab.exe gui

# Command Prompt (CMD, direct):
.venv\Scripts\soundlab.exe gui

# Or by activating the virtual environment:
.\.venv\Scripts\Activate.ps1    # PowerShell
# or: .venv\Scripts\activate.bat # CMD
soundlab gui
```

### macOS
```bash
# Direct:
.venv/bin/soundlab gui

# Or by activating the virtual environment:
source .venv/bin/activate
soundlab gui
```

## Setup

```bash
brew install ffmpeg                  # skip if already installed
./scripts/install_deepfilter.sh      # DeepFilterNet3 → tools/deep-filter (SHA-256 checked)

python3 -m venv .venv                # Python >= 3.10
source .venv/bin/activate
pip install -e .                     # installs the `soundlab` command

soundlab doctor                      # dependency check
```

Activate the environment in every new terminal with `source .venv/bin/activate`
(or call `.venv/bin/soundlab ...` directly).

Without `deep-filter` the pipeline falls back to ffmpeg's `afftdn` denoiser automatically.

On **Windows**: install ffmpeg (7 or newer) so `ffmpeg` / `ffprobe` are on `PATH` (e.g.
`winget install ffmpeg`), create the venv with `py -m venv .venv` and run `.venv\Scripts\pip install -e .`.
`install_deepfilter.sh` is macOS only: put the Windows build of deep-filter 0.5.6 from the
[DeepFilterNet releases](https://github.com/Rikorose/DeepFilterNet/releases) at `tools\deep-filter.exe`,
or use the `afftdn` fallback. Deleting in the GUI moves files to `~\SoundLab Trash` (not the Recycle Bin).

Optional generative denoisers (see below), each in its own venv under `tools/`, with pinned
versions and checksummed model files:

```bash
./scripts/install_resemble.sh        # Resemble Enhance, ~1.6 GB, Python 3.10 or 3.11
./scripts/install_sidon.sh           # Sidon, ~2 GB, Python 3.11+
```

## Usage

```bash
soundlab enhance take.m4a                       # default: -16 LUFS, m4a
soundlab enhance *.m4a --target -14             # louder, for social media
soundlab enhance take.m4a --format wav          # 24-bit WAV master
soundlab enhance take.m4a --denoiser afftdn     # deepfilter | resemble | sidon | afftdn | none
soundlab enhance take.m4a --preset voice-b      # alternative DSP chain
```

### Resemble Enhance

`--denoiser resemble` uses [Resemble Enhance](https://github.com/resemble-ai/resemble-enhance)
instead of DeepFilterNet: it removes the noise and then **rebuilds the voice** with a generative
model. In the first ear test (an 8 s MacBook take) it beat DeepFilterNet: the pauses come out
~13 dB quieter (−64 vs −51 dB). Because it generates the audio, listen before you use a result.
It runs on the **CPU** (it crashes on the Mac's GPU) and takes about **3–4× the recording's length**
(a 34 s take: 2 min; 10 min: ~35 min, more once the fanless MacBook Air gets hot and slows down).
DeepFilterNet stays the default. Edit first and enhance only the final export: Resemble then processes
just the finished content (Tighten alone cut a take by ~22%). The CLI prints the estimate before it
starts; the GUI asks first when a job would take over a minute.

```bash
soundlab enhance take.wav --denoiser resemble                 # ear-tested defaults
soundlab enhance take.wav --denoiser resemble --tau 0.3 --seed 7
```

| Setting | GUI | Default | What it does |
|---|---|---|---|
| `--tau` | Temperature | 0.5 | 0–1: lower stays closer to the input, higher varies more (0.5 sounded better than 0.2) |
| `--lambd` | Strength | 1.0 | 0–1: how much it denoises before the rebuild (0.5 vs 1.0 inaudible) |
| `--nfe` | Steps | 32 | 1–128: fewer = faster (32 vs 64 inaudible, 32 is ~25% faster) |
| `--solver` | Solver | midpoint | `midpoint` / `rk4` (slower) / `euler` |
| `--seed` | Variation | 0 | 0–9999: the rebuild starts from random noise, so each number is a slightly different take (±3 dB in the 6–12 kHz band and the pauses); the same settings + number give the same result |

Its *denoise-only* mode isn't offered: on a real take it removed nothing (the output equalled the
input). Outputs get `_resemble` in their name, and the report lists the settings.

### Sidon

`--denoiser sidon` uses [Sidon](https://github.com/sarulab-speech/Sidon) (University of Tokyo, MIT),
an open reproduction of Google's Miipher: a w2v-BERT 2.0 model predicts clean speech features from a
16 kHz copy of the input and a decoder synthesises 48 kHz speech from them — so everything above
8 kHz is generated. It's **fast** (~0.45× the recording's length on the CPU: a 34 s take in 15 s,
~7× faster than Resemble), multilingual, deterministic and has no settings. On the first test take
it removed less noise than Resemble (pauses −57 vs −64 dB) and changed the voice's tone the most
(1–3 kHz +2 dB) — listen before you use it. CPU only: its CPU model files fail on the Mac GPU.
Outputs get `_sidon` in their name. It runs offline; everything loads from `tools/sidon-model`.

### Projects

Each project has its own folder: `projects/<name>/{inputs, outputs, voice-records}`. The CLI and
GUI share the same **current project** (switch it from the GUI's top bar or with `soundlab project`).

```bash
soundlab project                       # list projects (● current)
soundlab project new "Reels October"
soundlab project use Default
soundlab project rename Default Main   # renames the folder too
```

Rename from the GUI when you can: the browser keeps each project's timeline under its name, and
only a GUI rename carries it over. Deleting a project is done from the GUI (the folder goes to the
Trash).

### GUI

```bash
soundlab gui        # opens http://127.0.0.1:8765 in the browser
```

- **Top bar**: pick a project, **+ New project**, **Rename**, **Delete project**. Each project keeps
  its own timeline.
- **Media** (left): the files you imported (`inputs/`) and recorded (`voice-records/`) in the
  current project. Import by dropping from Finder or with **+ Add files** (the file is copied into
  `inputs/`). On hover: **Finder** (show in Finder) and **Delete** (move to the Trash). **Exports**
  sit in a separate, collapsible section; compare sets are hidden.
- **Recording**: ● starts/stops a recording (CoreAudio via `sounddevice`). **Take name** names the file (`intro.wav`,
  then `intro-2.wav` …; empty: `rec_HHMMSS.wav`). With **3-2-1** on, the mic starts at "3" and the
  countdown is cut from the take when it stops (0.3 s lead-in kept); clicking during the countdown
  cancels it (the file goes to the Trash). The bar under the button is the live input level
  (−60…0 dBFS, yellow above −12, red above −3; the marker is the loudest peak so far) — it shows
  during the countdown too, to check the level before speaking.
- **Editor**: up to 33 recordings stacked as tracks. Drop a file on the editor header or empty area
  → new track; drop it on a track → replace that track's recording. Pressing and dragging on the
  waveform moves the playhead; the trim range only changes from its left/right edges. The playhead
  can't leave the trim range (so I/O can only narrow it — widen it with the edges). `I` / `O` set
  start/end at the playhead (the times next to Play show the range), `Space` plays (the last clicked track), speed 0.5×–2× (pitch preserved). The bar at a
  track's bottom edge sets its height. **Tighten** makes a new `<name>_tight.wav` in Media from the
  track's range and opens it in a new track right below (the track itself stays as it is): silence at
  the start and end is trimmed (0.1 s lead-in, 0.15 s decay kept; mouse / trackpad clicks don't count
  as speech) and pauses inside are cut down. The slider under the button sets how hard, 0.05–1 s:
  **full left = Skip silence**, social-media jump cuts — everything 18 dB under your voice (room
  tone, breaths, word tails) counts as silence, every gap longer than 0.1 s goes down to 0.05 s and
  the first word starts 0.05 s in; towards the right the longest kept pause grows to 1 s and only
  real silence (the recording's noise-based threshold) is cut. It remembers where you left it. Half
  the kept pause stays on each side, every cut gets a 5 ms fade, and short quiet sounds inside a
  pause (breaths, lip smacks: < 0.25 s, 15 dB under the voice) go with it.
  The original stays untouched; Play, cards and export all match the new file. A pause is a run of
  20 ms windows under a threshold between the recording's noise floor and speech level; a `_tight`
  file keeps those levels, so tightening it again changes (almost) nothing.
  **✨ Enhance** works the same way: the enhanced version opens in a new track right below.
  The editor header picks the denoiser for Enhance and Export — **DeepFilterNet** (default),
  **Resemble Enhance** or **Sidon** (not installed ones are greyed out) — and Resemble's **⚙ Settings** (Temperature, Strength, Steps, Solver, Variation, 🎲 for
  a random one, *Reset to defaults*; a • marks changed settings). The choice and settings are
  remembered in the browser. A Resemble job estimated to take over a minute asks first, with the
  estimate (~2.5 + 0.03 × Steps seconds per second of audio). While Enhance or Export runs, the
  button shows the progress; click it again to cancel.
  The **⠿ chip** next to *Add to timeline* drags the selection (trim + speed) to the timeline — or,
  dropped in the editor, makes it a recording of its own: `<name>_part.wav` (lossless, in `inputs/`,
  the source untouched) opens in a new track below the track it lands on, or at the end on empty
  space, with the same speed. E.g. to drop a mistake from the middle of a take: trim to the part
  before it, drag the chip out, then trim this track to the part after it. Between the trim range's
  start and end times is the playhead, to the millisecond (trim points are stored and rendered at
  1 ms); the **▲** under the start / end time sets it to the playhead (like `I` / `O`; a start after
  the end, or an end before the start, is ignored). `←` / `→` move it 10 ms (`⇧` 100 ms, `⌥` 1 ms), inside the trim range.
  **Zoom** is per track: pinch or `⌘` + scroll on the waveform (around the pointer), `+` / `−` (around
  the playhead), `0` or **Fit** for the whole recording; the header shows how much time one pixel
  covers (down to 1 px = 1 ms) and a ruler under the waveform gets finer as you zoom in. While
  playing, the view follows the playhead.
  An enhanced file gets an **A/B** button (`B`): it switches to the original at the same position —
  rendered from the same clips and raised to the enhanced file's loudness, so it isn't just "louder
  sounds better"; the button shows by how much (e.g. "A · Original +16 dB").
- **Timeline**: drag clips in, drag cards to reorder; clicking a card opens the clip in a track and
  **Update clip** writes changes back to the same card. **▶ Play** previews the timeline: after
  every change the server renders one preview WAV in the background with the same trim/speed as the
  export (no enhancement, cached in the system temp folder), so there are no gaps between clips.
  Cards are proportional to clip length and show the trimmed part's waveform; click or drag on the
  strip above the cards to move the playhead (after clicking the timeline, `Space` plays/pauses it
  too).
- **Export**: every clip edge gets a 10 ms fade-in / fade-out (no clicks at cuts); clips that continue
  each other (same file, one ends where the next starts) render as one clip, so cutting a take
  apart and putting it back together adds no seam. If a clip is already
  enhanced and **Enhance** is ticked, a warning shows: enhancing twice runs the denoiser again and
  re-encodes lossy audio → untick it → **Join & Export** →
  `<project>/outputs/YYYY-MM-DD/edit_HHMMSS.m4a` (+ a `.json` with the clip list)

Edits are non-destructive: source files never change; trim/speed are applied only on export.

### Video

Video files (`.mp4 .mov .m4v .webm .mkv`, with a sound track) can be imported like audio: the
waveform shows their sound, and the edits apply to picture and sound together.

- **Tighten** / copy to a new track on a video → `<name>_tight.mp4` / `<name>_part.mp4`.
- **Join & Export** of a timeline with at least one video clip → `edit_HHMMSS.mp4` (H.264 + AAC):
  every clip is scaled and padded to the first video clip's size and frame rate, audio-only clips get a
  black picture, and the (enhanced) sound is the same as an audio export's. Each clip gets exactly as
  many frames as its sound lasts, so picture and sound stay in sync over any number of cuts.

### Recording

```bash
soundlab record                          # built-in mic, stop with Ctrl+C
soundlab record --duration 60            # record 60 seconds
soundlab record --enhance --target -14   # record + enhance automatically
soundlab record --list-devices           # input devices
soundlab record --device airpods         # device name (or part of it) or index
soundlab record --name intro             # intro.wav, then intro-2.wav …
```

Recordings are saved as lossless 24-bit WAV under
`<project>/voice-records/YYYY-MM-DD/rec_HHMMSS.wav`. Voice Memos records compressed AAC by default;
lossless input lets the denoiser work more cleanly. Levels are checked at the end of a recording
(clipping / too quiet warning).

The mic is read through CoreAudio (Python `sounddevice`), not ffmpeg: ffmpeg's `avfoundation`
input dropped ~7% of the audio in 11.6 ms blocks (clicks, an 8 s take came out 7.4 s long). ffmpeg
only converts the captured samples to the WAV.

On the first recording macOS asks for mic permission for the terminal app. Without permission
**silence** is recorded without any error: System Settings → Privacy & Security → Microphone.

### A/B comparison

```bash
soundlab compare take.m4a
```

Processes the same recording stage by stage (unprocessed → light/full DeepFilterNet → EQ, + Resemble
Enhance with its defaults and Sidon if they're installed) into WAV variants all at the same loudness:
`<project>/outputs/YYYY-MM-DD/compare_<name>_<time>/`. Use it to hear which stage improves or harms
the sound.

Outputs are saved in the current project as
`<project>/outputs/YYYY-MM-DD/<name>_<preset>_<HHMMSS>.<ext>`, next to a `.json` report with the
measurements and the chain used.

## Test

```bash
pip install -e ".[dev]" && playwright install webkit   # once
python tests/e2e_gui.py            # GUI end to end (WebKit, headless); --headed to watch
python tests/test_video.py         # video: Tighten / export sync, progress / cancel, GUI endpoints
```

The tests generate their own media files and remove everything they create. It never uses the mic:
recording runs on a generated tone (`SOUNDLAB_RECORD_INPUT=tone:330`).

## Pipeline

1. 80 Hz high-pass, 48 kHz mono WAV
2. Denoise: DeepFilterNet3 (`-a 30 --pf`), Resemble Enhance / Sidon (`soundlab/resemble_worker.py`,
   `soundlab/sidon_worker.py`, each run with its own venv's Python, offline) or `afftdn`
3. DSP chain (EQ, de-esser, compressor) — `soundlab/presets.py`
4. Pre-gain + `alimiter`: keeps peaks under the TP target
5. 2-pass `loudnorm` (`linear=true`): dynamics stay intact

Without step 4, loudnorm silently drops from linear to *dynamic* mode on quiet recordings
(pumping). The same happens when the measured LRA exceeds the target, so the last pass stretches
the LRA target to the measured value. The report's `normalization_type` field shows which mode ran.

## License and credits

Sound Lab is released under the [MIT License](LICENSE).

- [wavesurfer.js](https://github.com/katspaugh/wavesurfer.js) 7.12.12 is bundled in
  `soundlab/web/vendor/` under the BSD 3-Clause License ([notice](soundlab/web/vendor/LICENSE)).
- Downloaded by the install scripts, not bundled, each under its own license:
  [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet),
  [Resemble Enhance](https://github.com/resemble-ai/resemble-enhance),
  [Sidon](https://github.com/sarulab-speech/Sidon) and
  [w2v-BERT 2.0](https://huggingface.co/facebook/w2v-bert-2.0).
- [ffmpeg](https://ffmpeg.org) is installed separately.
