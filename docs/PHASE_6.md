# Phase 6 — Moments

SPEC §28–§34; §126 step 12. **Acceptance: ranked moments, each with a stored
`score_breakdown` and `explanation`.**

Status: **complete and verified.** `ruff check` is clean and the acceptance runs
through the whole pipeline on real files — moments formed from events that came
from detectors that came from a decoded recording.

This is the pipeline's centre of gravity. Everything before it *describes* a
recording; everything after it *edits* one.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Moment formation (§28) | `backend/moments/formation.py` | `TestFormation` — 8 tests |
| Adaptive context (§29) | `backend/moments/context.py` | `TestContextExpansion` — 9 tests |
| Dead time (§30) | `backend/moments/dead_time.py` | `TestDeadTime` — 6 tests |
| Repetition (§31) | `backend/moments/repetition.py` | `TestRepetition` — 6 tests |
| Ten-dimension scoring (§32) | `backend/moments/scoring.py` | `TestScoring` — 13 tests |
| Variety (§33) | `backend/moments/repetition.py` | `TestVariety` — 4 tests |
| MOMENTS stage | `backend/pipeline/workers/moments_worker.py` | `test_moments_pipeline.py` — 11 tests |
| Persistence | `repositories/moments.py` | pipeline tests |

`IMPORT → … → GAME_EVENTS → MOMENTS` now runs end to end, and the runner
queues the project-wide stages once every media chain finishes. STORY is the
frontier.

---

## §33 is the design constraint, not a caveat

> **The highest score is not necessarily the best clip.** The system must
> consider story, context, progression, variety and pacing.

So this phase produces **ranked candidates with their working shown**, not a
selection. Nothing here decides what goes in the video — §37 does, and it needs
the breakdown rather than the total. "This scored 0.82" says nothing about
whether the clip belongs in a story.

Three consequences run through the code:

**Variety is a penalty, not a filter.** Once a type dominates the shortlist, the
next moment of that type is worth less *to this video* than its own merits
suggest — but it is never rejected. Twelve excellent kills in a row is a worse
video than eight kills, two fails and a funny moment, and no per-moment score
can express that because the cost only exists relative to the selection.

**Dead time is scored, not deleted.** §30 says removal happens *only when
removal does not damage context*, so this module marks and the narrative stage
decides. A stretch that is 90 % dead air may still be the only bridge between
two moments the story needs.

**Repetition keeps the strongest, not the first.** Chronological thinning is the
obvious approach and the wrong one: the fifth attempt at a boss is usually the
one that worked, and the first is somebody dying to a mechanic they had not
learned yet. Keeping earliest-first produces a video of first drafts.

---

## The decisions worth knowing

### A fixed ±20 s is what makes an automated edit obvious

§29 says the roll must be *adaptive*, with the emphasis in the spec. The fixed
version is instantly recognisable: every clip opens on twenty seconds of
somebody walking and ends twenty seconds after the interesting part is over.

So the roll is shaped by moment type — a clutch is *made* by its build-up, a
funny moment by the beat after it — and then snapped to something real:

* a **scene boundary**, because a cut is where the picture already changed;
* a **speech boundary**, because opening mid-word is the most audible artefact
  automated editing produces.

Both are bounded. A clip that opens forty seconds early to catch a distant scene
cut has traded one artefact for a longer one, so the scene snap gives up outside
its window. The speech rule is bounded differently — by the configured pre-roll
ceiling rather than the snap window — because a cosmetic misalignment and an
audible one do not deserve the same tolerance. A sentence that began before any
allowed lead-in causes the clip to start *after* it instead: half a sentence is
worse than none of it.

### Context expansion runs before dead time

Dead time is measured against what a clip would actually **show**. The pre-roll
is already part of a moment, so counting it as removable would mean the same
seconds are both kept and cut.

### Dead time adjacent to a kept moment is protected

The walk *up to* the ambush is what makes the ambush land. Cutting straight from
one kill to the next is exactly the "no breathing room" failure §37 warns about,
and it is what naive dead-time removal produces.

### Penalties are multiplicative

A moment that is repetitive *and* mostly dead air should fall much further than
either alone. Additive penalties cannot express that without going negative.

### Scoring is rule-based, and that is a requirement

§95 says the system degrades when the LLM is unavailable, and scoring is the
last place that could quietly stop working — a pipeline that produces no moments
without a model does nothing on a machine without one. Every dimension is
computed from stored evidence, and a test asserts the scorer works with an empty
`ScoringContext`.

Several dimensions are honestly partial and say so rather than inventing
confidence:

* **skill** without game knowledge cannot tell a lucky kill from an outplay, so
  a plain kill scores 0.45 and only the types that *mean* difficulty score high;
* **narrative** without the story pass knows only that some types are natural
  beats and that beginnings and endings carry structural weight;
* **entertainment** is the one composite, because "entertaining" has no single
  measurement and pretending otherwise would be worse than combining the three
  signals that gesture at it.

### User decisions survive a re-run

§78 and §121 give the user the final word. A moment they rejected must not come
back accepted because the analysis re-ran, so `user_state` is read before the
replace and re-applied.

---

## Acceptance

Run on a real clip through the whole pipeline:

* moments produced and **ranked** by score, descending;
* every moment carrying all ten §32 dimensions in its stored breakdown, each in
  0–1, plus the penalties and the multiplier **stored separately** — so a low
  score can be attributed rather than guessed at;
* every moment carrying an **explanation** in sentences, which says something
  about the evidence rather than repeating the number;
* every moment's viewing span wider than its events (§29);
* moments never outnumbering the events they came from — §28 groups, it does
  not multiply.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| The runner could not queue the project-wide stages | After MOMENTS the pipeline stopped with nothing queued; STORY was unreachable without an API call. The runner now queues them once every media chain is complete, which is what §46 means by "once every file has finished its own chain" |
| The speech-boundary rule used the scene snap window | A clip whose start landed 4 seconds into a sentence opened mid-word, because a window sized for a cosmetic scene snap was being applied to an audible artefact |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| `ai/llm/ollama_provider.py` | The plan listed it here, but the acceptance is rule-based by §95's requirement and nothing in this phase needs a model. It lands in Phase 7, where the narrative genuinely needs one |
| LLM-refined scoring | Same reason. The breakdown is designed so a model can adjust dimensions later without the rule-based path becoming dead code |
| Moment thumbnails | `thumbnail_path` exists and `extract_at_times` is the mechanism; the review screen that needs them is Phase 12 |
| Cross-media moment ranking | Moments are per-media by schema. Ranking across files is a selection concern, and selection is §37 |

---

## Gate to Phase 7

Met: `ruff` clean, the acceptance criterion executed against real files through
the whole pipeline.

Phase 7 begins at §126 step 13: story construction (§35–§37), the hook (§38) and
the duration optimiser (§39). Its constraint is §33 made concrete — the
optimiser has to hit a 10–60 minute target from a ranked list without producing
a video that is just the top N clips in score order.
