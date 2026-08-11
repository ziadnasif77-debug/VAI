# Phase 10 — Final render

SPEC §65, §67, §72–§75; §126 step 16. **Acceptance: the final MP4 opens in
standard players; duration inside the 10–60 minute band.**

Status: **complete and verified.** `ruff` is clean, and the pipeline now
produces a file someone can watch — decoded end to end without a single warning,
carrying both streams, at the requested format and length.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Encoder choice (§52, §75) | `backend/rendering/encoder.py` | `TestEncoderArguments` — 9 tests |
| Cut and concat (§65, §47) | `backend/rendering/ffmpeg_renderer.py` | `test_render.py` |
| Audio mix (§72–§74) | `backend/rendering/audio_mix.py` | `TestDuckingEnvelope`, `TestMixGraph` — 16 tests |
| Composite and encode (§67) | `backend/rendering/composite.py` | `TestAcceptance` |
| Render record (§45, §80) | `backend/database/repositories/renders.py` | `TestTheRenderRecord` |
| RENDER stage | `backend/pipeline/workers/render_worker.py` | `test_render.py` — 16 tests |

`IMPORT → … → STORY → EDL → RENDER` now runs end to end and produces an MP4.
QA is the frontier.

---

## The defect this phase existed to find

`ffmpeg -encoders` lists `h264_nvenc`. `nvidia-smi` shows an RTX 3070. Both are
true, and the encoder still cannot open:

> Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0

Every render would have failed on the first clip, several minutes in, with a
message about "incorrect parameters such as bit_rate, rate, width or height" —
which points at nothing. And `doctor.py` reported `[OK] nvenc — Hardware
encoding available`, because it read the list too.

This is exactly the PaddleOCR defect from Phase 5 wearing different clothes: an
installed-but-broken component reports as ready right up until the moment it
matters. The fix is the same one that worked there — **try it, do not ask it**.
`encoder_works()` encodes one frame of generated colour to the null muxer,
which costs about a tenth of a second, and `select_encoder` walks the
preference list until something actually encodes. The health check calls the
same function, so the report and the render cannot disagree.

The machine now correctly falls back to libx264 and says why:

> Hardware encoding is listed but does not work: h264_nvenc (Driver does not
> support the required nvenc API version…). Rendering will use libx264.
> → Update the NVIDIA driver, or accept slower CPU encoding.

---

## Ducking is computed, not detected

§74 says music drops when the player speaks and when important game audio
occurs. The obvious tool is a sidechain compressor — and it is the wrong one,
for the reason §64 gives about renderers generally: it would put the decision
inside FFmpeg, where a level threshold has to guess what a speech detector
already knows. Worse, on a recording whose microphone was never on a separate
track there is no signal to watch at all, which describes every recording this
project has been run against.

So the gain envelope is computed in Python from spans the analysis already
established, with §74's attack, release and hold applied as explicit ramps, and
FFmpeg does one multiplication (`amultiply`). The curve is exact, click-free by
construction, and testable without decoding anything.

Overlapping spans take the **deepest** reduction rather than adding up: speech
during a big game moment should duck the music once, by the larger amount, not
twice into inaudibility.

### §72's priority needed a knob that did not exist

*Speech > Important Game Audio > Music.* With the shipped gains — microphone
+2 dB, music −18 dB — that order holds until the game gets loud, which is
exactly when someone shouts. `ducking.game_under_speech_db` (−4 dB) makes the
gameplay lean back under speech. Much shallower than the music's −14: the
gameplay is the subject of the video, not a background bed.

---

## Other decisions worth knowing

### Segments, not one enormous filter graph

Trimming seventy clips in a single `filter_complex` is a legitimate way to do
this and a bad way to ship it: no meaningful progress, no resume after a crash
three quarters through a twenty-minute encode, and a failure that reports one
unreadable graph rather than the clip that broke. Segments give §47 its resume,
§82 a cancellation point every few seconds, and a stack trace that names a clip.

The cost is a second encode, so segments are written visually lossless — every
artefact introduced there survives into the finished video, and disk is the
cheapest thing in this pipeline. They are deleted once the MP4 exists.

A segment left behind by a killed process is a readable file of the wrong
length, so reuse is decided by **measuring** it, not by finding it.

### The audio is one pass, the video is many

The reasons video is segmented — resume, progress, per-clip failures — do not
apply to audio, which is cheap. One filter graph keeps sample alignment exact
across every cut.

### The finished file is probed, not restated

An encoder can exit zero having produced 29.97 fps instead of 30, or no audio
stream at all. `FinalRender` is built from `ffprobe` output, so the job result
describes what is in the file rather than what was asked for.

### The render row is written before the encode

A render that crashes leaves evidence that it was attempted. A row that only
appears on success cannot answer "why is there no video?".

---

## Acceptance

`test_render.py` runs the whole pipeline once and asserts against the file it
produced: it decodes end to end with **empty stderr**, is an MP4 with the
configured codecs, carries both a picture and a sound, matches the requested
resolution and frame rate, and lasts as long as the edit said it would — within
one second, because a frame per cut is normal and a second is not.

The 10–60 minute band is checked where it is decided, in the timeline
(`test_timeline.py::TestDurationClamp`). A forty-second fixture cannot become a
ten-minute video, and stretching one to test the band would be testing the
clamp rather than the render.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| NVENC listed but unopenable; both the encoder choice and the health check read the list | Every render fails minutes in, with a message that points at nothing, on a machine whose doctor said hardware encoding was available |
| Music looped with `apad` instead of `-stream_loop` | `apad` pads with silence: a two-minute bed under a twenty-minute video gives two minutes of music and eighteen of nothing |
| `amix` left to normalise by default | Everything drops ~9 dB the moment music is added to the mix |
| A dead `if not mixed` guard | Unreachable: the gameplay track is always appended. Removed rather than tested |
| The acceptance re-ran the whole pipeline per test | Fifteen minutes of repetition for one acceptance; the fixture is now module-scoped and the file runs in seven |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| QA of the finished file (§76–§79) | Phase 11, which is what checks for black frames, desync and clipping |
| Transitions beyond cut and programme fades | The timeline carries `transition_in`/`transition_out` per clip; realising a crossfade needs an overlap the concat path does not currently produce |
| Speed ramping and freeze frames (§69, §70) | FFmpeg-engine effects. The planner emits them; the renderer does not yet apply them |
| Rendering only the drawn overlay spans | The spans are computed (Phase 9) and unused. Worth doing when a real twenty-minute render makes the saving visible |
| Music selection per section (`change_on_section`) | One bed, looped. Choosing per section needs the narrative beats, which exist, but no music library does yet |

---

## Gate to Phase 11

Met: `ruff` clean, and an MP4 that decodes without a warning.

Phase 11 begins at §126 step 17: QA (§76–§85). Its job is to check the file
this phase produces — duration, black frames, frozen frames, A/V sync, caption
sync, loudness and clipping — and §79's rule is the one that shapes it: a
failed check must say what to do about it, not merely that something is wrong.
