# Engineering Backlog

Maintained by the scheduled engineering mission. Project phases and the owner's
gates live in `docs/PLAN.md`; this backlog tracks the mission's own findings.
Statuses: Verified and implemented · Implemented but not fully verified ·
Verified finding awaiting implementation · Research hypothesis requiring an
experiment · Blocked by access, authorization, or infrastructure · in progress.

Ledger: `.ai-mission/SESSION_LEDGER.md`. Reports: `.ai-mission/reports/`.

## P0

| ID | Title | Status | Evidence | Files | Next step |
| --- | --- | --- | --- | --- | --- |
| P0-PORT-1 | Shipped config loads on a POSIX checkout | Verified and implemented (78c4b81) | report 2026-10-22_0711 item 2 | `backend/config/schema.py` | -- |
| P0-PROXY-1 | Software proxy fallback received NVENC preset `p4`; every import failed without NVENC | Verified and implemented (9d297f7) | report 2026-10-22_0711 item 3 | `backend/media/proxy.py` | -- |
| P0-SUITE-1 | Remaining failures of `pytest -m "not slow"` on the Linux runner | Verified finding awaiting implementation | report 2026-10-22_0711 item 6 | see report | Triage each failing test: environment (missing easyocr/faster-whisper/Remotion, Windows-only paths) vs real defect; fix real defects first |

## P1

| ID | Title | Status | Evidence | Files | Next step |
| --- | --- | --- | --- | --- | --- |
| P1-AUDIO-1 | alimiter auto-level (+0.3 dB) and 5 ms look-ahead delay | Verified and implemented (55ee08e) | report 2026-10-22_0711 item 4 | `backend/rendering/audio_mix.py` | -- |
| P1-AUDIO-2 | Single-pass loudnorm runs in dynamic mode: misses the target and reshapes dynamics | Verified finding awaiting implementation | Synthetic 60 s programme: single-pass LRA 7.9 → 18.0 LU, two-pass linear 7.9 → 8.0 LU; 36 s integration render measures -13.0 LUFS vs -14 target | `backend/rendering/audio_mix.py`, render worker | Add a measurement pass (`loudnorm print_format=json`) over the mixed programme, feed `measured_*` + `linear=true` to the final pass; keep single-pass as the fallback when measurement fails; test with ebur128 on a synthetic programme (target ±0.5 LU) |
| P1-NVENC-1 | NVENC final encode runs without spatial/temporal AQ or multipass | Research hypothesis requiring an experiment | FFmpeg `libavcodec/nvenc_h264.c`: `spatial-aq`, `temporal-aq` default 0, `multipass` default disabled; `config/rendering.yaml` sets none | `backend/rendering/encoder.py`, `config/rendering.yaml` | On the RTX 3070: encode the same 60 s gameplay base with/without `-spatial-aq 1 -temporal-aq 1 -multipass qres`, compare VMAF/size/time (needs a VMAF-enabled FFmpeg). Not runnable on the mission runner (no GPU) |
| P1-REMOTION-1 | Overlay render crashes intermittently on Windows (`0xC0000409`) | Blocked by access, authorization, or infrastructure | PLAN.md P0.6; retry added in 7e1e8b8; 0xC0000409 is a fast-fail, not a real stack overrun (Microsoft Old New Thing) | `backend/rendering/remotion.py` | Needs the owner's machine: capture the faulting module from the Windows Application event log on the next crash |

## P2

| ID | Title | Status | Evidence | Files | Next step |
| --- | --- | --- | --- | --- | --- |
| P2-RUNNER-1 | Mission runner cannot exercise Remotion, GPU, OCR or Whisper paths | Blocked by access, authorization, or infrastructure | no `nvidia-smi`; `remotion/node_modules` absent; easyocr/faster-whisper not installed (NEW_DEPENDENCY_ALLOWED=false) | -- | Owner decision: allow `npm ci` in `remotion/` on the runner (already a manifest dependency) |
| P2-DOCS-1 | Research sources outside GitHub unreachable from the runner | Blocked by access, authorization, or infrastructure | ffmpeg.org, nvidia.com, ebu.ch, remotion.dev, pytorch.org → proxy 403 | -- | Owner may widen the environment's network policy |
