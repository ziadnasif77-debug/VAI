# Phase 4 — Vision

SPEC §15, §16, §17; §126 steps 08–09. **Acceptance: the system describes major
visual changes, and the VLM sees only candidate keyframes — verified against
`analysis.vision.max_frames_per_source_hour`.**

Status: **complete and verified.** 652 tests pass, `ruff check` is clean, and
both halves of the criterion are checked against real files — including two
opt-in tests against the actual local VLM on the RTX 3070.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Scene detection (§17) | `backend/analysis/scenes.py` | `TestSceneDetection` — 5 tests |
| The candidate cascade (§15, §16) | `backend/analysis/candidates.py` | `test_candidates.py` — 27 tests |
| Frame-difference detector | same | `TestTriggerExtraction` |
| Local VLM (§15, §50) | `ai/vision/ollama_provider.py` | `TestResponseValidation`, `TestRealVisionModel` |
| Deterministic test double | `ai/vision/fake_provider.py` | `TestFakeVisionProvider` |
| Prompt architecture (§92) | `backend/core/prompts.py`, `prompts/vision/frame_description/` | `TestPromptRegistry` — 6 tests |
| SCENES stage | `backend/pipeline/workers/vision_workers.py` | `TestSceneDetection` |
| VISION stage | same | `TestVisionBudget` — 5 tests |
| Persistence + migration 0002 | `repositories/{scenes,vision}.py` | `TestObservationPersistence` |

`IMPORT → PROBE → PROXY → AUDIO → FRAMES → TRANSCRIPT → AUDIO_EVENTS → SCENES →
VISION` now runs end to end. OCR is the frontier.

---

## The cascade, and the arithmetic behind it

A two-hour recording sampled every three seconds is **2 400 frames**. A local 7B
vision model spends seconds on each. That is an afternoon of GPU time for one
video, and §15 forbids it in as many words: *the vision model must not process
every frame.*

So cheap detectors nominate first — audio spikes, scene changes, frame
difference, speech activity — and every one of them has already run by the time
the cascade is called. Nominating costs a database read, not a decode.

Three properties make it hold:

**The budget is a ceiling, not a target.** `max_frames_per_source_hour` bounds
model work per hour of source. Under the shipped defaults a two-hour recording
gets at most 480 frames to the model — a fifth of what naive sampling would
send, and bounded no matter how eventful the recording is. An uneventful
recording uses a fraction of it; nothing is invented to fill it.

**Dropped regions are reported.** When more is nominated than the budget can
cover, the weakest are dropped and the count is on the job row. A silent
truncation would read as "we looked at everything" when we did not. Dropped
regions keep their evidence, so a re-run with a larger budget can reach them.

**Agreement outranks intensity.** A region three detectors independently
nominated ranks above one very loud bang — the same principle §27 applies to
events, applied before any model has looked at anything.

### The bug this design nearly had

The first implementation merged overlapping nominations without a bound. On a
recording with something loud every thirty seconds, every region merged into
**one region spanning two hours**, which then received four keyframes — four
frames to describe two hours, while the plan reported 100 % coverage. It passed
the budget check perfectly.

Merging is now bounded by a region size derived from the sampling config
(pre-roll + post-roll + the dense span, one minute with the defaults), so a long
firefight becomes several regions that each get their own keyframes. A test
asserts no region exceeds it.

---

## Other decisions worth knowing

### Scene change scores are measured, not assumed

PySceneDetect reports *where* it cut. Storing the configured threshold as the
change score would make every boundary in the database claim the same magnitude,
and the cascade could not rank them. A `StatsManager` records the real
`content_val` at each boundary frame, and that is what is stored.

### Scene detection walks the video in slices

A two-hour proxy is minutes of work. `detect_scenes` resumes from the stream's
current position, so calling it repeatedly walks the file once — no frame
decoded twice, detector state carried across — while giving a progress report
and a cancellation checkpoint every thirty seconds (§82).

### Unloading an Ollama model is an HTTP call

Ollama keeps a model resident for five minutes after the last request. On an
8 GB card that means the vision model still holds 7 GB when the next stage
tries to load, and §54's "one model at a time" quietly stops being true.
`keep_alive: 0` is what actually frees it.

### The prompt's schema is enforced twice

Ollama accepts a JSON schema as its `format` parameter, so the schema shipped
beside the prompt is both the constraint given to the runtime (§93) and the
contract validated on the way back (§94). One definition, two enforcement
points.

A response describing a different number of frames than were sent is rejected
outright: an observation attached to the wrong second is worse than no
observation, because it will be believed.

### Prompts cannot drift from their versions

§92 wants versioned prompts; the version participates in the cache key (§48).
`load_prompt` refuses to load a prompt whose `meta.json` version disagrees with
the registry in `versions.py`. That catches the failure that is otherwise
silent: edit the wording, forget the bump, and every cached result from the old
prompt is served as though the new one produced it.

---

## Acceptance

### Half one: major visual changes are described

Checked on a clip built from three visually distinct shots — colour bars, solid
red, solid blue — with cuts at 3 s and 6 s. The test asserts **where** the
boundaries were found, not how many: `[0.0, 3.0, 6.0]` and `[3.0, 6.0, 9.0]`.
Each scene gets a preview keyframe (§56), and each boundary carries a distinct
measured change score.

### Half two: the VLM sees only candidate keyframes

Counted at the provider. The number of frames that reach a vision model is the
number the provider was handed, and no amount of reading the cascade proves it
as directly:

* frames described == frames the plan committed to;
* frames described ≤ frames sampled;
* frames described ≤ the budget, which scales with duration;
* batch size never exceeds `max_frames_per_request`;
* the model is loaded once and released (§54).

And in the unit tests, at every source length §7 lists — 30 min through 8 h —
under a relentless stream of nominations, `frames_planned` never exceeds
`max_frames_per_source_hour × hours`.

---

## Verification status

| Check | Result |
| --- | --- |
| `pytest` | **652 passed**, 4 skipped |
| `ruff check .` | clean |
| Scene boundaries at known times | passed |
| Frame budget at 0.5 h – 8 h | passed |
| Real `qwen2.5vl:7b` on the RTX 3070 | passed (`VAI_TEST_MODELS=1`) |
| `doctor.py` | 1 warning — the Phase 13 LLM model |

The four skips are the opt-in real-model tests, gated because the first run
downloads gigabytes:

```bash
VAI_TEST_MODELS=1 python -m pytest -m requires_models -q
```

All four were run on this machine and pass. The vision one is the meaningful
part: a real local VLM, given a real extracted frame, returned JSON that
survived validation and landed on the right timestamp.

---

## Dependencies added

| What | Why | Size |
| --- | --- | --- |
| `scenedetect` 0.7.1 | §17 names PySceneDetect, and the shipped `threshold: 27.0` is written in its units | ~200 KB (OpenCV was already present) |
| `qwen2.5vl:7b` | the configured vision model | 6.0 GB |

Migration `0002_vision_observations.sql` adds the observations table — and is
the first second migration, so it also exercises the forward-only migrator.
A new test asserts `SCHEMA_VERSION` equals the highest migration number, which
`versions.py` claimed but nothing enforced.

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| HUD-change trigger | 5 — the trigger name is reserved in the cascade; nothing produces HUD state until OCR exists |
| Game-event detection from observations | 5 — §27 correlates several sources, and OCR is one of them |
| Motion vectors as a cheap detector | frame difference covers the same ground for now; revisit if the cascade misses in-shot action on the golden dataset |
| Per-region dense sampling | the cascade plans keyframes directly; `plan_candidate_sampling` is there when a stage needs the dense pass |

---

## Gate to Phase 5

Met: `pytest` green (652), `ruff` clean, both halves of the acceptance criterion
executed against real files, and the real VLM verified on the target hardware.

Phase 5 begins at §126 steps 10–11: OCR (§25) and game-event detection (§21,
§26, §27). PaddleOCR is already installed. Its central constraint mirrors this
phase's: region-restricted OCR against the HUD boxes a game profile declares is
both cheaper and far more accurate than scanning a whole frame of stylised game
UI — and §23 still requires an unknown game to work, via the reduced-resolution
full-frame fallback on candidate frames only.
