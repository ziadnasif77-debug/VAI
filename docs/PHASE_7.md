# Phase 7 — Narrative

SPEC §35–§39; §126 step 13. **Acceptance: a 2-hour source becomes a coherent
20-minute edit within the configured tolerance.**

Status: **complete and verified.** `ruff check` is clean, the optimiser hits
every duration preset §6 offers, and the STORY stage runs end to end through the
pipeline.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Duration optimiser (§39) | `backend/narrative/optimizer.py` | `TestOptimiser` — 10 tests |
| Three video modes (§35, §36) | `backend/narrative/story.py` | `TestModes` — 7 tests |
| Hook (§37) | `backend/narrative/hook.py` | `TestHook` — 6 tests |
| Pacing (§38) | `backend/narrative/pacing.py` | `TestPacing` — 5 tests |
| STORY stage | `backend/pipeline/workers/story_worker.py` | `test_narrative_pipeline.py` — 11 tests |

`IMPORT → … → MOMENTS → STORY` now runs end to end. EDL is the frontier.

---

## §39 is an optimisation problem, not a sort

The spec says so in bold, and the difference is concrete. Sorting by score and
taking clips until the clock runs out fails twice over:

**It misses the target.** The greedy prefix stops wherever the next clip happens
not to fit — a 20-minute request becomes 17:40 because the 19th moment was three
minutes long. No re-sorting fixes that; the problem is that the last choice was
made without knowing what came after it.

**It produces a monotonous video.** The top of a score ranking is the same kind
of moment repeatedly, because whatever the scorer likes, it likes consistently.

So the optimiser is an exact 0/1 knapsack over one-second buckets. The problem
is small — a few hundred moments against a 1 200-second target is a table filled
in milliseconds — and an exact answer is worth more than a heuristic here.

**Variety lives inside the objective, not after it.** A bonus computed per
moment cannot express "this is the fourth kill in a row", so the search carries
the type mix of each partial solution and prices the next addition against it.
That is the whole reason a sort cannot do this job.

Measured on a 150-moment synthetic session against a 20-minute target:

| | duration | distinct types |
| --- | --- | --- |
| greedy top-N | 1192.6 s | 8 |
| optimiser | 1255.1 s | **15** |

Both land inside the ±60 s the product declares acceptable. The optimiser
returns nearly twice the variety.

### On landing slightly long

Deviation from the target is priced (`DEVIATION_WEIGHT`), but value grows with
duration, so the search fills towards the ceiling when more good clips are
available. That is the correct trade: 20:55 for a 20:00 request is inside the
tolerance the config itself declares, and 15 distinct moment types beats 8.

---

## Other decisions worth knowing

### The hook selects; it never invents

§37 is explicit: **the system must not invent narration.** So the hook is a
moment that already exists, moved to the front. No voice-over, no title card, no
generated footage.

That constraint also makes the feature honest. A generated "you won't believe
what happens next" is a promise the video may not keep; a clip of the thing
actually happening is the same promise, kept in advance.

A hook too long for the opening is trimmed **from the front**, because the
payoff is at the end of a moment and an opening that stops before it promises
nothing.

### Pacing preserves the caller's order

The first implementation re-sorted chronologically, which silently made all
three §35 modes produce **identical output** — story, best-moments and
compilation returned the same 70 clips in the same sequence. Each mode has
already decided its sequence for a reason, so pacing now only does local swaps
to break up runs of one type.

### Pacing warns rather than corrects

"This section is flat" is a judgement a human may reasonably disagree with, and
§78 gives them the last word. The report carries warnings; nothing is silently
dropped to make a curve work — the selection came from the optimiser, which
balanced duration against value, and discarding part of it here would break that
guarantee.

### Story mode weighs coherence against score

§36: *a slightly weaker moment may be selected when it creates necessary
context.* A tense build-up scores modestly and is the only thing that can fill
the build-up beat, so it wins that slot over a higher-scoring kill that would
make the arc nonsense. The ratio is configurable
(`story.coherence_weight` / `score_weight`).

A beat with no candidate is simply absent. A session with no defeat has no
"reaction to defeat" beat, and inventing one would mean inventing footage.

### One helper instead of a test each phase rewrites

Four phases in a row broke the *previous* phase's "the runner stops here" test,
because each named the frontier stage and each phase moves it. The name was
never what the test was for. `tests/conftest.py::assert_frontier_waits` asserts
the property instead — the first stage with no worker is queued, not failed —
and the four copies now call it.

### The plan is not a timeline

STORY produces an ordered selection with a hook and a pacing report, stored on
the job row. Phase 8 turns it into an EDL. Keeping them apart is what makes
§127's re-edit cheap: changing the target duration re-runs this stage against
stored moments in milliseconds and never re-analyses the source.

---

## Acceptance

`test_narrative.py::TestAcceptance` runs the arithmetic against a 200-moment,
2-hour-plus session: it lands inside the tolerance, has a hook, has a climax,
and carries at least five distinct moment types. `TestOptimiser` repeats the
duration check across **every preset §6 offers** — 10, 15, 20, 30, 45 and 60
minutes — from the same source.

`test_narrative_pipeline.py` runs the stage through the real pipeline on a
decoded recording: the plan is built from stored moments, every clip references
the source non-destructively (§42), the hook is a clip that exists in the plan,
and a source too short to fill the request **reports missing the target rather
than faking it**.

The pipeline fixture is a short clip on purpose. A 2-hour fixture would take an
hour to transcode and prove nothing the unit tests do not already prove about
the optimiser; what only the pipeline can show is that the stage is wired, reads
what earlier stages stored, and produces a plan the EDL stage can act on.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| Pacing re-sorted chronologically | All three §35 video modes produced identical output. The user picks "compilation" and gets the story edit |
| A circular import through the repositories package | `backend/gaming/events.py` imported `StoredObservation` from persistence — a domain module depending on the repository layer. It surfaced as an ImportError only from certain entry points; the type now lives beside `VisionObservation` in `ai/providers/base.py`, which removes the dependency rather than working around it |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| `ai/llm/ollama_provider.py` | Still not needed. Everything in this phase is deterministic, and §95 requires it to stay that way. It lands in Phase 13, where natural-language editing genuinely needs a model |
| LLM-assisted story structure | The beat assignment is rule-based and works; a model could refine which moment fills which beat, and the breakdown is designed so it can without the rule path becoming dead code |
| Music-driven pacing | §73 is local-music-only and music selection is Phase 10's audio mix. Pacing against a track's beats needs the track first |
| Cross-project narrative | Out of scope: a video is one project |

---

## Gate to Phase 8

Met: `ruff` clean, the optimiser verified against every duration preset, and the
stage running end to end.

Phase 8 begins at §126 step 14: the EDL and timeline (§40–§45). Its constraint
is §42 — the timeline is **non-destructive**, referencing source timestamps
rather than copying frames — and the last-resort duration clamp lives at its
boundary, with a warning when that path is reached.
