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

## 2026-10-21 15:45 UTC — owner follow-up run

**Outcome:** the owner delegated the next actions. Found that FFmpeg's
`alimiter` defaults to auto-level (ignoring its `limit`) and to no latency
compensation (5 ms audio delay); the mix now uses `level=0:latency=1` and a
−1.8 dBFS ceiling chosen by measurement, meeting −1 dBTP after AAC. HUD
reading no longer depends on undeclared scipy. `slow` suite green without a
GPU. Report: [2026-10-21_1545_run.md](.ai-mission/reports/2026-10-21_1545_run.md).

No new web sources: the evidence this run is the local FFmpeg 6.1.1 filter
help (`ffmpeg -h filter=alimiter`: `level` default true, `latency` default
false) and reproducible local measurements recorded in the report.

## 2026-10-21 21:07 UTC — run-20261021-2107

**Outcome:** consolidated the five parallel mission branches onto one
(`claude/great-feynman-vsj885`); the caption overlay is now rendered in BT.709
instead of Remotion 4's untagged BT.601 (BL-014, measured colour error fixed).
Report: [2026-10-21_2107_run.md](.ai-mission/reports/2026-10-21_2107_run.md).

| Title | URL | Date / version | What it demonstrates | Applicable |
| --- | --- | --- | --- | --- |
| YouTube recommended upload encoding settings | https://support.google.com/youtube/answer/1722171 | current (search summary only) | BT.709 for SDR; missing tags assumed BT.709 | Yes — target colour space |
| Remotion `options/color-space.tsx` | https://github.com/remotion-dev/remotion/blob/v4.0.409/packages/renderer/src/options/color-space.tsx | v4.0.409 (pinned) | v4 default `default`; `bt709` from v5; CLI flag `color-space` | Yes — BL-014 |
| Remotion `options/color-space.tsx` (main) | https://github.com/remotion-dev/remotion/blob/main/packages/renderer/src/options/color-space.tsx | main, 2026-10 | `bt601` = `default` since 4.0.424; conversion since 4.0.83 | Yes |
| Remotion `ffmpeg-args.ts` | https://github.com/remotion-dev/remotion/blob/v4.0.409/packages/renderer/src/ffmpeg-args.ts | v4.0.409 | `bt709` → zscale 709 + tags; default emits nothing | Yes — BL-014 |
| FFmpeg `libavcodec/nvenc.c` | https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/nvenc.c | master, 2026-10 | no read of `scenechange_threshold`; `no_scenecut` only with lookahead; idrPeriod = gopLength | Yes — BL-015 |

ffmpeg.org and remotion.dev did not resolve from this runner; GitHub raw did.
