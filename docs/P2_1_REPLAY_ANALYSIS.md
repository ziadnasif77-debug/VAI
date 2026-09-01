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
median span of 39.5 s.~~ **Superseded by the correction above:** resolving
events are a median of 12.5 s and were never the coarse unit. What this option
was reaching for is the moment-to-event gap, and V2-P2.2 has since built it —
`EditorialEventSpan` locates a resolution inside a moment in 36 of 435 cases,
which is what took replay candidates from 0 to 5. Narrowing them — the same class of problem as the coarse
transcript segments — would create candidates for replay *and* improve
`PAYOFF`, the pacing axis's `differs`, and the situations reader. It is not
Replay, and it is closer to the blocked P1.8 than to P2.

**(c) Take P2 out of order** and build `Humor` and `Competitive` doctrine
first. Both are style doctrine through the seam that already exists, both
touch no shared defect boundary, and neither depends on the classifier the way
Replay depends on event spans.

**My recommendation is (c), then (b), then Replay last** — the reverse of the
agreed order, on the strength of the 2-of-254 measurement. But the order was
agreed and this is a change to it, so it is the owner's call and no code is
written until it is made.
