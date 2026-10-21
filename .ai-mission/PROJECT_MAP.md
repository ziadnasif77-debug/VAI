# Project map — VAI (AI Gaming Video Editor)

Maintained by the autonomous engineering mission. The owner's own running
state is `docs/PLAN.md` ("the file to read first"); this map does not replace
it. Last updated: 2026-10-21 (first mission run).

## Languages and frameworks
- Python ≥3.10 backend (`backend/`, ~38k lines per PLAN.md), pydantic v2, FastAPI + uvicorn, SQLite with migrations.
- AI layer `ai/` (llm, ocr, providers, speech, vision). Optional extra `.[ai]` pins faster-whisper 1.2.1, ctranslate2 4.8.1, scenedetect 0.7.1, easyocr 1.7.2, onnxruntime 1.27.0, av 18, opencv 4.11.
- Overlay renderer: Remotion (`remotion/`, Node). UI: `apps/web` (Vite). Desktop/launcher: `VAI.bat`, `scripts/serve.py`.
- FFmpeg/ffprobe required (Phase 2+).

## Entry points
- `python scripts/serve.py` — API + job worker + built UI on 127.0.0.1:8765.
- `scripts/doctor.py` (what the machine lacks), `scripts/db_init.py`, `scripts/daily_cycle.py`, `scripts/run_inbox.py`, `scripts/package.py`.

## Pipeline stages (`backend/core/models/enums.py::JobStage`, workers in `backend/pipeline/workers/`)
IMPORT → PROBE → PROXY → AUDIO → FRAMES → TRANSCRIPT → AUDIO_EVENTS → SCENES → VISION → OCR → GAME_EVENTS → SEMANTIC → MOMENTS → STORY → EDL → CRITIQUE → RENDER → QA → CRITIC2 (plus publish/shorts).
- Analysis is stored per stage and keyed by input digests, so editing does not re-analyse (SPEC §48).
- Selection: `backend/narrative/optimizer.py` (knapsack + `repair_sequence`), story in `backend/narrative/story.py`, Director/Critic layers.
- EDL/timeline: `backend/timeline/` (validated, AuthorizedSpan grants per P0.3).
- Render: `backend/rendering/` (encoder.py NVENC/x264, audio_mix.py amix + single-pass loudnorm + alimiter), Remotion overlay pass.

## Models (`config/models.yaml`)
- ASR: faster-whisper `large-v3-turbo`. VLM: Ollama `qwen2.5vl:7b`. LLM: Ollama `qwen2.5:7b-instruct`. OCR: `en_PP-OCRv4`.
- VRAM plan: models released between stages; loads refused when the estimate exceeds usable VRAM (models.yaml §52/§53).

## Tests
- `python -m pytest -m "not slow"` fast loop; full run ~45 min on the owner's Windows machine (2911 passed / 5 skipped on 2026-09-09).
- `ruff check .` expected clean. Markers: unit, integration, e2e, requires_ffmpeg/gpu/models/node, slow.
- Linux cloud container: see the mission report of 2026-10-21 for which tests depend on the Windows host.

## Games
- Profiles in `profiles/`: `generic` (fallback), `grounded`, `gta_v`; HITMAN is the P0 benchmark recording (per PLAN.md). Authoring: `docs/PROFILES.md`, `scripts/profile_report.py`.

## Configuration
- 18 YAML files in `config/`, schema in `backend/config/schema.py`; shipped values name the owner's Windows drives (`D:/Gaming 2026`, `D:/Models`).

## Hardware assumptions
- RTX 3070 8 GB, 32 GB RAM, i7; everything written stays inside the repository (C: is refused as an output drive).

## Known limitations (from PLAN.md, unverified in this container)
- P0.6/P0.3 closures wait on the owner's viewing and the DATASET GATE (owner labels not recorded).
- Overlay renderer crash on 2/4 attempts on the owner's machine (one retry added in 7e1e8b8).
- No GPU in the cloud container: GPU, NVENC, Ollama and model tests cannot run here.
