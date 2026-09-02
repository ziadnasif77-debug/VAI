# P2.1 — Replay, analysed before it is built

No code written. This is the pre-implementation analysis the owner asked for:
what would be built, what it touches, how it would be judged, what could move
the house edit, and what needs deciding first.

Its conclusion is that the last question matters most, because **the candidate
set on this machine is two clips out of two hundred and fifty-four.**

## What exists already

| | |
|---|---|
| `hook.allow_replay_in_body` | a real replay switch, **`false`** in `config/narrative.yaml` |
| `timeline/builder.py::_exclusive` | **each second of source appears at most once**, enforced at the EDL boundary |
| `timeline/validation.py::ensure_chronological` | one exception only: the leading run of `role == "hook"` clips |
| `timeline/retime.py` | freeze and speed-ramp written into the timeline; slow motion is already renderable |
| `TimelineClip.speed` | exists, validated against the clip's spans |

The mechanism for a slow replay is therefore already built. The thing standing
in the way is not technical, it is a rule — and the rule has a history worth
quoting from `_exclusive`'s own docstring:

> the first real viewer watched 25 seconds twice (a hook replayed by
> configuration)

`allow_replay_in_body` was `true`, a hook was replayed in place, a person
watched it and complained, and the switch was turned off and a boundary rule
written so it could not happen by accident again. **Any replay design has to
pass through that rule deliberately, never around it.**

## What the footage offers

Measured across the house edits of all 17 projects, 254 clips:

| | count |
|---|---:|
| clips whose purpose is `PAYOFF` | **0** |
| clips containing a resolving event (victory, clutch, multi-kill…) | 35 |
| clips somebody responded to | 22 |
| clips whose core span is ≤ 8 s — brief enough to replay at all | 43 |
| **both a resolution and brief** | **2** |

And the reason the two columns barely intersect:

> resolving clips' **core span: median 39.5 s**, min 8.0 s, max 164.4 s

**Corrected, 2026-09-02.** That number is a *moment* span, not an event span,
and the sentence that followed it here — "a victory on this machine is a
thirty-nine-second event" — was wrong. Measured off `game_events` directly:

| unit | n | median | max |
|---|---:|---:|---:|
| all events | 1,141 | **12.0 s** | 280.8 s |
| resolving events | 173 | **12.5 s** | 91.2 s |
| moments | 430 | **33.3 s** | 280.8 s |

The coarse unit is the moment, not the event. The events were always about
twelve seconds long; what had no located instant inside it was the moment
wrapped around them, which is precisely what `EditorialEventSpan` was built to
answer in V2-P2.2 — and it now locates a resolution in 36 of 435.

The conclusion of this section survives the correction and the reason changes:
replay candidates are scarce because a *clip* spans a moment rather than an
event, not because events are long.

`PAYOFF` is zero for a related reason: it needs either a tension-lane drop or a
resolving event, and V2-P0 measured only 3 payoffs across 293 shots.

**So the blocker is not the chronology rule and not `_exclusive`. It is that
the event boundaries are too coarse to say where the moment worth replaying
is.** That is the same shape as the transcript-segment defect V2-P1.4 found:
a store whose spans are far larger than the question being asked of them.

## 1. What would be built

The chain the owner specified, unchanged:

```
ReplayCandidate  →  ShouldReplay?  →  ReplayStrategy
                                      NONE | MICRO | SLOW | FULL | ANGLE
```

- **`ReplayCandidate`** — a shot plus the instant inside it worth showing
  again. Derived from existing evidence, no new detector.
- **`ShouldReplay?`** — a bounded decision: at most one replay per N clips, never
  two adjacent, never inside the duration band's margin, never on a shot that
  is already the edit's ending.
- **`ReplayStrategy`** — resolved by style through `ResolvedEditingPolicy`, like
  every other doctrine since V2-P0. `competitive` would replay the play;
  `cinematic` would slow it; `minimal` would refuse.
- **A declared re-use in the timeline** — a clip carrying
  `metadata["replay_of"]`, which `_exclusive` admits *because it is declared*
  and continues to reject when it is not.

**`ANGLE` cannot be built.** It needs a second camera on the same instant and
this system records one. It would be an enum member with no implementation,
which is the orphaned-key pattern this project has spent two phases removing.

## 2. Files affected

| file | change |
|---|---|
| `backend/editorial/replay.py` | new — the three-stage chain |
| `backend/editorial/strategy.py` | a `ReplayPolicy`, neutral by default |
| `backend/config/schema.py` | `StyleShotsConfig` gains the replay keys |
| `config/style.yaml` | doctrine per style, and the `limits` fence |
| `backend/timeline/builder.py` | **`_exclusive` learns about a declared replay** |
| `backend/timeline/validation.py` | a replay clip must sit adjacent to its original — chronology is otherwise untouched |
| `backend/pipeline/workers/story_worker.py` | the consumer |
| `scripts/baseline.py` | measure replays per edit |
| `tests/unit/test_replay.py` | new |
| `tests/integration/test_house_edit_frozen.py` | unchanged, and must stay green |

The row in bold is the risky one. `_exclusive` is a defect boundary written
after a viewer complaint, and loosening it is the single most dangerous edit
in this plan.

## 3. Acceptance criteria

Not "replay works". These:

| | measured by |
|---|---|
| the house edit is byte-identical | `test_house_edit_is_unchanged` — the replay policy is neutral for `best_moments` |
| no undeclared repeat ever reaches a timeline | a test that feeds `_exclusive` an overlapping pair with no `replay_of` and asserts it is still trimmed |
| a replay is adjacent to its original | `ensure_chronological` extended, with a test for a replay placed anywhere else |
| bounded | at most one replay per N clips, never two adjacent; asserted |
| it fires at all | `scripts/baseline.py` reports replays per edit — **currently this would be 2 across 17 projects** |
| reproducible | two independent baseline runs identical |
| the judge does not fall | `judge_total` per style, before and after |

## 4. What could change the house edit

- **Nothing, if the policy is neutral for `best_moments`** — the pattern every
  phase since V2-P0 has used, and the frozen contract checks it by identity.
- **The `_exclusive` change is the exception.** It is a shared code path. A
  loosening that is subtly wrong changes every edit, including the house one,
  and the failure mode is footage shown twice — the exact defect a viewer has
  already caught once. This is why it needs a test that proves the *undeclared*
  case is still rejected, written before the loosening.
- The chronology extension is additive: a new allowance for a declared replay
  adjacent to its original. It cannot make an existing edit illegal.

## 5. The decision needed before any of this

**Two candidates out of 254 clips.** Building a three-stage chain, a new
policy, a config section, and a loosening of the EDL's repeat guard — for
0.8 % of clips.

Three honest options:

**(a) Build it as specified and accept it rarely fires.** The machinery would
be correct and would come alive on footage with sharper events. The cost is
real: a shared defect boundary is loosened today for a benefit that arrives
later, and the audit currently blocked on missing footage is the one that
would tell us whether event boundaries improve.

**(b) Fix what makes candidates scarce first.** ~~Resolving events have a
median span of 39.5 s. Narrowing them — the same class of problem as the coarse
transcript segments — would create candidates for replay *and* improve
`PAYOFF`, the pacing axis's `differs`, and the situations reader.~~

**Superseded.** Resolving events are a median of 12.5 s and were never the
coarse unit; the moment wrapped around them was. What this option was reaching
for is exactly the moment-to-event gap, and V2-P2.2 has since built it —
`EditorialEventSpan` locates a resolution inside a moment in 48 of 435 cases.
This option is therefore **done**, and the closure below measures what it
produced.

**(c) Take P2 out of order** and build `Humor` and `Competitive` doctrine
first. Both are style doctrine through the seam that already exists, both
touch no shared defect boundary, and neither depends on the classifier the way
Replay depends on event spans.

**My recommendation is (c), then (b), then Replay last** — the reverse of the
agreed order, on the strength of the 2-of-254 measurement. But the order was
agreed and this is a change to it, so it is the owner's call and no code is
written until it is made.

---

# CLOSED — 2026-09-02

**Replay is not built, and `_exclusive` is not touched.** The decision was made
against a metric fixed *before* the number was seen, which is the only way a
threshold means anything.

## The definition that was measured

A clip is a Replay Candidate only if **all** of these hold:

| | |
|---|---|
| `EditorialEventSpan.action` | established — not `None`, not in `unknown` |
| `action.confidence` | **≥ 0.80** |
| `EditorialEventSpan.resolution` | established |
| `resolution.confidence` | **≥ 0.80** |
| the clip's core span | **≤ 12.0 s** (`span.raw_duration`, which is what "core span" meant earlier in this document) |
| `reaction` | **not evidence.** A supplementary signal at most; it entered no condition |
| `ShotPurpose.PAYOFF` | **not a criterion.** Not on its own, and not in combination |

No replay-specific exception was used and no shared boundary was relaxed. The
measurement walked `editorial_reading.read(...)` — the story stage's own call,
not an approximation of it.

## The result

| | |
|---|---:|
| moments examined | **435** |
| **candidates under the official definition** | **0** |
| source footage | **10.21 h** (612.9 min; no recording is shared between projects) |
| **candidates per 10 minutes** | **0.0000** |
| **coverage** | **0 of 17 projects — 0 %** |
| per project, median / mean | 0 / 0.000 |

The rejection funnel, which matters more than the total because it shows that
nothing failed narrowly:

| first failing criterion | moments |
|---|---:|
| no `action` boundary at all | 227 |
| has an action, no `resolution` | 160 |
| action confidence below 0.80 | 12 |
| resolution confidence below 0.80 | 14 |
| **passes both confidence gates, core span over 12 s** | **22** |
| **accepted** | **0** |

Those 22 are the whole population of near-misses, and they are not near: the
**shortest is 23.5 seconds** against a 12-second rule, and their median is
81.4 s.

## Both thresholds failed

| threshold, fixed in advance | required | measured | |
|---|---|---|---|
| rate | ≥ 1.0 per 10 min | 0.0000 | **FAIL** |
| coverage | ≥ 60 % of projects | 0 % | **FAIL** |

## Sensitivity — the verdict does not depend on a judgement call

"Core span" could have been read as the action→resolution stretch rather than
the moment's own duration. It was measured both ways:

| reading | candidates | rate | coverage | |
|---|---:|---:|---:|---|
| moment duration (official) | **0** | 0.0000 | 0 % | FAIL / FAIL |
| action → resolution (alternative) | **8** | 0.1305 | 23.5 % | FAIL / FAIL |

**Upper bound.** Ignoring the core-span rule *and* both confidence floors
entirely, only **48 moments in the whole database carry an action and a
resolution together**. That is **0.7832 per 10 minutes** — below the 1.0
threshold before a single quality condition is applied. Reaching 1.0 would
need 61.3 candidates against a total stock of 48.

**So the threshold is structurally unreachable on this data.** No adjustment to
the definition reverses the result, which is why this is a closure rather than
a deferral.

## `ANGLE` remains unbuildable

Unchanged from §1 and worth restating in the closure: `ANGLE` needs a second
camera on the same instant, and this system records one. It would be an enum
member with no implementation — the orphaned-key pattern two phases were spent
removing.

## When this may be reopened

Only on a change in the **evidence**, never on a change of mind about the
threshold:

- the **source material changes** — a different game, capture setup, or
  recording style whose events are sharper than the ones measured here;
- **multi-angle footage** exists, which would make `ANGLE` implementable and
  change what a replay is for;
- **richer evidence** narrows the moment further, so that the action and
  resolution inside it are located often enough and tightly enough to clear
  0.80 confidence inside a short span.

Until one of those is true, the machinery would be correct and idle, and the
cost of getting there is a loosening of `_exclusive` — a defect boundary
written after a real viewer watched 25 seconds twice.
