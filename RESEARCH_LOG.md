# Research Log — autonomous engineering mission

Chronological, concise. Entries older than 30 days move to
`RESEARCH_LOG_ARCHIVE.md`. Full reports: `.ai-mission/reports/`.

---

## 2026-10-21 19:07 UTC — run `2026-10-21_1907`

**Outcome:** first mission run. Four portability/correctness defects fixed with
red→green tests (config paths on non-Windows hosts, NVENC preset leaking into
the libx264 proxy fallback, undeclared `scipy` in the HUD reader, `alimiter`
defaults re-gaining and delaying the final mix); single-pass `loudnorm`
target miss measured and logged as a hypothesis.
Report: [.ai-mission/reports/2026-10-21_1907_run.md](.ai-mission/reports/2026-10-21_1907_run.md)

| # | Source | URL | Date / version | What it demonstrates | Applicable |
| --- | --- | --- | --- | --- | --- |
| S1 | FFmpeg `doc/filters.texi` (alimiter, loudnorm sections) | https://github.com/FFmpeg/FFmpeg/blob/master/doc/filters.texi | master, read 2026-10-21 | alimiter: delay equals attack time, `level` auto-level "normalizes audio back to 0dB" (default on), `latency` compensates the delay; loudnorm: dynamic mode upsamples to 192 kHz, linear mode needs all measured_* and reverts to dynamic when target LRA < source LRA or TP would exceed target | Yes — `backend/rendering/audio_mix.py` (ENG-002, ENG-003) |
| S2 | FFmpeg `libavfilter/af_alimiter.c` | https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/af_alimiter.c | master | `level = auto_level ? 1/limit : 1` (line 139): output gained by 1/limit; `level` default 1, `latency` default 0 | Yes — explains the measured +0.3 dB (ENG-002) |
| S3 | FFmpeg `libavfilter/af_loudnorm.c` | https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/af_loudnorm.c | master | Frame-type state machine; LINEAR_MODE only when measured values allow; 192 kHz input rate in dynamic mode | Yes (ENG-003) |
| S4 | ffmpeg-normalize `_streams.py` + LICENSE | https://github.com/slhck/ffmpeg-normalize | v1.42.0 (README), MIT | Two-pass EBU R128 by default; predicts fallback to dynamic when `input_tp + (target − input_i) > TP` and tells the user the max reachable target | Pattern only (no dependency): ENG-003 design |
| S5 | faster-whisper releases | https://github.com/SYSTRAN/faster-whisper/releases | 1.2.1 is latest (31 Oct, year not shown on page) | 1.2.1: Silero VAD v6, batched pipeline fixes, `<|nocaptions|>` suppressed | Project already pins 1.2.1 — no action |
| S6 | PySceneDetect releases | https://github.com/Breakthrough/PySceneDetect/releases | v0.7.2, 12 Oct 2026; v0.7.1, 22 Jul; v0.7, 3 May | 0.7.2: NTSC `min_scene_len` cut-drop fix (#577), PyAV 19, ThresholdDetector CEILING first-frame fix, Histogram/Hash default changes | Partly — project pins 0.7.1 and passes `min_scene_len` as a timecode (ENG-005) |
| S7 | ffmpeg-user thread "loudnorm - linear normalization" (via search summary only; ffmpeg.org blocked) | https://ffmpeg.org/pipermail/ffmpeg-user/2022-March/054422.html | 2022-03 (old) | Users rarely get linear mode when TP/LRA constraints bind | Context only; superseded by S1/S3 |

Network note: `ffmpeg.org` and `newreleases.io` are blocked by this
environment's network policy; FFmpeg docs were read from the GitHub mirror.
