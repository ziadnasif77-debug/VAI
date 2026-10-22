# Project map (mission view)

Authoritative project documentation is `docs/PLAN.md` (resume here),
`docs/SPEC.md` (every `§N` in code), `docs/ARCHITECTURE.md`. This file is the
mission's short index into them. Last verified: 2026-10-22, run-20261022-a.

## Stack
- Backend: Python ≥3.10 (`backend/`, `ai/`), FastAPI + uvicorn, SQLite (WAL),
  pydantic config typed from `config/*.yaml` (`backend/config/schema.py`).
- Overlays: Remotion (`remotion/`, Node 22). UI: `apps/web` (Vite), `apps/renderer`.
- Media: FFmpeg/FFprobe via `backend/media/ffmpeg.py` (process layer).
- Models (target machine, unverified here): faster-whisper (ASR, `models/`),
  Ollama `qwen2.5vl:7b` (vision) and `qwen2.5:7b-instruct` (LLM), EasyOCR /
  PaddleOCR (OCR), PySceneDetect. Loaded through factories in `ai/*/`.

## Entry points
- `python scripts/serve.py` (API + worker + UI, port 8765); `VAI.bat` on Windows.
- `scripts/run_real_source.py <file>` — one recording end to end with stage costs.
- `scripts/doctor.py` — what the machine is missing.

## Pipeline (job stages, `backend/pipeline/runner.py`)
ingest/probe → proxy (`media/proxy.py`) → audio extract → transcript (ASR)
→ audio events / reactions → scenes + frames → vision (VLM) → OCR / HUD
→ game events + correlation (`gaming/`) → semantic timeline (`semantic/`)
→ moments (`moments/`) → narrative + Director (`narrative/`, `director/`)
→ EDL / timeline (`timeline/`) → Critic (`critic/`) → overlay (Remotion)
→ final render + audio mix (`rendering/encoder.py`, `rendering/audio_mix.py`)
→ QA (`qa/`) → CRITIC2 post-render (`critic2/`) → publish (`publishing/`).

## Tests
- `python -m pytest -m "not slow"` (dev loop); full suite incl. slow ≈45 min.
- `ruff check .` must be clean. Marker `requires_ffmpeg`, `requires_models` etc.
- On Linux the `ai` extras minus `easyocr` are enough for the non-slow suite.

## Games
Game profiles in `profiles/` (HITMAN, GTA, generic fallback); see `docs/PROFILES.md`.

## Hardware assumptions
Target: Windows, RTX 3070 8 GB, 32 GB RAM, i7. NVENC p4 for proxy and final.
This mission's container: Linux, 4 vCPU, no GPU — NVENC/VRAM unmeasurable here.

## Known limitations (from this run)
- Single-pass `loudnorm` misses −14 LUFS on bursty material (backlog M-5).
- `sfx.py` stinger recipes use alimiter auto level (backlog M-6).
