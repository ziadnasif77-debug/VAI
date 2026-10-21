# Research log (autonomous mission)

Chronological, one entry per run. Full reports live in `.ai-mission/reports/`.

## 2026-10-21 14:06 UTC — run-202610211406

Outcome: first mission run. Baseline on a Linux cloud container; three
defects fixed with regression tests (Windows paths in config validation,
broken NVENC→x264 proxy fallback, `repair_sequence` oscillation); left
uncommitted per `COMMIT_AUTHORIZED = false`.
Report: [.ai-mission/reports/2026-10-21_1406_run.md](.ai-mission/reports/2026-10-21_1406_run.md)

Network note: only github.com / raw.githubusercontent.com resolve from this
container; ffmpeg.org, docs.nvidia.com and docs.python.org do not. Primary
sources were read from their GitHub mirrors instead.

| Source | URL | Date / version | What it shows | Applicable |
| --- | --- | --- | --- | --- |
| x264 public header | https://raw.githubusercontent.com/mirror/x264/master/x264.h | master, read 2026-10-21 | Valid presets ultrafast…placebo (also numeric 0–9); no `p1`–`p7` | Yes — proves M-002 |
| FFmpeg `libavcodec/libx264.c` | https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavcodec/libx264.c | master, read 2026-10-21 | An unknown preset makes `x264_param_default_preset` fail → `AVERROR(EINVAL)` | Yes — the observed failure |
| FFmpeg `libavcodec/nvenc_h264.c` | https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavcodec/nvenc_h264.c | master, read 2026-10-21 | NVENC presets p1 (fastest) … p7 (slowest), default p4 "medium" | Yes — preset family check in M-002 |
| CPython `Doc/library/pathlib.rst` | https://raw.githubusercontent.com/python/cpython/main/Doc/library/pathlib.rst | main, read 2026-10-21 | `is_absolute` needs root and (if the flavour allows) a drive; `PureWindowsPath('c:/a/b').is_absolute()` is True on any host | Yes — basis of M-001 |
| FFmpeg `doc/filters.texi` (loudnorm) | https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/doc/filters.texi | master, read 2026-10-21 | Linear mode needs all four measured_* values, else dynamic; dynamic upsamples to 192 kHz unless -ar/aresample | Yes — M-004 hypothesis; project already sets `-ar` in encoder.py |
| faster-whisper releases | https://github.com/SYSTRAN/faster-whisper/releases | latest 1.2.1 (2025-10-31) | Project pin 1.2.1 is current; Silero VAD v6, batched timestamp fixes | Yes — no action |
| PySceneDetect releases | https://github.com/Breakthrough/PySceneDetect/releases | v0.7.2 (12 Oct) | Changed hist/hash defaults, AdaptiveDetector min_scene_len fix, NTSC fix, PyAV 19 | Partly — M-005 proposal (project uses ContentDetector) |

Licences: no code, model or dataset adopted this run.
