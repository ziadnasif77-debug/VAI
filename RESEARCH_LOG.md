# Research log (autonomous mission)

Chronological, one entry per run. Entries older than 30 days move to
`RESEARCH_LOG_ARCHIVE.md`.

## 2026-10-21 18:07 UTC — run-20261021-1807

Outcome: first run. Made the suite runnable on Linux (config portability,
numpy declared, daily shelves guarded), took the unit baseline, measured
encode colour tags and loudnorm modes. [Report](.ai-mission/reports/2026-10-21_1807_run.md)

| Title | URL | Date / version | Demonstrates | Applicable |
| --- | --- | --- | --- | --- |
| FFmpeg loudnorm commit | https://ffmpeg.org/pipermail/ffmpeg-cvslog/2016-May/100083.html | 2016 | `measured_*` required for linear mode | yes (BL-005) |
| loudnorm two-pass doc patch | https://ffmpeg.org/pipermail/ffmpeg-devel/2018-August/233372.html | 2018 | two-pass workflow; 192 kHz note | yes (BL-005) |
| ffmpeg-user: linear fallback | https://ffmpeg.org/pipermail/ffmpeg-user/2022-March/054422.html | 2022 | fallback to dynamic when LRA/TP conditions fail | yes (BL-005) |
| YouTube upload encoding settings | https://support.google.com/youtube/answer/1722171 | current | 12 Mbps 1080p60, AAC 384k, BT.709 | yes (BL-004) |
| h264_nvenc spatial/temporal AQ patch | https://ffmpeg.org/pipermail/ffmpeg-devel/2020-May/261935.html | 2020 | AQ options exist, default off | hypothesis (BL-007) |
| PySceneDetect v0.7 migration guide | https://www.scenedetect.com/docs/0.7/api/migration_guide.html | 2026-05-03 | VFR timecodes, module moves | already pinned 0.7.1 |
| CPython pathlib docs | https://github.com/python/cpython/blob/ff420f0e08a2443339da0df7ace95e14177bac53/Doc/library/pathlib.rst | docs | `is_absolute` = drive + root | yes (BL-001) |
| PEP 631 | https://peps.python.org/pep-0631 | accepted | core vs optional dependencies | yes (BL-002) |

All read via search extracts; full-page fetches were blocked by the environment proxy.
