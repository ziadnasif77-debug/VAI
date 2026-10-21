# Research log (autonomous mission)

Chronological, one entry per run. Full reports live in
[`.ai-mission/reports/`](.ai-mission/reports/). Entries older than 30 days move
to `RESEARCH_LOG_ARCHIVE.md`.

## 2026-10-21 15:09 UTC — run-20261021-1509

**Outcome:** first run. The fast suite could not even collect on Linux (shipped
config used Windows paths the schema judged relative); fixed. The NVENC→libx264
proxy fallback could never work (kept preset `p4`); fixed. QA now reads the
true peak it was already measuring. Shipped mix measured above its own −1 dBTP
target (backlog BL-006). Report:
[2026-10-21_1509_run.md](.ai-mission/reports/2026-10-21_1509_run.md).

Web access was limited to search-result summaries: every page fetch failed
(DNS / proxy 403 under this environment's network policy). The local FFmpeg
build's own filter documentation and local experiments were used as primary
evidence instead. No source below was read in full.

| Title | URL | Date / version | What it demonstrates | Applicable |
| --- | --- | --- | --- | --- |
| FFmpeg 6.1.1 `ffmpeg -h filter=ebur128` / `filter=loudnorm` (local binary) | local | 6.1.1-3ubuntu5 (2023) | `ebur128` `peak=true` = true-peak mode; `loudnorm` defaults I=−24, TP=−2, LRA=7, `linear=true` "if possible", two-pass via `measured_*` | Yes — basis of BL-005/BL-006 |
| FFmpeg-devel: ebur128 summary print patch | https://ffmpeg.org/pipermail/ffmpeg-devel/2015-March/170051.html | 2015 (old) | Summary layout: "True peak:" heading then "Peak: x dBFS" | Yes — parser shape; confirmed locally on 6.1.1 |
| FFmpeg-cvslog: f_ebur128 export results into read-only options | https://ffmpeg.org/pipermail/ffmpeg-cvslog/2023-May/137179.html | 2023-05 | Peak values become read-only options; max across channels | Partly — newer builds; parser does not depend on it |
| FFmpeg trac #9723 ebur128 peak=true broken channels | https://trac.ffmpeg.org/ticket/9723 | 2022, closed fixed | Old regression in peak=true | Low — fixed upstream |
| FFmpeg-user: loudnorm linear normalization | https://ffmpeg.org/pipermail/ffmpeg-user/2022-March/054422.html | 2022-03 | Linear mode silently falls back to dynamic when the targets cannot be met | Yes — BL-006 experiment design |
| Fora Soft: true peak and inter-sample overshoot (vendor) | https://www.forasoft.com/learn/audio-for-video/articles-audio/true-peak-dbtp-inter-sample | 2026-06-06 | Lossy codecs add true-peak overshoot; cites AES TD1008 (−1 dBTP at codec input; lower bitrates overshoot more) | Lead only (vendor, secondary) — matches local measurement (128k worse than 384k) |
