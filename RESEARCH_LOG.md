# Research Log

Chronological, concise. Entries older than 30 days move to
`RESEARCH_LOG_ARCHIVE.md`. Full reports: `.ai-mission/reports/`.

## 2026-10-22 07:11 UTC — run-2026-10-22_0711

Outcome: first mission run. Baseline set; two import/portability defects and
one audio-mix defect fixed and pushed; loudnorm and NVENC findings recorded.
Report: [`.ai-mission/reports/2026-10-22_0711_run.md`](.ai-mission/reports/2026-10-22_0711_run.md)

Network note: only GitHub and package registries are reachable from the
runner; ffmpeg.org, nvidia.com, ebu.ch, remotion.dev and pytorch.org return 403.
FFmpeg documentation was therefore read from its source tree on GitHub.

| Title | URL | Date / version | What it demonstrates | Applicable? |
| --- | --- | --- | --- | --- |
| FFmpeg filters documentation, `loudnorm` section | https://github.com/FFmpeg/FFmpeg/blob/master/doc/filters.texi | master, read 2026-10-22 | Dynamic vs linear mode; linear needs `measured_I/LRA/TP/thresh` from a first pass; dynamic mode upsamples to 192 kHz and needs `-ar`/`aresample` | Yes -- P1-AUDIO-2; `-ar 48000` already set in `encoder.py` |
| FFmpeg `af_loudnorm.c` | https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/af_loudnorm.c | master | `linear` defaults to true but silently falls back to dynamic without measured values; input rate fixed at 192000 | Yes -- confirms single-pass in `audio_mix.py` is dynamic |
| FFmpeg filters documentation, `alimiter` section | https://github.com/FFmpeg/FFmpeg/blob/master/doc/filters.texi | master | `level` (auto level, default on) "normalizes audio back to 0dB"; `latency` compensates the look-ahead delay | Yes -- fixed in P1-AUDIO-1 (measured +0.30 dB and 5 ms) |
| FFmpeg `af_alimiter.c` | https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/af_alimiter.c | master | Option table: `level` default 1, `latency` default 0, `attack` default 5 ms | Yes -- same |
| FFmpeg `nvenc_h264.c` option table | https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/nvenc_h264.c | master | `tune` default hq; `spatial-aq`, `temporal-aq` default 0; `multipass` default disabled; `b_ref_mode` | Hypothesis P1-NVENC-1 (needs GPU) |
| x264 public header, preset names | https://github.com/mirror/x264/blob/master/x264.h | master (mirror) | Valid presets: ultrafast … placebo; no `p1`-`p7` | Yes -- basis of the P0-PROXY-1 mapping |
| FFmpeg Changelog | https://github.com/FFmpeg/FFmpeg/blob/master/Changelog | master | `loudnorm` and `alimiter` are long-standing filters (present since 3.x) | Yes -- the fix is safe on the owner's FFmpeg |
| Microsoft "The Old New Thing": STATUS_STACK_BUFFER_OVERRUN | https://devblogs.microsoft.com/oldnewthing/20190108-00/?p=100655 | 2019-01-08 (older than 12 months; search snippet only, page 403) | 0xC0000409 is a fast-fail termination, not necessarily a stack overrun | Context for P1-REMOTION-1 |

Licensing: no code, model or dataset was adopted; FFmpeg/x264 were only read.
