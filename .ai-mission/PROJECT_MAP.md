# Project map (for the autonomous engineering mission)

Built 2026-10-21 from the repository at `b034df8`. The authoritative
running state is [`docs/PLAN.md`](../docs/PLAN.md); this file is an index for
unattended runs, not a replacement. Anything marked *unverified* was read in
docs but not exercised in a mission run.

## Stack

| Layer | What | Where |
| --- | --- | --- |
| Backend | Python ≥3.10 (pydantic 2, FastAPI, uvicorn, PyYAML, numpy) | `backend/`, `ai/` |
| UI | Vite web app (Node workspaces) | `apps/web` |
| Overlays | Remotion (Node) | `remotion/` |
| Encode | FFmpeg / ffprobe, `h264_nvenc` with `libx264` fallback | `backend/rendering/encoder.py` |
| Config | 18 YAML files validated by one schema | `config/*.yaml`, `backend/config/schema.py` |
| DB | SQLite with migrations | `backend/database/` |

## Entry points

- `python scripts/serve.py` — API + job worker + built UI on :8765 (`VAI.bat` on Windows).
- `scripts/doctor.py` (machine check), `scripts/db_init.py`, `scripts/run_real_source.py`, `scripts/daily_cycle.py`.

## Pipeline (JobStage order, `backend/core/models/enums.py`)

IMPORT → PROBE → PROXY → AUDIO → FRAMES → TRANSCRIPT → AUDIO_EVENTS → SCENES →
VISION → OCR → GAME_EVENTS → SEMANTIC → MOMENTS → STORY → EDL → CRITIQUE →
RENDER → QA → CRITIC2 (→ publish).

Analysis and editing are separate stores; an edit does not re-run analysis
(`tests/integration/test_analysis_reuse.py`).

## Models (`config/models.yaml`)

- ASR: faster-whisper `large-v3-turbo` (downloads into `models/`).
- Vision-language: Ollama `qwen2.5vl:7b`. Director/LLM: Ollama `qwen2.5:7b-instruct`.
- OCR: `en_PP-OCRv4` (onnxruntime); easyocr is also pinned in the `ai` extra.
- Models run sequentially to fit 8 GB VRAM (docs/ARCHITECTURE.md "Sequential model execution"). *unverified in a mission run.*

## Games

Profiles in `profiles/`: `gta_v`, `grounded`, and `generic` (fallback). The
P0 benchmark in PLAN.md is an 88.5-minute HITMAN recording on the owner's machine.

## Tests

- `pip install -e ".[dev]"` then `python -m pytest -m "not slow"`; full run is ~45 min on the owner's machine.
- Markers: `requires_ffmpeg`, `requires_gpu`, `requires_models`, `requires_node`, `slow`.
- `ruff check .` is clean at `b034df8`. `ruff format` is **not** used by the repo (do not reformat).
- `scripts/config_coverage.py`: every config key has a consumer (1022 keys, 0 orphans at `b034df8`).

## Hardware assumptions

Owner: Windows, RTX 3070 8 GB, 32 GB RAM, `C:` must never be written (<20 GB free),
media on `D:/Gaming 2026`. Cloud mission runs: Linux, 4 CPU, 15 GB RAM, **no GPU**,
ffmpeg present, outbound web fetch blocked except search.

## Known limitations found by the mission

- Before 2026-10-21 the shipped config (Windows `D:/` paths) failed validation on
  non-Windows hosts and numpy was not a core dependency, so the suite could not
  be collected on Linux. Fixed in BL-001/BL-002.
- Final renders carry no colour tags (BL-004).
