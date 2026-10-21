# Project Map — AI Gaming Video Editor (VAI)

Maintained by the autonomous engineering mission. First written 2026-10-21
(run `2026-10-21_1907`). The project's own source of truth is
[`docs/PLAN.md`](../docs/PLAN.md) (resume section, current phase) and
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); this file is an index into
them, not a replacement. Anything marked *unverified* was read from the docs
but not exercised by the mission.

## Languages and frameworks

| Area | Stack | Where |
| --- | --- | --- |
| Backend, pipeline, QA | Python ≥3.10 (pydantic 2, FastAPI, uvicorn, PyYAML) | `backend/`, `ai/`, `scripts/` |
| Analysis stack (optional extra `ai`) | faster-whisper 1.2.1, ctranslate2 4.8.1, scenedetect 0.7.1, easyocr 1.7.2, OpenCV 4.11, PyAV 18, onnxruntime 1.27 | `pyproject.toml` |
| Local LLM / VLM | Ollama: `qwen2.5vl:7b` (vision), `qwen2.5:7b-instruct` (interaction/director/critic) | `config/models.yaml` |
| Speech | faster-whisper `large-v3-turbo` | `config/models.yaml` |
| Overlay render | Remotion (Node ≥20, Chromium) | `remotion/` |
| UI | TypeScript, Vite (npm workspaces) | `apps/`, `package.json` |
| Media | FFmpeg / ffprobe on PATH; NVENC when available, libx264 fallback | `backend/media`, `backend/rendering/encoder.py` |
| Storage | SQLite (`*.db`, migrations) | `backend/database` |

## Entry points

- `python scripts/serve.py` — API + job worker + built UI on `127.0.0.1:8765` (`VAI.bat` on Windows).
- `scripts/doctor.py` — what the machine is missing; `scripts/db_init.py` — create/migrate DB.
- `scripts/run_real_source.py`, `scripts/run_inbox.py`, `scripts/daily_cycle.py` — unattended runs (*unverified*).

## Pipeline stages (raw footage → final video)

`backend/core/models/enums.py::JobStage`, workers in `backend/pipeline/workers/`:

`import → probe → proxy → audio → frames → transcript → audio_events → scenes →
vision → ocr → game_events → semantic → moments → story → edl → critique →
render → qa → critic2` (+ shorts, publish).

- **Understand**: `backend/media`, `backend/analysis` (scenes via PySceneDetect `ContentDetector`/`AdaptiveDetector`/`ThresholdDetector`, `backend/analysis/scenes.py`), `backend/gaming`, `ai/` (speech, vision, OCR, LLM providers with fakes for tests).
- **Decide**: `backend/moments`, `backend/narrative`, `backend/director`, `backend/semantic` (9-lane 2 Hz session timeline).
- **Describe**: `backend/timeline` (EDL), `backend/emphasis`, `backend/effects`.
- **Render**: `backend/rendering` — FFmpeg cut/concat/mix → `base.mp4`; Remotion overlay → `overlay.webm` (skipped when nothing to draw); FFmpeg composite/final encode. Audio mix graph: `backend/rendering/audio_mix.py` (amix → `loudnorm` single pass → `alimiter` → fades).
- **Verify**: `backend/qa/technical.py` (ffprobe + blackdetect/freezedetect/silencedetect/ebur128), `backend/critic`, `backend/critic2`.

Re-edit never re-analyses: change duration/mode/clip and only story → EDL → render → QA re-run (`backend/pipeline/reuse.py`).

## Models and VRAM

One model resident at a time (`gpu.exclusive_model_slot`, `gpu.empty_cache_between_stages`, `gpu.preflight_vram_check` in `config/models.yaml`). Vision only on candidate regions (`analysis.vision.only_candidate_regions`, `max_frames_per_source_hour`).

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -m "not slow"   # fast loop
.venv/bin/python -m pytest                 # full; PLAN.md quotes 2911 pass / 5 skip, ~45 min on the owner's Windows box
.venv/bin/ruff check .
```

Markers: `unit`, `integration`, `e2e`, `requires_ffmpeg`, `requires_gpu`, `requires_models` (`VAI_TEST_MODELS=1`), `requires_node`, `slow`. Media fixtures are generated with FFmpeg `testsrc`/`sine` (`tests/conftest.py`); golden projects in `tests/golden/`.
The repository does **not** use `ruff format` (HEAD is not formatted); only `ruff check` is enforced.

## Rendering a sample

*Unverified by the mission so far.* Via the UI/API (`scripts/serve.py`) or `scripts/run_real_source.py`. A full render needs ffmpeg, and Ollama/faster-whisper for real analysis; fakes exist for tests.

## Supported games

`profiles/generic`, `profiles/grounded`, `profiles/gta_v` — a profile declares HUD regions and reads (e.g. GTA V wanted stars). Without a profile: vision + OCR + audio + speech only. See `docs/PROFILES.md`.

## Configuration

18 YAML files in `config/` (`config/*.local.yaml` overrides, git-ignored), schema in `backend/config/schema.py` (`extra="forbid"`). The shipped files name the owner's Windows paths (`D:/Gaming 2026`, `.../Output`, `.../Ferdig`).

## Hardware assumptions

Target: RTX 3070 8 GB, 32 GB RAM, i7, Windows, system drive `C:` nearly full (nothing may be written there — enforced in config validators). The mission's cloud container has **no GPU**, 4 CPUs, 15 GB RAM, FFmpeg 6.1.1, Python 3.11, Linux.

## Known limitations (from docs and mission runs)

- Current phase per PLAN.md: P0.6 waiting on the owner watching a render and on the owner's dataset labels (DATASET GATE).
- Until run 2026-10-21 the shipped config failed validation on any non-Windows host (fixed, ENG-001).
- Single-pass `loudnorm` misses the −14 LUFS target by 2–4 LU on bursty synthetic programmes (ENG-003, open).
- Until run 2026-10-21 the libx264 proxy fallback received NVENC's `p4` preset and failed every import without NVENC (fixed, ENG-008); `scipy` was imported but undeclared (fixed, ENG-009).
- Off Windows, `scripts/rooted.py` and `backend/config/paths.py` resolve `D:/Models/huggingface` differently (ENG-006, open).
- Remotion tests and overlays need `npm install` in `remotion/`; the mission container does not have it.
