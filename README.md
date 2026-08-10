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

**Status:** Phase 1 (Foundation) complete. See [docs/PHASE_1.md](docs/PHASE_1.md).
The analysis, moment, narrative and render stages are scaffolded but not yet
implemented — the roadmap is in [docs/ASSESSMENT.md](docs/ASSESSMENT.md).

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
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip

python scripts/doctor.py               # check the environment
python scripts/db_init.py              # create the database

.venv/bin/python -m uvicorn backend.api.app:create_app --factory --port 8765
```

`doctor.py` reports what is missing and what the pipeline will fall back to. A
missing GPU or model is a warning, not a failure.

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
| [docs/PHASE_1.md](docs/PHASE_1.md) | what Phase 1 delivered and what it deferred |
| [docs/ASSESSMENT.md](docs/ASSESSMENT.md) | environment, dependencies, risks |

## Development

```bash
.venv/bin/python -m pytest              # 926 tests (~17 min)
.venv/bin/python -m pytest -m "not slow"  # fast subset
.venv/bin/ruff check .
```

Test artefacts stay inside the repository: `pyproject.toml` pins
`--basetemp=.pytest-tmp`, so transcoded proxies and frame dumps never land in
the system temp directory. Both are gitignored.

Tests never touch the real `projects/` directory or database — every fixture is
rooted in a temporary directory.
