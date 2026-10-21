# Project map (maintained by the engineering mission)

Written 2026-10-21 from the repository at `b034df8`. The authoritative running
state is [`docs/PLAN.md`](../docs/PLAN.md) (read it first); this file is a
short index of it. Anything marked *unverified* was read in the docs and not
exercised in this environment.

## Stack
- **Backend:** Python ≥3.10 (`pyproject.toml`), pydantic 2, FastAPI/uvicorn, SQLite (`backend/database`, migrations under it), PyYAML config.
- **Analysis extras (`.[ai]`):** faster-whisper 1.2.1 / ctranslate2 4.8.1, scenedetect 0.7.1, easyocr 1.7.2, opencv-headless 4.11, PyAV 18, onnxruntime 1.27, numpy 2.2.6. torch is a CUDA build installed separately (*unverified here*).
- **Local models (Ollama, `config/models.yaml`):** vision `qwen2.5vl:7b` (~6 GB est.), reasoning `qwen2.5:7b-instruct` q4_K_M; ASR `large-v3-turbo`; OCR `paddleocr en_PP-OCRv4` on CPU, with EasyOCR and Tesseract engines in `ai/ocr/engines.py`.
- **Rendering:** FFmpeg (cut + concat + mix → base.mp4, NVENC preferred, libx264 fallback), Remotion (`remotion/`, transparent overlay of captions and graphics, rendered only where something is drawn), FFmpeg composite and final encode.
- **UI:** `apps/web` (Vite) served by the API; `apps/renderer`. TypeScript via npm workspaces.

## Entry points
- `python scripts/serve.py`: API, job worker and UI on http://127.0.0.1:8765. `VAI.bat` on Windows.
- `scripts/doctor.py` (machine check), `scripts/db_init.py`, `scripts/daily_cycle.py` (the owner's daily production policy), `scripts/run_real_source.py`, `scripts/score_moments.py --gate` (golden-label gate).

## Pipeline (JobStage order, `backend/core/models/enums.py`)
IMPORT → PROBE → PROXY → AUDIO → FRAMES → TRANSCRIPT → AUDIO_EVENTS → SCENES → VISION → OCR → GAME_EVENTS → SEMANTIC → MOMENTS → STORY → EDL → CRITIQUE → RENDER → QA → CRITIC2.
Workers live in `backend/pipeline/workers/`. Analysis results are cached by input digest, so an edit does not re-run analysis (§48).

| concern | where |
| --- | --- |
| media engine (probe, proxy, audio, frames) | `backend/media/` |
| gameplay understanding (OCR, HUD, events, menu/exclusion layer) | `backend/gaming/`, `backend/analysis/` |
| semantic lanes | `backend/semantic/` |
| moments and grants | `backend/moments/` |
| story, plans, judge | `backend/narrative/`, `backend/director/`, `backend/editorial/` |
| EDL, timeline, AuthorizedSpan | `backend/timeline/` |
| render, mix, encoder, Remotion | `backend/rendering/` |
| QA | `backend/qa/`, `backend/critic/`, `backend/critic2/` |
| natural-language editing | `backend/interaction/` |

## Games
Profiles in `profiles/`: `generic` (fallback), `grounded`, `gta_v`. The HITMAN benchmark (`proj-5db1780821a6`) uses the generic path plus content rules. Profile authoring: `docs/PROFILES.md`.

## Tests
- `pytest -m "not slow"` is the development loop; the full suite is 2,911 tests and about 45 min on the owner's Windows machine (PLAN.md).
- Markers: `requires_ffmpeg`, `requires_gpu`, `requires_models` (`VAI_TEST_MODELS=1`), `requires_node`, `slow`.
- `ruff check .` must be clean. `ruff format` is **not** enforced (HEAD is not formatted).
- Fixtures generate media with FFmpeg `testsrc`/`sine` (`tests/conftest.py`); golden data in `tests/golden/`.

## Hardware assumptions
RTX 3070 8 GB, 32 GB RAM, i7, Windows. The data root is the repository root, and nothing is written to C: (owner's rule). Configured folders are Windows paths (`D:/Gaming 2026`, `D:/Models/huggingface`).

## Governance (important for any automated change)
The owner authorises phases by name (`AUTHORIZED: P0.x` lines in PLAN.md). Current order: P0.6 → P0.5 → P0.4 → P0.7 → P0.8. P0.6 waits on the owner's viewing, and P0.3 and P0.6 also wait on the DATASET GATE (the owner's labels). P0.5 is authorised as a design report only. **Do not build editorial behaviour for an unauthorised phase.** Portability, fallback and test-infrastructure fixes are outside the phases.

## Known limitations (from PLAN.md, plus this mission's findings)
- The overlay renderer crashed in 2 of 4 attempts on the owner's machine; it is now retried once before the first frame (`7e1e8b8`).
- The sequence repair oscillates between the variety and flatness rules (left for P0.8).
- Single-pass `loudnorm` lands 3.3 LU under the −14 LUFS target on wide-dynamic-range synthetic audio. QA only checks a −30 LUFS floor (backlog VAI-M-004).
- `directories.models: D:/Models/huggingface` is created as a relative folder off Windows (backlog VAI-M-005).
