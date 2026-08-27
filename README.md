# AI Gaming Video Editor

A local-first AI video editor for gameplay. It ingests long recordings,
understands what happened through multimodal analysis, ranks the moments worth
keeping, builds a coherent 10–60 minute YouTube video, and verifies the result —
entirely on your own machine.

Not a video cutter. The system watches the footage, detects gameplay events and
player reactions, forms and scores moments, removes dead time and repetition
while preserving context, constructs a story, and renders it.

```
3 hours of gameplay  →  Story / Best Moments  →  20 minutes  →  YouTube-ready MP4
```

**Status:** the 15 foundation phases and the 2.0 plan are complete. A real
recording goes in and a finished, QA'd video comes out, entirely through the
browser. On top of the pipeline, 2.0 added perception dense enough to watch
every nominated region (`unknown_event_ratio` 0.61→0.36 on one real recording,
0.45→0.23 on another), a **Director** that proposes the shape of the edit, a
**Critic** that reviews the assembled timeline before rendering, learned
per-user preferences, and an overlay pass that renders only the frames that
carry something — measured 1,284 s → 15.5 s on a real ten-minute edit. Every
model answer is checked against evidence and every fallback is the
deterministic pipeline that shipped first. Progress, measurements and what
remains: [docs/PLAN.md](docs/PLAN.md).

---

## What makes it different

**It decides, then executes.** The AI produces structured decisions; ordinary
code applies them. No model ever runs a shell command, writes a file or builds
an FFmpeg filter graph.

**It works within 8 GB of VRAM.** One model is resident at a time. Vision runs
only on candidate regions nominated by cheap detectors, so a two-hour recording
does not become six hours of inference.

**It never re-analyses to re-edit.** Change the duration, the mode, or a single
clip, and only the story → EDL → render → QA chain re-runs.

**It explains itself.** Every selected moment carries the score breakdown and
the events that justified it. Ask why a clip was chosen and the answer comes
from the data, not from a language model's imagination.

**It works without talking to it.** Import, pick a duration, press analyze. The
chat is a control surface, not a requirement.

**It answers in the language you asked in.** Arabic question, Arabic answer;
English question, English answer — decided per message, not by a setting. The
moment types, event names and score dimensions are translated too, so an
explanation reads as one sentence rather than a frame around English terms.

**It reviews its own work.** Before rendering, a model reads the assembled
edit clip by clip — what is on screen, what was said, what happened — and may
trim a dead opening or drop a clip that is all menu. Code holds the veto:
a note about a clip that does not exist is discarded, and no review may take
the video out of its requested length.

**It learns what you keep asking for.** "Make it faster" in three separate
projects becomes the starting point of the fourth — and anything you say about
the current project still beats it.

**It reads the HUD when it knows the game.** A profile declares where the game
puts its state, and the pipeline reads what no OCR can — GTA V's wanted level
is five star glyphs and no text. Without a profile, nothing changes: vision,
OCR, audio and speech carry the analysis on their own.

---

## Requirements

| | |
| --- | --- |
| OS | Windows 10/11 (target), Linux (development) |
| Python | 3.10+ |
| Node.js | 20+ (for the Remotion overlay pass) |
| FFmpeg | with NVENC for hardware encoding |
| GPU | NVIDIA RTX 3070 class, 8 GB VRAM. CPU fallback works, slower |
| Disk | recordings and intermediates are large; plan for tens of GB per project |

Nothing is uploaded. No cloud API is required.

## Getting started

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,ai]"   # Windows: .venv\Scripts\pip

python scripts/doctor.py               # check the environment
python scripts/db_init.py              # create the database
npm install && npm run build -w apps/web

python scripts/serve.py                # everything: interface, API, worker
```

`[ai]` is the analysis stack — Whisper, PySceneDetect, EasyOCR, torch. It is a
few gigabytes and pinned to a CUDA build of torch; the CPU wheel works and is
much slower.

### Everything lives beside the project

The machine this was built on has 5.7 GB free on its system drive and 1.3 TB on
its data drive, so nothing is left to a library's idea of where a cache goes.
`scripts/rooted.py` puts all of it under the project root and the launcher
applies it to every process it starts:

| | |
| --- | --- |
| `.venv/` | interpreter and packages (torch alone is gigabytes) |
| `tools/ffmpeg`, `tools/node` | bundled binaries, ahead of `PATH` |
| `.tmp/` | `TMP`/`TEMP` — segments, audio slices, frame dumps |
| `.cache/hf`, `.cache/torch` | model weights |
| `.cache/pip`, `.cache/npm` | package caches |
| `remotion/.browser` | Remotion's Chromium |

`OLLAMA_MODELS` is read and reported, never written. Ollama is a shared runtime
several projects on this machine use, and one 6 GB vision model loaded once
serves all of them — where its files live is the machine's decision, not a
consumer's. Setting it here once cost 36 GB in duplicate downloads.

Then open **http://127.0.0.1:8765**.

On Windows, double-clicking **`VAI.bat`** does all of the above, opens the
browser once the server answers, and finds an interpreter that has the
dependencies — with several Pythons installed, plain `python` is whichever is
first on PATH and is rarely the right one. Running it by hand, `py -3.11
scripts/serve.py` is the reliable spelling.

Launching again while a copy is running **restarts it**, which is what "start
the app" means when you have just closed the tab. It checks first that the
port belongs to this application, and leaves anything else alone. Pass
`--keep-existing` to fail instead of taking over.

`doctor.py` reports what is missing and what the pipeline will fall back to. A
missing GPU or model is a warning, not a failure.

Optional, for the natural-language editing in the chat panel (§63):

```bash
ollama pull qwen2.5:7b-instruct
```

Without it the chat still works — the rule parser understands the common
phrasings, and only the unusual ones are lost (§95).
`python scripts/verify_phase13.py` shows what the model adds when it is there.

## Configuration

Everything tunable lives in `config/`. No business rule is scattered through the
source.

| File | Covers |
| --- | --- |
| `application.yaml` | paths, database, API, logging, disk limits |
| `output.yaml` | the 10–60 minute duration policy, video modes |
| `analysis.yaml` | chunking, frame sampling, scenes, audio, vision, OCR, HUD |
| `models.yaml` | AI providers, GPU budget, hardware profiles, fallbacks |
| `moments.yaml` | event correlation, scoring weights, dead time, repetition |
| `narrative.yaml` | story structure, hook, pacing, duration optimizer |
| `effects.yaml` | the effects library, budgets, per-style profiles |
| `rendering.yaml` | encoders, proxy, thumbnails, Remotion overlay pass |
| `audio.yaml` | mixing, ducking, music |
| `captions.yaml` | caption timing, layout, appearance |
| `qa.yaml` | technical and content checks |
| `interaction.yaml` | editing presets and the chat layer |
| `publishing.yaml` | delivery targets |

Any value can be overridden by environment variable:

```bash
VAI__APPLICATION__API__PORT=9000
VAI__ANALYSIS__CHUNK_SECONDS=300
VAI__MODELS__VISION__MODEL=llava:13b
```

## Documentation

**Resuming work on this project? Start with [docs/PLAN.md](docs/PLAN.md).**

| | |
| --- | --- |
| [docs/PLAN.md](docs/PLAN.md) | **master plan and progress** — what is done, what is next, how to resume |
| [docs/SPEC.md](docs/SPEC.md) | the specification — every `§N` in the code points here |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | layers, contracts, dependency direction |
| [docs/DECISIONS.md](docs/DECISIONS.md) | every choice the spec left open, and why |
| `docs/PHASE_N.md` | what each phase delivered, what it deferred, and the bugs it found |
| [docs/ASSESSMENT.md](docs/ASSESSMENT.md) | environment, dependencies, risks |

## Development

```bash
.venv/bin/python -m pytest              # 1505 tests (~25 min)
.venv/bin/python -m pytest -m "not slow"  # fast subset
.venv/bin/ruff check .
```

Test artefacts stay inside the repository: `pyproject.toml` pins
`--basetemp=.pytest-tmp`, so transcoded proxies and frame dumps never land in
the system temp directory. Both are gitignored.

Tests never touch the real `projects/` directory or database — every fixture is
rooted in a temporary directory.
