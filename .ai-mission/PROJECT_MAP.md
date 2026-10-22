# Project Map

Maintained by the scheduled engineering mission. The project's own
authoritative documents are `docs/PLAN.md` (read first), `docs/SPEC.md` (every
`§N` in the code), and `docs/ARCHITECTURE.md`. This file is a short index into
them plus what the mission verified on its own runner. Anything not checked by
the mission is marked *unverified*.

Last updated: 2026-10-22 (run-2026-10-22_0711).

## Languages and frameworks

- Python ≥3.10 backend (`backend/`, `ai/`): pydantic 2, FastAPI, uvicorn,
  PyYAML, SQLite. Analysis extra (`pyproject.toml [ai]`): faster-whisper,
  ctranslate2, scenedetect, easyocr, numpy, opencv-headless, Pillow, av,
  onnxruntime. torch (CUDA build) is assumed but not pinned in the manifest.
- TypeScript: `apps/web` (Vite UI), `apps/renderer`, `remotion/` (overlay
  compositions). Node 22 on the mission runner.
- FFmpeg/FFprobe on PATH (owner: `choco install ffmpeg-full`; runner: 6.1.1).
- Ollama (local VLM/LLM) -- model store outside the repo (`docs/SHARED_MODELS.md`).

## Entry points

- `python scripts/serve.py` -- API + job worker + built UI on :8765 (`VAI.bat`).
- `scripts/doctor.py` -- reports what the machine is missing.
- `scripts/db_init.py` -- create/migrate the database.
- `scripts/daily_cycle.py`, `scripts/run_inbox.py`, `scripts/run_real_source.py`
  -- unattended production paths (*unverified* on the runner).
- `scripts/evaluate.py`, `scripts/baseline.py`, `scripts/score_moments.py` --
  golden-set measurement (*unverified* on the runner).

## Pipeline (JobStage, `backend/core/models/enums.py`)

IMPORT → PROBE → PROXY → AUDIO → FRAMES → TRANSCRIPT → AUDIO_EVENTS → SCENES →
VISION → OCR → GAME_EVENTS → SEMANTIC → MOMENTS → STORY → EDL → CRITIQUE →
RENDER → QA → CRITIC2 (plus publish/metadata stages).

- Media engine: `backend/media/` (ffmpeg runner, probe, proxy, audio, frames, chunking).
- Analysis: `backend/analysis/`, `backend/gaming/` (OCR/HUD, content/exclusions),
  `backend/semantic/` (lanes), `backend/evidence/` (projection).
- Selection: `backend/moments/`, `backend/editorial/` (situations, jump cuts,
  doctrine), `backend/narrative/` (plans, judge), `backend/director/`.
- Timeline/EDL: `backend/timeline/` (validation incl. AuthorizedSpan), critic
  (`backend/critic/`), post-render critic (`backend/critic2/`).
- Rendering: `backend/rendering/` -- FFmpeg base pass → Remotion overlay
  (alpha WebM/ProRes) → FFmpeg composite + final encode; `audio_mix.py` builds
  the mix (amix → loudnorm single-pass → alimiter → fades).
- QA: `backend/qa/` (technical + content checks).
- Interaction / NL editing: `backend/interaction/` with LLM fallback.

## Models (where loaded)

- Speech: `ai/speech/faster_whisper_provider.py` (weights in `models/`).
- Vision / LLM: `ai/vision/ollama_provider.py`, `ai/llm/ollama_provider.py`.
- OCR: `ai/ocr/engines.py` (easyocr).
- Every provider has a `fake_provider.py` used by the tests.
- One model resident at a time on 8 GB VRAM (`docs/ARCHITECTURE.md`
  "Sequential model execution").

## Tests

- `pytest` (markers in `pyproject.toml`: unit, integration, e2e,
  requires_ffmpeg/gpu/models/node, slow). `pytest -m "not slow"` is the fast loop.
- `ruff check .` must be clean (it is, 2026-10-22). `ruff format` is **not**
  applied to the codebase -- do not reformat files wholesale.
- Owner's machine: 2911 passed, 5 skipped, ~45 min (PLAN.md, 2026-09-09).
- Mission runner (Linux, no GPU, no Remotion `node_modules`): see the latest
  report in `.ai-mission/reports/`.
- Runner setup used: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
  plus `numpy Pillow opencv-python-headless av scenedetect requests` at the
  manifest's pinned versions (the `[ai]` extra minus easyocr/faster-whisper).

## Games

`profiles/generic`, `profiles/gta_v`, `profiles/grounded`; HITMAN benchmark
referenced throughout PLAN.md (*profile location unverified*). Authoring guide:
`docs/PROFILES.md`.

## Configuration

`config/*.yaml` (13+ files) validated by `backend/config/schema.py`;
`config/*.local.yaml` overrides are git-ignored. Shipped values name the
owner's Windows drives (`D:/Gaming 2026`).

## Hardware assumptions

RTX 3070 8 GB, 32 GB RAM, i7; NVENC with libx264 fallback; Windows 11 is the
production machine. The mission runner has no GPU (`nvidia-smi` absent), so
GPU/NVENC benchmarks cannot run there.

## Known limitations (from PLAN.md, 2026-09-09 / 2026-10-18)

- P0.6 and P0.3 await the owner's viewing and the DATASET GATE (owner labels).
- Remotion overlay render crashed on 2 of 4 attempts on the owner's machine
  (`0xC0000409`); a single retry was added in `7e1e8b8`.
- P0.8: sequence repair oscillates between the variety and flatness rules.
- Mission runner network: only GitHub/package registries are reachable;
  ffmpeg.org, nvidia.com, ebu.ch, remotion.dev return 403 through the proxy.
