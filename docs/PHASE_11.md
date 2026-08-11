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
| Technical checks (§76) | `backend/qa/technical.py` | `test_qa.py` — 13 tests |
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
| QA raised when the render had legitimately skipped | A recording with nothing worth editing produced a *failed* project instead of "there was nothing to make a video from" — the pipeline working, reported as the pipeline breaking |

---

## A sensitivity worth knowing about

The frozen-frames check gives **different verdicts for the same content
depending on the encoder**. On this project, rendered from one timeline:

| Encoder | Verdict |
| --- | --- |
| libx264, CRF 19 | failed — "3.2 s of frozen picture", export blocked |
| h264_nvenc, 16 Mbps | no frozen-frames finding at all |

`freezedetect=n=-60dB` wants near-identical frames, and libx264 at a quality
target can emit bit-identical ones through a low-motion passage where NVENC's
rate-targeted quantisation always varies slightly. So the check is partly
measuring the *encoder* rather than the video — and the earlier block was
probably an artefact rather than a defect in the footage.

It is recorded rather than hurriedly retuned: raising the threshold trades one
kind of wrong answer for another, and picking the trade wants the golden
dataset Phase 15 builds.

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
