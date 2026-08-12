# Phase 11 — QA

SPEC §76–§79; §126 step 17. **Acceptance: a deliberately broken render is
detected. Technical failures block export; content warnings go to human
review.**

Status: **complete and verified.** `ruff` is clean. Five deliberately broken
renders are each caught by name, and a good one comes back clean.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Findings and policy (§76, §79) | `backend/qa/report.py` | `TestReportPolicy` — 6 tests |
| Technical checks (§76) | `backend/qa/technical.py` | `test_qa.py` — 18 tests |
| Content checks (§77) | `backend/qa/content.py` | `test_qa_content.py` — 15 tests |
| Persistence (§45, §80) | `backend/database/repositories/qa.py` | `TestQaStage` |
| QA stage | `backend/pipeline/workers/qa_worker.py` | `test_render.py::TestQaStage` |

`IMPORT → … → RENDER → QA` now runs end to end. Every stage the runner queues
has a worker; export and publish are excluded by design, because §46 makes
publishing an explicit action rather than a pipeline step.

---

## The acceptance needs things that are really broken

A detector can only be tested against a real defect, so the fixtures are
genuinely bad videos built with FFmpeg: black throughout, frozen, silent,
missing an audio stream, a third of the length it claims. Each is caught **by
name** — asserting only that "QA failed" would also pass for a check that fails
everything.

The mirror matters as much and is easy to forget: a good render must come back
clean. A QA engine that cries wolf is one people switch off, and then it
protects nothing. `test_a_good_render_passes_every_check` is the test that
keeps the rest honest.

### The bug the frozen fixture found

`freezedetect` prints `freeze_start`, then `freeze_duration` when the freeze
*ends*. A freeze still running when the file ends never prints a duration — and
the first implementation zipped the two lists together, so it dropped exactly
that case. The one video most obviously broken, frozen from beginning to end,
was the one that passed.

Unterminated freezes are now closed against the file's own length, and the test
that found it says so.

---

## Two kinds of finding, two consequences

§76 and §77 describe different things, and conflating them would make one of
them useless:

**Technical** findings are facts about the file. It does not decode; it has no
audio stream; it is thirty seconds long. These block export.

**Content** findings are judgements about the edit. A menu screen survived into
the video; there is a nineteen-second silence; the caption band sits over the
scoreboard. §78 says the system must never assume its own decisions are
correct, so these go to a person and never stop a render.

Every non-passing finding carries a **remedy**, and a test enforces it. §79's
point is that a QA engine which only says *something is wrong* has moved the
problem rather than solved it.

Passes are stored too. "The audio was checked and is fine" and "nobody looked
at the audio" are different statements, and a table holding only problems
cannot tell them apart.

---

## Content QA reads analysis; it does not watch the video again

The pipeline already looked at this footage — vision described the candidate
frames, the audio pass found the silences, the narrative stage measured its own
pacing. Re-watching twenty minutes to rediscover that would cost more than the
render and find less, because a second pass would have none of the context the
first one built.

So each check reads stored analysis and asks about the **finished timeline**.
The distinction is the whole design: not "is there a menu in this recording" —
there usually is — but "did a menu screen end up in the edit". A menu at 60 s
of a recording whose clips cover 0–20 s and 100–120 s is not a finding.

`caption_covers_hud` is geometry rather than vision: the caption band comes
from the layout config and the HUD regions from the game profile, so it is a
rectangle intersection. Under the generic profile there are no declared regions
and the check says so, which is more useful than silence.

---

## What the fake provider taught the tests

The first version of the pipeline QA test asserted that a good render passes
*every* check. It failed — because `FakeVisionProvider` describes "a menu with
the inventory open", and the content check correctly found that clip in the
edit.

The check was right and the assertion was wrong. What "a good render" means at
that level is that nothing **technical** failed and export is not blocked; a
content warning is the system doing its job.

---

## The suite change this phase forced

Adding the RENDER and QA workers meant `run_project` ran to a finished MP4
everywhere — so a Phase 4 vision test began spending minutes of CPU encoding a
video to prove something about keyframes. That is also what made a full-suite
run appear to hang for an hour.

`tests/conftest.py::workers_through` limits a file's registry to its own phase,
and the shared `pipeline_runner` fixture stops at VISION for the same reason.
A vision test stops at VISION, an EDL test at EDL, and `assert_frontier_waits`
sees the runner waiting exactly as it did before. The helper itself now covers
both cases — an unimplemented stage waits, and a fully implemented pipeline
finishes with nothing failed — because that is what every caller meant.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| A freeze running to the end of the file was dropped when pairing starts with durations | An entirely frozen video — the most obviously broken case — passed QA |
| Every phase's integration test ran the full pipeline once RENDER existed | Minutes of CPU encoding per test; a full suite that looked like it had hung |
| `TrackInfo` had no stream start time | A/V desync could not be checked at all |
| `freezedetect`'s noise floor was hard-coded at -60 dB, near bit-identical | The verdict followed the encoder: menus blocked an export through one encoder and a real 4-second stall passed through the other |
| QA raised when the render had legitimately skipped | A recording with nothing worth editing produced a *failed* project instead of "there was nothing to make a video from" — the pipeline working, reported as the pipeline breaking |

---

## The frozen-frames check was measuring the encoder — resolved

It gave **different verdicts for the same content depending on the encoder**.
On one project, rendered from one timeline:

| Encoder | Verdict |
| --- | --- |
| libx264, CRF 19 | failed — "3.2 s of frozen picture", export blocked |
| h264_nvenc, 16 Mbps | no frozen-frames finding at all |

`freezedetect=n=-60dB` asks for near-identical frames, which is a question
about the quantiser rather than about the picture. Two things were wrong, and
only one of them was the number.

### What it should mean

A frozen render is two opposite events wearing one face:

* the **recording** was still — a menu, a loading screen, a paused game. The
  render is faithful and the video plays. Whether that belongs in the edit is a
  judgement, and §77 gives judgements to a person.
* the **render** stopped on its own — a bad seek, a corrupt segment, a
  swallowed decode error. That is a fact about the file, and it should block.

The render alone cannot tell them apart, so it was blocking on both. The check
now maps each freeze back through the timeline and asks the *recording* whether
it was holding still there too. Both files are measured at the same noise
floor, so what is compared is two pictures rather than two quantisers. A freeze
the recording accounts for is a **content warning**; one it does not is a
**technical failure**, as before. With no timeline to map through — a bare file
handed to `inspect` — nothing is explained and every freeze still fails, which
is the safe way to be wrong.

### Picking the noise floor by measuring it

`qa.technical.thresholds.freeze_noise_db` is configuration now, not a constant
in a filter string, which is how it went a whole phase untuned. The value came
from measurement rather than taste: 19 passages of real recordings — two games
on two engines, four menus, two pause screens, two loading screens, an idle
character, a desktop, and seven stretches verified to be moving — each encoded
with **both** encoders `config/rendering.yaml` chooses between, at their
production settings, and swept from -70 dB to -30 dB. Plus a real 4-second
stall spliced into moving footage, as the defect that must always be caught.

| Noise floor | Encoders disagree | 4 s stall caught | Moving footage flagged |
| --- | --- | --- | --- |
| -60 dB *(was)* | **5 of 19** | by libx264 only | 0 of 7 |
| -55 dB | 4 of 19 | by libx264 only | 0 of 7 |
| -50 dB | 2 of 19 | by libx264 only | 0 of 7 |
| **-45 dB** *(now)* | **0 of 19** | **by both** | **0 of 7** |
| -40 dB | 0 of 19 | by both | 0 of 7 |

-45 dB is the **strictest** floor that reaches full agreement, which leaves the
most headroom against slow-but-real motion in footage the sample does not
contain.

The sharpest number is in the middle column. At -60 dB the genuine stall
measured 4.00 s through libx264 and 2.00 s through NVENC — so the setting that
blocked an export over a menu screen would have **let a real stalled render
through**, on the encoder this machine prefers. It was not merely noisy; it was
noisy in both directions at once.

Two corrections to the original diagnosis, both from the data:

* the direction is not fixed. NVENC froze *more* than libx264 on menus and
  *less* on the stall. It is quantisation either way, not a property of one
  encoder.
* runs kept landing on exact 2.00 s multiples — the 2-second GOP. At a floor
  that tight, each IDR reconstructs the picture slightly differently and breaks
  the run, so the check was partly measuring `render.gop_seconds`.

That non-monotonicity is also why the third option on the table — requiring a
freeze to exceed the threshold in both a low- and a high-bitrate encode — was
rejected. There is no consistent direction to exploit, and it doubles the cost
of every render to answer a question one decode of the source already answers.

### What the false-positive check cost

At -45 dB all eleven genuinely static passages register as frozen. That is the
detector working, and it is exactly why corroboration is not optional: without
it, raising the floor would block every video containing a menu. With it, those
eleven become warnings a person can dismiss, and the seven moving passages stay
silent.

The loading screens are the sharpest of them, and worth keeping in any future
sample. Their art is still but the spinner is not, so they do not fill the clip
the way a menu does — they produce runs of 3.2 s and 4.4 s, landing just past
the 3 s threshold rather than far above it. That is where a detector is most
likely to flip on a small change, and both encoders still agree there at -45 dB
(they do not at -55 dB, where one reads 2.65 s and the other 4.10 s on the same
picture).

### The same measurement, one layer up — found 2026-08-12

Matching the noise floor across encoders fixed *what* counts as a still frame.
It did not fix how still frames are grouped into runs, and the check compares
a **run** on the render against a **run** on the source.

`freezedetect` closes a run the instant one frame differs and opens the next at
the same timestamp, so a still scene arrives as several adjacent runs. That
grouping is encoder-dependent in exactly the way the noise floor no longer is:
once an encoder has quantised near-identical frames into identical ones, the
same stillness comes back as one unbroken run.

A finished 9:58 video was blocked on "3.2 s of frozen picture at 578.1 s, which
the recording does not account for". The frames are a dialogue scene — an NPC
talking, the player standing still. Measured on the recording behind it at
-45 dB:

| | |
| --- | --- |
| Source, as reported | runs of 2.167 s and 2.783 s, adjacent at 2.180 s |
| Source, as it is | 4.95 s still out of a 5.2 s window |
| What `_freeze_in` returned | 2.783 s — the longest single run |
| Threshold | 3.0 s |
| Verdict | "the recording was moving" → export blocked |

Adjacent runs are now joined in `_parse_freeze`, which both call sites go
through, so the render and the source are grouped by the same rule. The same
render now reports "11.4 s of the edit does not move, starting at 573.7 s — the
recording is still there too": a content warning, and the true length of the
still stretch, which the split runs had hidden.

The lesson generalises past this check. Two measurements are only comparable if
**every** step of them matches — the threshold, the grouping, and the summary
statistic. Matching one of the three and assuming the rest is how a comparison
keeps a bug while looking rigorous.

### A trap for anyone re-running the measurement

The first synthetic defect fixture reported a 4-second stall as a 12-second
one. The held frame had been spliced in from a PNG, which carried a different
colour range, and FFmpeg **reconfigures the filter graph when frame parameters
change** — which resets `freezedetect` mid-file, so the pending freeze never
prints its duration and gets closed against the end of the file instead. Build
such fixtures in a single filter graph, and treat "Reconfiguring filter graph"
in the log as invalidating every number from that decode.

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| An LLM for §77's content judgements | Phase 13 brings the model. The checks here are deterministic and read stored analysis; a model could add nuance where a rule cannot, and the split is designed so it can |
| Caption sync measured against the audio | The captions come from transcript timestamps mapped through clip positions (§71), so a desync would be an arithmetic error the timeline tests already cover. Measuring it in the finished file needs speech recognition on the render — a Phase 15 quality question |
| Per-check confidence (§79) | Every technical check here is a measurement, not a judgement, so its confidence is 1. Content checks are advisory by construction |
| Resource and disk management (§83, §84) | Part of the same Part X, but not QA: they belong with the UI's controls in Phase 12 |

---

## Gate to Phase 12

Met: `ruff` clean, and every deliberately broken render detected by name.

Phase 12 begins at §126 step 18: the UI (§57–§63). §126's rule is the one that
governs it — *do not build a large interface before the pipeline produces a
convincing video*. It now does, so the gate is open.
