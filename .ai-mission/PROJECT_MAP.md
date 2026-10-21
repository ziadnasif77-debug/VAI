# Project map (maintained by the autonomous engineering mission)

Built 2026-10-21 from the source tree at `b034df8`. The project's own source of
truth is [`docs/PLAN.md`](../docs/PLAN.md) (read it first) and
[`docs/SPEC.md`](../docs/SPEC.md) (every `§N` in the code). This file is a map,
not a replacement. Anything marked *unverified* was read in docs, not run.

## Stack
- **Backend:** Python ≥3.10 (`backend/`, `ai/`), FastAPI + uvicorn, pydantic v2, SQLite (`backend/database`, migrations), PyYAML config.
- **Analysis extras (`pip install -e ".[ai]"`):** faster-whisper 1.2.1 / CTranslate2 4.8.1, PySceneDetect 0.7.1, EasyOCR 1.7.2, OpenCV-headless 4.11, PyAV 18, onnxruntime 1.27, numpy 2.2.6.
- **Models (config/models.yaml):** speech `large-v3-turbo` (faster-whisper), vision `qwen2.5vl:7b` and LLM `qwen2.5:7b-instruct` via Ollama, OCR `en_PP-OCRv4`. VRAM is used strictly sequentially (PLAN change log 2026-08-10). *Model behaviour unverified here: no GPU, no Ollama in this container.*
- **Rendering:** FFmpeg (NVENC `h264_nvenc` with libx264 fallback) + a Remotion overlay project (`remotion/`, Node ≥20).
- **UI:** `apps/web` (Vite/TypeScript), served by the API.

## Entry points
- `python scripts/serve.py` — API + job worker + built UI on :8765 (`VAI.bat` on Windows).
- `scripts/doctor.py` (what the machine lacks), `scripts/db_init.py`, `scripts/daily_cycle.py` (daily production), `scripts/run_real_source.py`, `scripts/evaluate.py` / `baseline.py` (golden-set metrics), `scripts/config_coverage.py` (orphaned config keys).

## Pipeline (`backend/core/models/enums.py::JobStage`)
IMPORT → PROBE → PROXY → AUDIO → FRAMES → TRANSCRIPT → AUDIO_EVENTS → SCENES → VISION → OCR → GAME_EVENTS → SEMANTIC → MOMENTS → STORY → EDL → CRITIQUE → RENDER → QA → CRITIC2 (+ SHORTS, PUBLISH as delivery stages).
- Media: `backend/media` (probe, proxy, audio, frames, chunking).
- Understanding: `backend/analysis`, `backend/gaming` (HUD/OCR content rules, exclusions), `backend/semantic` (2 Hz lanes), `backend/evidence`.
- Selection/story: `backend/moments`, `backend/narrative`, `backend/director`, `backend/editorial` (jump cuts, situations, doctrine).
- Timeline: `backend/timeline` (EDL, AuthorizedSpan validation), `backend/critic`, `backend/critic2`.
- Render: `backend/rendering` (encoder, audio_mix, composite, remotion, shorts, sfx), `backend/audio_director`.
- QA: `backend/qa` (technical: decode, black/freeze, A/V sync, loudness, **true peak (added 2026-10-21)**; content checks).

## Games / profiles
`profiles/generic`, `profiles/grounded`, `profiles/gta_v`; a deterministic game recogniser picks one (PLAN 2026-08-15). The benchmark recording is HITMAN (PLAN §2) — *its profile handling unverified here*.

## Configuration
`config/*.yaml` (18 files), validated by `backend/config/schema.py`; overrides by `VAI__SECTION__KEY` env vars and gitignored `config/*.local.yaml`.
**Shipped config contains Windows drive paths** (`D:/Gaming 2026`, `application.media_source_roots`, `daily.output_directory`, `daily.archive_directory`); on Linux/macOS they fail validation. To run the suite on POSIX, override them (see "Tests").

## Tests
- `pytest` (markers: unit, integration, e2e, requires_ffmpeg, requires_gpu, requires_models, requires_node, slow). PLAN: 2911 passed, 5 skipped on the owner's Windows machine (45 min).
- POSIX container recipe used by the mission:
  ```bash
  python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" numpy==2.2.6 scenedetect==0.7.1 \
      opencv-python-headless==4.11.0.86 Pillow==12.2.0 av==18.0.0 arabic-reshaper==3.0.1 python-bidi==0.6.11
  export VAI__APPLICATION__MEDIA_SOURCE_ROOTS='["/abs/media"]' \
         VAI__DAILY__OUTPUT_DIRECTORY=/abs/out VAI__DAILY__ARCHIVE_DIRECTORY=/abs/arch
  .venv/bin/python -m pytest -m "not slow"
  ```
  `numpy` is imported by core modules but only declared in the `[ai]` extra, so `.[dev]` alone fails collection.
- `ruff check .` must be clean (it is). `ruff format` is not enforced (many files would reformat).

## Hardware assumptions
RTX 3070 8 GB, 32 GB RAM, i7, Windows, data on `D:` (nothing written to `C:`). This container: 4 vCPU, no GPU (`libcuda.so.1` missing → NVENC unavailable), FFmpeg 6.1.1.

## Known limitations (from PLAN, not re-verified)
- P0.6 and P0.3 await the owner's viewing and the DATASET GATE; phase order P0.6 → P0.5 → P0.4 → P0.7 → P0.8 is the owner's.
- Overlay renderer crashed early on 2 of 4 Windows attempts (retried once since `7e1e8b8`).
- Sequence repair oscillates between variety and flatness rules (left for P0.8).
