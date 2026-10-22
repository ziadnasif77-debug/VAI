# Project map (autonomous engineering mission)

Built on run `run-20261021-1509` from the repository itself. The project's own
documents are authoritative: [`docs/PLAN.md`](../docs/PLAN.md) (state, resume
order), [`docs/SPEC.md`](../docs/SPEC.md) (every `§N` in the code),
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md). This file is an index into
them, not a replacement. Anything marked *unverified* was read, not run.

## Languages and frameworks

| Layer | Stack | Where |
| --- | --- | --- |
| Backend, pipeline | Python ≥3.10 (pydantic v2, FastAPI, uvicorn, PyYAML, SQLite) | `backend/`, `ai/` |
| Analysis stack (optional extra `ai`) | faster-whisper, ctranslate2, PySceneDetect, EasyOCR, OpenCV, PyAV, onnxruntime | `pyproject.toml [ai]` |
| Overlay rendering | Remotion (Node/TypeScript) | `remotion/` |
| UI | Vite web app; Tauri/Electron-style renderer shell | `apps/web`, `apps/renderer` |
| Local models | Ollama (`qwen2.5vl:7b` vision, `qwen2.5:7b-instruct` LLM), faster-whisper `large-v3-turbo`, OCR `en_PP-OCRv4` | `config/models.yaml` |

## Entry points

- `python scripts/serve.py` — API + job worker + built UI on `127.0.0.1:8765` (`VAI.bat` on Windows).
- `scripts/doctor.py` — what this machine is missing. `scripts/db_init.py` — database.
- `scripts/daily_cycle.py`, `scripts/run_inbox.py` — the unattended daily producer (`backend/services/daily_producer.py`).

## Pipeline (`backend/core/models/enums.py: JobStage`, §46)

IMPORT → PROBE → PROXY → AUDIO → FRAMES → TRANSCRIPT → AUDIO_EVENTS → SCENES →
VISION → OCR → GAME_EVENTS → SEMANTIC → MOMENTS → STORY → EDL → CRITIQUE →
RENDER → QA (→ CRITIC2, PUBLISH). Analysis results are cached by input digest;
re-editing re-runs only STORY → EDL → RENDER → QA.

| Concern | Package |
| --- | --- |
| Media engine (ffmpeg/ffprobe, proxy, audio, frames) | `backend/media/` |
| Speech, audio events | `ai/speech`, `backend/analysis` |
| Vision, OCR, HUD, game events, exclusions | `ai/vision`, `ai/ocr`, `backend/gaming/` |
| Semantic lanes | `backend/semantic/` |
| Moments, episodes | `backend/moments/` |
| Story, plans, judge, Director | `backend/narrative/`, `backend/director/` |
| EDL / timeline, authorised spans | `backend/timeline/` |
| Critic (pre-render), CRITIC2 (post-render) | `backend/critic/`, `backend/critic2/` |
| Render: encoder, audio mix (loudnorm → alimiter `level=0:latency=1`, ceiling −1.8 dBFS), SFX, shorts, Remotion | `backend/rendering/` |
| QA technical (ffprobe + one decode pass: blackdetect, freezedetect, ebur128) and content | `backend/qa/` |
| NL editing, conversation, versions | `backend/interaction/` |

## Games

Profiles in `profiles/`: `generic` (fallback), `grounded`, `gta_v` (HUD regions,
wanted-level glyphs). See `docs/PROFILES.md`. The benchmark recording is HITMAN
(generic profile + content rules), per `docs/PLAN.md`.

## Configuration

`config/*.yaml`, validated by `backend/config/schema.py` (`extra="forbid"`).
Local overrides: `config/*.local.yaml` (gitignored). Every key must have a
consumer (`scripts/config_coverage.py`, enforced by a unit test).

The shipped YAML names the owner's Windows folders (`D:/Gaming 2026`,
`D:/Models/huggingface`). Since run `run-20261021-1509` the schema accepts
drive-rooted Windows paths on any host (BL-001); a directory key naming a
Windows drive on another host falls back to its default (BL-004).

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pip install numpy==2.2.6 opencv-python-headless==4.11.0.86 Pillow==12.2.0 scenedetect==0.7.1  # subset of [ai] used by the fast suite
.venv/bin/python -m pytest -m "not slow"   # fast loop
.venv/bin/ruff check .
```

Without `scenedetect` every pipeline integration test fails with a bare
`KeyError` on a later stage; the run header now names the missing package
(BL-017). Measured on the cloud runner (4 vCPU, 2026-10-22): the unit suite,
file by file, is 397 s in total; `test_edl_pipeline.py` alone is ~155 s.

Markers: `unit`, `integration`, `e2e`, `requires_ffmpeg`, `requires_gpu`,
`requires_models`, `requires_node`, `slow`. `ruff format` is **not** enforced
(193 files would be reformatted at `b034df8`).

## Rendering a sample

Through the UI/API only (`scripts/serve.py`, import → analyze → export), or the
`slow`-marked render tests (`tests/integration/test_render.py`), which pass on
Linux without a GPU (libx264). Overlays need `cd remotion && npm ci`. A
meaningful edit still needs Whisper and Ollama.

## Hardware assumptions

RTX 3070 8 GB, 32 GB RAM, i7, Windows, data on `D:`; `C:` is refused for
writes. One model resident at a time (`config/models.yaml` VRAM plan). NVENC
with libx264 fallback. The cloud runner used by this mission has **no GPU**,
no Ollama, no Whisper, and no general web access (search only).

## Known limitations (from PLAN.md, current at `b034df8`)

- P0.6 and P0.3 await the owner's viewing and the DATASET GATE (owner labels not yet recorded).
- Order from PLAN: P0.6 → P0.5 → P0.4 → P0.7 → P0.8.
