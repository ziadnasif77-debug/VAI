# Research log (autonomous mission)

Chronological, one entry per run. Entries older than 30 days move to
`RESEARCH_LOG_ARCHIVE.md`. Full reports live in `.ai-mission/reports/`.

## 2026-10-21 17:12 UTC — run-20261021-1712

**Outcome:** first run of the mission. Baseline: the suite could not be collected on Linux. Fixed three defects with regression tests (proxy fallback preset, config portability, maxrate rounding). Measured the single-pass loudnorm miss on synthetic audio. Report: [`.ai-mission/reports/2026-10-21_1712_run.md`](.ai-mission/reports/2026-10-21_1712_run.md).

| # | Source | URL | Date / version | Demonstrates | Applicable |
| --- | --- | --- | --- | --- | --- |
| 1 | FFmpeg `filters.texi` (loudnorm, ebur128) and `af_loudnorm.c` | https://github.com/FFmpeg/FFmpeg/blob/master/doc/filters.texi (mirror of https://ffmpeg.org/ffmpeg-filters.html#loudnorm, which the proxy blocked) | master, read 2026-10-21 | Linear mode needs all four measured values and falls back to dynamic if measured LRA > target LRA or the TP headroom is short. Dynamic mode uses a 3 s window and upsamples to 192 kHz. `print_format=json` reports `normalization_type`. | Yes: VAI-M-004. The 192 kHz output is already resampled by `-ar 48000` in `encoder.audio_arguments`. |
| 2 | FFmpeg `nvenc_h264.c` / `nvenc.c`, nv-codec-headers 13.1 | https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/nvenc_h264.c | master, read 2026-10-21 | p1–p7 (default p4); `spatial-aq` and `temporal-aq` off by default; `b_ref_mode`; unsupported options fail with ENOSYS rather than degrading. | Yes: VAI-M-006. Ampere support is **not** confirmed by NVIDIA docs (blocked). |
| 3 | faster-whisper releases | https://github.com/SYSTRAN/faster-whisper/releases | 1.2.1, 2025-10-31 (about 12 months old); MIT | Silero VAD v6, BatchedInferencePipeline; README: RTX 3070 Ti batch 8 fp16 6,090 MB, int8 4,500 MB. | The pin is current. Keep int8 on 8 GB. |
| 4 | PySceneDetect releases | https://github.com/Breakthrough/PySceneDetect/releases | 0.7.2, 2026-10-12; BSD-3 | Histogram detector 0.05→0.20 and hash detector 0.395→0.35 default changes; `framerate` alias deprecated. | VAI-M-008 |
| 5 | EasyOCR / RapidOCR / PaddleOCR on PyPI | https://pypi.org/project/rapidocr/ , https://pypi.org/project/easyocr/ | RapidOCR 3.10.0 (2026-10-08), EasyOCR 1.7.2 (2024-09, older than 12 months), PaddleOCR 3.7.0 (2026-06); all Apache-2.0 | RapidOCR runs PP-OCR models on onnxruntime with no torch. | VAI-M-007 (new dependency, proposal only) |
| 6 | Remotion docs and LICENSE.md | https://github.com/remotion-dev/remotion/tree/main/packages/docs | 4.0.537 (2026-10-19) | "Target closed" is a tab OOM/CPU crash (lower concurrency); no built-in retry; frame cache up to 50 % RAM; license free for ≤3 employees. | Consistent with the existing retry (`7e1e8b8`) and VRAM fitting. Licence matches PLAN.md §8. |

Primary pages on ffmpeg.org, nvidia.com and remotion.dev were refused by this environment's network proxy; the same content was read from the projects' source repositories instead.
