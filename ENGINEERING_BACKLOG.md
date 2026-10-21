# Engineering Backlog — autonomous mission

Durable backlog for the scheduled engineering mission. The product's own plan
and phase order live in [`docs/PLAN.md`](docs/PLAN.md); this file tracks what
the mission found, did and proposes. Status values: *Verified and implemented*,
*Implemented but not fully verified*, *Verified finding awaiting
implementation*, *Research hypothesis requiring an experiment*, *Blocked by
access, authorization, or infrastructure*, *in progress*.

Report for all entries below: [.ai-mission/reports/2026-10-21_1907_run.md](.ai-mission/reports/2026-10-21_1907_run.md)

| ID | Pri | Title | Status | Evidence | Affected files | Next step |
| --- | --- | --- | --- | --- | --- | --- |
| ENG-001 | P0 | Shipped config failed validation off Windows (`D:/…` read as relative): suite could not collect, app could not start | Verified and implemented (`c81dbf2`) | WI-2 | `backend/config/schema.py`, `tests/unit/test_config.py` | Owner: confirm the full suite on Windows (PLAN: 2911 green) |
| ENG-002 | P1 | Final-mix `alimiter` defaults: auto-level +0.3 dB (peaks −1.0→−0.6 dBFS, over −1 dBTP) and 5.0 ms audio delay vs picture | Verified and implemented (`d807227`) | WI-5 | `backend/rendering/audio_mix.py`, `tests/unit/test_rendering.py` | Owner: one real render, `ebur128` before/after |
| ENG-003 | P1 | Single-pass `loudnorm` misses −14 LUFS by 1.9–4.0 LU on bursty synthetic programmes; two-pass with measured values lands within 1.3–1.4 LU | Research hypothesis requiring an experiment | WI-6 | `backend/rendering/audio_mix.py`, render worker | Measure on one real mix on the owner machine (single vs two-pass I, extra wall time); implement only if the miss reproduces on real gameplay. Note: the fake-model e2e render measured −13.7 LUFS (on target) |
| ENG-004 | P2 | QA loudness only warns below −30 LUFS; a programme 4 LU under target passes silently | Verified finding awaiting implementation | WI-6 | `backend/qa/technical.py`, `QaThresholds` in `backend/config/schema.py` | Add an off-target warning (abs(I − target) > 2 LU; TP > target) fed from `audio.mix.normalization` |
| ENG-005 | P2 | PySceneDetect 0.7.2 (2026-10-12) fixes NTSC `min_scene_len` drops, PyAV 19, ThresholdDetector CEILING first-frame cut; project pins 0.7.1 | Verified finding awaiting implementation | WI-1 sources | `pyproject.toml` | Dependency bump needs owner approval; project passes `min_scene_len` as a timecode string, so #577 likely does not apply |
| ENG-006 | P2 | `scripts/rooted.py::shared_model_root` and `build_paths` disagree off Windows (`D:/Models/huggingface` → `ROOT/models` vs `ROOT/D:/Models/huggingface`); 2 `test_rooted` failures on Linux | Verified finding awaiting implementation | WI-7 | `scripts/rooted.py`, `backend/config/paths.py` | Decide one rule for drive paths on POSIX (likely: treat as unavailable → repo `models/` in both); low priority while the product runs on Windows only |
| ENG-007 | P1 | Project output folder: every POSIX absolute path rejected (judged by `PureWindowsPath` only) | Verified and implemented (`912a9fa`) | WI-3 | `backend/core/models/project.py`, `tests/unit/test_project_manager.py` | — |
| ENG-008 | P0 | libx264 proxy fallback received NVENC's `-preset p4` and failed every proxy on a machine without working NVENC (§95 fallback broken) | Verified and implemented (`38a2066`) | WI-4 | `backend/media/proxy.py`, `tests/unit/test_media.py` | — |
| ENG-009 | P1 | `backend/gaming/hud.py` imported `scipy`, declared in no manifest; glyph-row reading failed on a fresh install | Verified and implemented (`9872814`) | WI-4 | `backend/gaming/hud.py` | Consider a test that scans `backend/` imports against the manifests |
| ENG-010 | P2 | Remotion `TestAcceptance` lacked `requires_node` and errored (9) without `npm install` | Verified and implemented (`074ff5f`) | WI-7 | `tests/integration/test_remotion.py` | — |
| ENG-011 | P1 | GPU baseline (NVENC throughput, peak VRAM) | Blocked by access, authorization, or infrastructure | WI-8 | — | Needs the owner's RTX 3070; the cloud container has no GPU |
