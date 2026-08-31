# The edit this system makes, measured before changing it

Everything in the professional-editing upgrade arrives holding an argument for
why the result is better. This document is what those arguments are checked
against: the exact edit the system produced on 2026-08-31, over every project
on this machine, in numbers taken from the pipeline's own code path.

```bash
python scripts/baseline.py                    # measure now, save it
python scripts/baseline.py --against FILE     # measure now, diff a saved run
python scripts/baseline.py --freeze           # rewrite the regression contract
```

## What it measures, and by which path

Not an approximation of the story stage — the story stage. With counterfactuals
enabled, which is the shipped setting, that stage does not build one plan: it
builds one per profile, judges all three on eight axes, and renders the winner.
The harness calls `propose` → `judge` → `best`, then lays the winner out with
`build_timeline`, so the boundaries recorded are the ones a viewer would see
after clamping, de-overlapping and transitions — not the ones the plan asked
for.

Two deliberate omissions, stated rather than hidden:

- **The Director is not offered.** It is a model call, its answer varies between
  runs, and a baseline whose numbers move on their own protects nothing. What
  it contributes is ordering, which the chronology constitution already bounds.
- **The screen guard is not applied.** It reads the vision store, which would
  make these numbers depend on analysis state. It can drop clips, so a render
  may hold fewer than the count here.

Nothing is rendered and nothing is re-analysed. Planning reads stored moments
and takes milliseconds, which is what makes this worth running after every
change rather than once.

## The numbers, before P0

17 projects, 6 styles, 102 edits.

### Styles differentiate only where the optimiser can choose

| | projects | style-edits identical to the house edit |
|---|---|---|
| footage exceeds the target | 9 | 15 of 45 — **33 %** |
| footage is shorter than the target | 8 | 40 of 40 — **100 %** |

The second row is the finding. When a session holds less footage than the
target length, the optimiser keeps every moment, and a policy whose only lever
is *which moments to keep* has no lever left. Five distinct styles produce one
identical video, and no amount of tuning the multipliers can change that,
because the selection was never a choice.

**This is the strongest argument in the data for P0.** `EditingStrategy`,
`CutPolicy` and `ContextPolicy` change *how* a shot is cut, which still works
when selection is forced. Nothing that only reweights selection can reach these
eight projects at all.

### How far each style is from the house style

Character distance, 0–1, over the nine binding projects — normalised across
intensity, pacing, context ratio, variety, reaction usage, dead-time tolerance,
repetition tolerance and structure:

| style | mean | max | identical to house |
|---|---|---|---|
| funny | 0.166 | 0.292 | 0 of 9 |
| competitive | 0.151 | 0.334 | 1 of 9 |
| gaming_fast | 0.109 | 0.249 | 3 of 9 |
| minimal | 0.068 | 0.275 | 5 of 9 |
| **cinematic** | **0.035** | 0.193 | **6 of 9** |

`cinematic` is the weakest: it produces the house edit in two projects out of
three, and where it differs it differs by two clips. A style whose whole claim
is a different pace is not currently making one.

### `dead_time_score` is zero on every moment ever stored, and must be

435 moments in the database. Every one has `dead_time_score = 0.0`. Every
dead-time job ever run reports 100 % of its segments protected — 2/2, 14/14,
16/16, 25/25, 6/6, 4/4.

Not a threshold problem. `_gaps_between()` builds dead segments from *the
stretches no moment's context occupies*; `dead_time_ratio()` then measures how
much those segments overlap a moment's context window. The producer and the
consumer are looking at disjoint regions of the timeline **by construction**,
so the answer is necessarily zero.

What that costs, today:

- `backend/narrative/optimizer.py:316` subtracts `penalties.dead_time * 0` on
  every moment of every edit.
- `SelectionPolicy.dead_time_penalty` — one of the five bounded multipliers a
  style is allowed to move — has never changed a single frame of a single
  video, in any style, on any project.

Recorded here rather than fixed. Giving it teeth changes edits, and a change to
the house edit is a decision, not a repair made in passing.

## The regression contract

`tests/integration/test_house_edit_frozen.py`, against
`tests/golden/house_edit.json`.

The default style's edit is frozen for all 17 projects — selection and its
order, the winning profile, the hook, the ending, the timeline's clip
boundaries after clamping, the finished length, and the eight judge axes.
Exactly, not approximately: a change that moves one boundary by 40 ms is a
change to the finished video, and the point of freezing it is that nobody gets
to decide afterwards that a difference was too small to matter.

Only the default style. The other five exist to differ, and freezing them would
turn every deliberate improvement to a style into a failing test. What holds
them is the identity table above.

**Measured sensitivity**, because a green test that cannot go red is worthless:

| a change of | is caught on |
|---|---|
| 2 % on one selection multiplier | 2 of 17 projects |
| 10 % on one selection multiplier | 6 of 17 |
| 50 % on one selection multiplier | 8 of 17 |
| **0.25 s on every cut point** | **17 of 17** |

The selection ceiling of 8 is the starved-project finding again: on nine
projects no weight can move a selection that was never a choice. The last row
is the one that matters for P0, whose changes are cut points and context — the
contract sees those everywhere.

## Replay, as corrected

Recorded now so P2 is built from the corrected design rather than the first
one. **Replay is not "show the same shot twice."** It is a chain, and each link
can refuse:

```
ReplayCandidate  ->  ShouldReplay?  ->  ReplayStrategy
                                        NONE | MICRO_REPLAY | SLOW_REPLAY
                                        FULL_REPLAY | ANGLE
```

The strategy is style-dependent — what `competitive` replays and how is not
what `cinematic` does — and it resolves through the same
`ResolvedEditingPolicy` seam as everything else, so the optimiser continues to
consume a typed value rather than reading a taste.

**It is not a general exception to the chronology constitution.** The
constitution has exactly one exception, the leading run of cold-open hook
clips, and it stays that way. A replay is a bounded, declared re-use of a span
already shown, not a licence to reorder.

## The golden baseline changed once, and this is why

**V2-P1, 4 of 17 projects.** Not a cosmetic change, not a drifting
number, and not a test made green: the pacing metric's **definition** was
wrong, correcting it changed which of the three counterfactual plans the judge
prefers, and the plan the judge prefers is the plan that gets rendered.

The old axis scored `(longest - shortest) / mean` against an ideal of 1.2 -- a
constant no edit this system has ever made comes near, the house style's own
spread being 1.888. It marked every edit down for being what every edit is, and
marked the styles that trim shots down *further* for succeeding, because
shortening shots lowers the mean the spread is divided by.

The corrected axis holds no ideal at all. It names the two ways a shot length
fails to be a decision:

* **arbitrary variation** -- the length changes and nothing else does;
* **a metronome** -- four or more shots in a row that never change length.

Between those, a deliberately steady edit and a deliberately uneven one both
score well, which is the distinction between *uneven because badly edited* and
*uneven because cinematic* that this correction exists to make.

A second correction went with it: **the judge was reading the sequence blind.**
It ran the seam analysis without the editorial reading, so two shots logged as
one moment type read as identical even when they were a payoff and the reaction
to it. Measured here, judging blind saw 11-13 arbitrary cuts on plans the
reading scored at 2-5, and the judge ranks plans by what it sees.

### What moved, project by project

| project | winning profile | clips | pacing (new metric) | arbitrary cuts | purposeful rhythm |
|---|---|---|---|---|---|
| `proj-67d1d971b60` | C to A | 18 to 19 | 0.824 to 0.833 | 3/17 to 3/18 | 0.812 to 0.812 |
| `proj-86edde704be` | A to C | 20 to 15 | 0.474 to 0.643 | 10/19 to 5/14 | 0.444 to 0.545 |
| `proj-bd2db7f630a` | A to C | 27 to 24 | 0.771 to 0.870 | 5/26 to 3/23 | 0.722 to 0.824 |
| `proj-ca4d0eac9d6` | B to A | 18 to 18 | 0.827 to 0.886 | 2/17 to 1/17 | 0.818 to 0.917 |

Each has a lower share of arbitrary cuts, higher purposeful rhythm, or both.
The full record is [p1-golden-change.json](p1-golden-change.json), written by
the same code that produced this table.

> An earlier report of this change said **five** projects moved and all five
> improved. That was measured with the judge still blind while the checking
> script had the reading -- an inconsistent comparison, and one of the five
> (`proj-dc1cf6be95a3`) had *more* arbitrary cuts under its new winner. Once
> the judge was given the same reading the checker had, that project stopped
> changing at all and four remain. The table above is the corrected figure.

### The pre-P1 baseline is kept

`tests/golden/house_edit.pre-p1.json` holds the house edit exactly as it stood
before any of this. Nothing reads it as a contract and nothing regenerates it;
it exists so the difference can be shown rather than asserted. The live
contract remains `house_edit.json`.

## What P0 had to prove, and what it did

Not "the code is more sophisticated." These four, in numbers. The results are
in [P0_RESULTS.md](P0_RESULTS.md); the verdict on each is here.

1. **The frozen house edit is byte-identical.** Met, on all 17 projects, by
   construction rather than by care — a neutral strategy returns the caller's
   own list and the contract asserts `shaped is moments`.
2. **`cinematic` stops producing the house edit in 6 of 9 projects.** Partly
   met: 6 of 9 became 5 of 17, and its character distance moved 0.019 to 0.035,
   the smallest gain of the five. Its real lever is the pacing doctrine,
   consumed after the point this harness measures.
3. **The eight starved projects stop producing one identical video.** Met: 40
   of 40 identical became 8 of 40.
4. **No judge axis regresses on any project under any style.** *Not met in P0*,
   and fixed in P1 by correcting the axis rather than the numbers — see the
   section above. In P0 the pacing axis fell on the three styles that trim
   shots (gaming_fast 0.642 → 0.399, funny 0.611 → 0.492, competitive 0.580 →
   0.473), because the axis divided spread by the mean and shortening shots
   lowers the mean. P1's redefinition brings all five back above 0.81.
