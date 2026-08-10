# Phase 2 — Media Engine

SPEC §99, §126 steps 05–06. **Acceptance: a long recording is probed, proxied,
audio-extracted and frame-sampled without loading the file into RAM.**

Status: **complete and verified on the target machine.** 515 tests pass,
`ruff check` is clean, and the §7 acceptance criterion was measured rather than
asserted: over a source **150× longer**, peak memory moved **+0.2 MB**.

Verified on Windows 11 with FFmpeg 9.0 (gyan.dev full build) and an
RTX 3070 — the machine this product targets, not a Linux CI box.

---

## Delivered

| §99 requirement | Where | Verified by |
| --- | --- | --- |
| FFmpeg/FFprobe integration | `backend/media/ffmpeg.py` | `test_media.py` (parsing, typing) · `test_media_engine.py` (real runs) |
| Media probing | `backend/media/probe.py` | `TestProbe` — 8 tests |
| Proxy generation (§55) | `backend/media/proxy.py` | `TestProxy` — 6 tests |
| Audio extraction (§18, §19) | `backend/media/audio.py` | `TestAudioExtraction` — 4 tests |
| Frame extraction (§16) | `backend/media/frames.py` | `TestFrameExtraction` — 4 tests |
| Chunked processing (§7) | `backend/media/chunking.py` | `TestChunking` — 11 tests |
| Stage workers | `backend/pipeline/workers/` | `TestPipelineEndToEnd` — 8 tests |
| Runner (§46, §47, §81, §82) | `backend/pipeline/runner.py` | end-to-end + cancellation tests |
| Persistence for probe and frame output | `repositories/media.py`, `repositories/frames.py` | end-to-end tests |

`IMPORT → PROBE → PROXY → AUDIO → FRAMES` now runs end to end. `TRANSCRIPT`
onward have no workers yet; the runner stops there and says so, rather than
failing a stage that was never built.

---

## The decisions worth knowing

### Frame budgets widen the interval, they never truncate the list

§16 sets `max_frames_per_hour`. The obvious implementation — plan the
timestamps, then cut the list at the ceiling — samples the first forty minutes
of a two-hour recording densely and abandons the rest. The ceiling therefore
*widens the interval* instead, and coverage stays uniform to the last second.
`SamplingPlan.was_capped` reports when it happened, so a stage logs that it
sampled more sparsely than configured instead of doing so quietly.

### Frame rates are parsed as rationals

ffprobe reports `60000/1001`, not `59.94`, because 59.94 is not representable.
`float("60000/1001")` raises and rounding to 60 loses seven seconds of
alignment over a two-hour recording — enough to put a moment's timestamp on its
neighbour. `parse_rational` handles the ratio, and `0/0` means *unknown* and
returns `None` rather than zero.

### The proxy is built in resumable segments

A two-hour proxy takes minutes. Building it as one FFmpeg run means a crash, a
cancellation or a closed lid at ninety minutes throws away ninety minutes. It
is built in ten-minute segments instead, each written to a temporary name and
moved into place only when complete, then concatenated with stream copy. That
buys three things at once: resume (§47), a checkpoint where cancellation can
stop cleanly (§82), and progress measured against the source timeline (§60).

The assembled proxy is then probed and its duration compared with the source.
A proxy short by one segment still *plays* — and every timestamp after the gap
would be wrong, so every moment derived from it would point at the wrong part
of the recording. That failure would only surface in the finished video, which
is why the check is not optional.

### Audio is extracted per track, not per file

§19: game audio and a player's microphone carry very different semantic weight.
A recording that kept them on separate streams gets one analysis file per
stream. Mixing them down first would destroy exactly the distinction the moment
detector depends on. The primary keeps a fixed name; the rest carry their
source stream index, so a result stays traceable to the microphone.

Audio comes from the **source**, not the proxy — the proxy's audio has been
through a lossy encode at preview bitrate. Frames come from the **proxy** —
decoding 720p30 rather than 1080p60 for pictures that only feed detectors.

### Both process pipes are drained

Reading `-progress` from stdout while ignoring stderr is the classic deadlock:
the unread pipe fills its OS buffer and FFmpeg blocks mid-encode. Both streams
are drained by reader threads and the control loop consumes a queue, which also
gives a steady half-second tick for the timeout and cancellation checks even
when FFmpeg emits nothing at all.

### Progress is throttled before it reaches the database

A worker reports every half-second; a two-hour proxy is about 14 000 reports.
Written straight through, a progress bar becomes thousands of transactions. The
runner writes on 1 % movement or two seconds, whichever comes first — and
always writes the terminal update, so a finished stage never shows 98 %.

---

## Acceptance

`tests/integration/test_long_source.py` tests §7 by **measuring memory**, not by
reading the code.

The same pipeline runs over a six-second source and a fifteen-minute source —
150× the length — each in a fresh subprocess, and the peak working set must
barely move. An implementation that read a recording into memory would fail
this by a factor of hundreds at two hours; one that streams shows a flat line,
which is the property §7 actually asks for.

A comparative test rather than an absolute threshold, because a threshold is
something somebody eventually tunes until it passes.

The companion tests confirm the whole chain runs over a multi-segment source:
three proxy segments concatenated and verified, audio extracted, 300 frames
sampled at the configured 3-second interval.

Marked `slow` — it transcodes a quarter of an hour of video:

```bash
python -m pytest -m slow -v
```

---

## Verification status

| Check | Result |
| --- | --- |
| `pytest` | **515 passed**, 0 skipped |
| `ruff check .` | clean |
| §7 memory acceptance | **passed** — +0.2 MB peak across a 150× length increase |
| Real decode, proxy, audio, frames | passed — 32 tests against FFmpeg 9.0 |
| Windows portability | 3 pre-existing tests fixed (see below) |

The measured result:

```
§7 peak working set
    6.0s source: 39.5 MB
  900.0s source: 39.6 MB
  150x the length, +0.2 MB peak
```

The full run takes about five minutes because it transcodes fifteen minutes of
video. For the development loop:

```bash
python -m pytest -m "not slow" -q
```

Tests that decode a file are marked `requires_ffmpeg` and skip — never silently
pass — through a hook in `tests/conftest.py` on a machine without FFmpeg.

### Three Windows failures fixed

The Phase 1 suite was green on Linux and red on Windows:

* Two tests passed `"/nowhere/clip.mp4"` as an absolute path. On Windows that
  is *drive-relative*, so `Path.is_absolute()` is false and ingestion rejected
  it as a bad path before the existence check ran. They now derive the path
  from `tmp_path`. The production behaviour was already right.
* `test_missing_gpu_does_not_fail_the_report` asserted the *overall* health
  status was ok or warning. On a machine without FFmpeg the report is correctly
  `failed`, and the test was conflating "GPU absence doesn't break the report"
  with "this machine is healthy". It now asserts what it means: GPU and NVENC
  never appear among blocking failures.

---

## Test fixtures

No media is committed. Clips are generated from FFmpeg's `testsrc` and `sine`
sources by session fixtures, so each test gets a file with exactly the property
it is about:

| Fixture | What it is for |
| --- | --- |
| `test_clip` | 6 s, one audio track — the general case |
| `two_track_clip` | two audio tracks — §19 microphone detection |
| `silent_clip` | no audio — silent gameplay is a real recording |
| `ntsc_clip` | `30000/1001` — the rational that must not be rounded |
| `hd_clip` | 1080p60 — so the proxy has something to downscale |
| `long_clip` | 15 min — multi-segment proxy and the §7 measurement |

---

## Contracts locked in this phase

| Contract | Enforced by |
| --- | --- |
| No recording is ever held in memory (§7) | Comparative peak-RSS test across a 150× length difference |
| Sampling coverage is uniform under budget pressure (§16) | Ceiling widens the interval; equal-gap assertion |
| Frame rates keep their precision | Rational parsing; NTSC probe test |
| A proxy's timeline matches its source (§55) | Post-concat duration verification, tolerance scaled per segment |
| The original is never modified (§42) | Every FFmpeg invocation reads the source and writes elsewhere |
| Cancellation leaves a consistent project (§82) | Segment checkpoints; partial files deleted, never promoted |
| Every subprocess is an explicit argv (§85) | One process layer, `shell=False`, no string interpolation |
| A stage without a worker is not a failure | Runner stops and reports; `TRANSCRIPT` stays `QUEUED` |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| Dense candidate-region sampling | 4 — nothing yet nominates a region, and sampling two hours densely to find four interesting minutes is what §15 forbids |
| Scene detection | 4 — `SCENES` depends on `PROXY`, which now exists |
| Whisper transcription | 3 — `TRANSCRIPT` depends on `AUDIO`, which now exists |
| Thumbnail generation (§56) | 8 — `extract_at_times` is the mechanism; nothing has moments to make thumbnails of yet |
| Hardware-accelerated decode | 10 — `ffmpeg.hwaccel` is read but not yet applied; proxy generation is CPU-bound on the encoder, not the decoder |

---

## Bugs found by the tests

Not test failures — three defects the tests caught, in code that looked right:

| Defect | Consequence had it shipped |
| --- | --- |
| `new_id("track")` — the registered entity key is `media_track` | PROBE raised `KeyError` and the whole chain stalled after IMPORT |
| `GetCurrentProcess()` left with ctypes' default `c_int` return type | The 64-bit pseudo-handle was truncated, so the §7 measurement could not run at all on Windows |
| `run_job` documented a manual re-run that nothing could perform | §90 re-analysis had no way to return a finished stage to the queue; `JobManager.requeue` now does, with five tests |

A fourth, environmental: an unrelated `tests` package installed in
site-packages shadowed this one. A plain directory is only a *namespace*
portion, and Python prefers a regular package found anywhere on the path over a
namespace portion found first — so `tests/__init__.py` is load-bearing here.

---

## Gate to Phase 3

Met: `pytest` green (515), `ruff` clean, the media chain complete and wired
into the job system, and the §99 acceptance criterion measured on the target
hardware.

Phase 3 begins at §126 step 07: Whisper transcription with word timestamps,
then audio events (§18) and reaction candidates (§20).
