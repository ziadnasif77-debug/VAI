# Research log (autonomous mission)

Chronological, one entry per run. Entries older than 30 days move to
`RESEARCH_LOG_ARCHIVE.md`. Full reports live in `.ai-mission/reports/`.

## 2026-10-21 20:07 UTC — run-20261021-2007

**Outcome:** first run. Baseline taken on POSIX; three audio/proxy defects fixed with tests (proxy software fallback, master limiter auto-level, unconsumed clipping config), one config message bug fixed. Report: [`.ai-mission/reports/2026-10-21_2007_run.md`](.ai-mission/reports/2026-10-21_2007_run.md).

| Title | URL | Date / version | What it demonstrates | Applicable |
| --- | --- | --- | --- | --- |
| FFmpeg filters documentation (`doc/filters.texi`: loudnorm, ebur128, alimiter) | https://github.com/FFmpeg/FFmpeg/blob/master/doc/filters.texi | master, fetched 2026-10-21 | alimiter `level` "Auto level output signal. Default is enabled. This normalizes audio back to 0dB"; loudnorm dynamic mode upsamples to 192 kHz, linear mode needs `measured_*`; ebur128 summary prints `Peak:` under "True peak" | Yes: AM-002, AM-003, AM-009 |
| FFmpeg `libavfilter/af_alimiter.c` | https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/af_alimiter.c | master | `level` default 1; `double level = s->auto_level ? 1 / limit : 1;` — output scaled by 1/limit | Yes: AM-002, AM-008 |
| FFmpeg `libavcodec/libx264.c` | https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/libx264.c | master | `-preset` goes to `x264_param_default_preset`; an unknown name logs "Error setting preset/tune" and returns EINVAL | Yes: AM-001 |
| FFmpeg Changelog | https://github.com/FFmpeg/FFmpeg/blob/master/Changelog | master (9.0 listed) | alimiter has existed since 3.0 | Context |
| NVENC Preset Migration Guide (search summary; docs.nvidia.com not resolvable from this container) | https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/nvenc-preset-migration-guide/index.html | SDK 13.1 | P1 (fastest) … P7 (best quality) per tuning info; presets are NVENC-only names | Yes: AM-001 (only snippets read) |
| PySceneDetect releases | https://github.com/Breakthrough/PySceneDetect/releases | v0.7.2 (12 Oct, year not shown) | 0.7.2 changes `detect-hist`/`detect-hash` defaults, fixes PyAV 19, Windows OpenCV 5.0 exit segfault; project pins 0.7.1 + av 18 | Watch: upgrade changes detector output |
| faster-whisper releases | https://github.com/SYSTRAN/faster-whisper/releases | 1.2.1 is latest | No release newer than the pinned 1.2.1 | No action |
| baken — true-peak ceiling notes (secondary source quoting AES TD1008) | https://raw.githubusercontent.com/M-Igashi/baken/main/docs/true-peak-ceiling.md | undated | Quotes TD1008 §4: max true peak not to exceed -1 dBTP at lossy codec input; overshoot grows as bitrate falls | Hypothesis: AM-007 (verify against TD1008 itself) |

Licensing: nothing adopted. FFmpeg docs and source were read only (LGPL/GPL); no code copied.
