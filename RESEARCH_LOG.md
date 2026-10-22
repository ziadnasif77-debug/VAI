# Research Log (autonomous mission)

Chronological. Entries older than 30 days move to `RESEARCH_LOG_ARCHIVE.md`.

## 2026-10-22 06:07 Europe/Oslo — run-20261022-a
Outcome: four verified P0 fixes (config load off Windows, ingestion allowlist,
mix limiter ceiling, proxy software-fallback preset); loudness behaviour of
the mix characterised. Report: `.ai-mission/reports/2026-10-22_0607_run.md`.

| Title | URL | Date / version | What it shows | Applicable |
| --- | --- | --- | --- | --- |
| FFmpeg `af_alimiter.c` (release/6.1) | https://raw.githubusercontent.com/FFmpeg/FFmpeg/release/6.1/libavfilter/af_alimiter.c | FFmpeg 6.1 | `level` default 1; output `* (auto_level ? 1/limit : 1)` | Yes — root cause of M-3 |
| FFmpeg `doc/filters.texi` (master) — alimiter, loudnorm | https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/doc/filters.texi | master, read 2026-10-22 | alimiter "normalizes audio back to 0dB if enabled"; loudnorm dynamic mode upsamples to 192 kHz, linear needs measured_* and reverts to dynamic when LRA/TP conditions fail | Yes — M-3, M-5 |
| FFmpeg `af_loudnorm.c` (master) | https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavfilter/af_loudnorm.c | master, read 2026-10-22 | exact linear-mode conditions (`offset_tp <= target_tp && measured_lra <= target_lra`) | Yes — M-5 |
| FFmpeg doxygen 4.2 `af_alimiter.c` | https://ffmpeg.org/doxygen/4.2/af__alimiter_8c_source.html | FFmpeg 4.2 (2019) — older source | `level` option already present in 4.2 → `level=0` safe on every supported FFmpeg | Yes (search snippet only; host unreachable from here) |
| ffmpeg-devel: double-pass loudnorm example | https://ffmpeg.org/pipermail/ffmpeg-devel/2018-August/233372.html | 2018-08 — old | 192 kHz resampling outside linear mode; recommend `aresample` | Partly — the project already sets `-ar` (search snippet only; host blocked by proxy) |
| FFmpeg trac #10637 (loudnorm) | https://trac.ffmpeg.org/ticket/10637 | 2023–2024 | lead only, not read | Not assessed |

Licensing: no code, model or dataset adopted. FFmpeg is already a project dependency.
