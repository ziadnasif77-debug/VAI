# Engineering Backlog (autonomous mission)

The owner's own phase plan is `docs/PLAN.md`; this backlog holds only what the
autonomous mission found or proposes. Evidence: `.ai-mission/reports/`,
`.ai-mission/SESSION_LEDGER.md`.

| ID | Pri | Title | Status | Evidence | Files | Next step |
| --- | --- | --- | --- | --- | --- | --- |
| M-1 | P0 | Shipped config loads on non-Windows (drive-letter paths) | Verified and implemented | report 2026-10-22 | `backend/config/schema.py` | — |
| M-2 | P0 | Drive-letter source root allows nothing off Windows | Verified and implemented | report 2026-10-22 | `backend/services/media_ingestion.py` | — |
| M-3 | P0 | Mix limiter ignored its ceiling (alimiter auto level) | Verified and implemented | report 2026-10-22 | `backend/rendering/audio_mix.py` | owner listens to one render to confirm |
| M-4 | P0 | Proxy libx264 fallback inherited NVENC preset `p4` → every import failed without NVENC | Verified and implemented | report 2026-10-22 | `backend/media/proxy.py` | — |
| M-5 | P1 | Programme loudness misses −14 LUFS on bursty game audio. Synthetic 90 s pink noise with 12 dB bursts: current single-pass → −17.1 LUFS; two-pass `linear=true` reverts to dynamic (−17.1 / −17.5) because loudnorm only goes linear when measured LRA ≤ target AND measured TP + gain ≤ target TP (`af_loudnorm.c` init); plain gain + limiter → −16.0 LUFS with 0.1 dBTP overshoot | Research hypothesis requiring an experiment | report 2026-10-22 | `backend/rendering/audio_mix.py`, `qa/technical.py` | On the target machine read QA's ebur128 integrated loudness for the last real renders. If they sit ≥2 LU under target, the choice is the owner's: accept quieter-but-dynamic, or add a gentle compressor (`acompressor`) before loudnorm and measure again |
| M-6 | P2 | `sfx.py` recipes use alimiter auto level (stingers normalised to 0 dBFS, limit not honoured) | Verified finding awaiting implementation | report 2026-10-22 | `backend/rendering/sfx.py` | Decide intended stinger peak; add `level=0` and re-tune gains, or document that 0 dBFS is intended |
| M-7 | P1 | Limiter is sample-peak, programme true peak still −0.7 dBTP vs −1 target | Verified finding awaiting implementation | report 2026-10-22 | `backend/rendering/audio_mix.py` | Set limiter ceiling at or below `true_peak_db` (or oversample) and test with ebur128 true peak |
| M-8 | P1 | Scene detection fails a job hard when PySceneDetect is missing (§95 says degrade) | Verified finding awaiting implementation | report 2026-10-22 | `backend/analysis/scenes.py` | Confirm whether failing is the owner's decision (docs/DECISIONS.md) before changing |
