# Controlled tuning

The only mechanism in this project permitted to change a decision without a
person asking. Everything about it is built to make that permission small.

**It has never run.** The switch is off, no video has been measured, and the
ledger is empty. That is the state described here, not a state to be reached
later and then documented.

## What it may do

Move one value in `config/style.yaml` by a small amount, inside the range that
file declares, because a comparison of measured videos suggested it — and record
the change as a row that can be undone with one command.

That is the whole of it. It cannot add a value, remove one, invent a key, edit
the file, change a stage, or touch anything outside `style.limits`.

## The six guards

Each is a separate refusal with its own sentence, because "the tuner declined"
is useless and "the step was 0.31 of a range that allows 0.15" is actionable.

| Guard | Rule |
|---|---|
| **Bounded** | the key must be one `style.limits` declares, and the result must land inside it |
| **Small** | one adjustment moves a value by at most `max_step_fraction` (10%) of its range |
| **Evidenced** | at least `minimum_videos` (15) measured videos, with `minimum_per_arm` (5) on each side |
| **Reversible** | the file is never rewritten; a delta is a row, and undoing it is marking that row |
| **Documented** | `reason` and `evidence` are required — a number that changed for reasons nobody wrote down is indistinguishable from a bug |
| **Switched** | `style.tuning.enabled` is off, and turning it on does not bypass the other five |

And one that is really arithmetic: **a delta is always relative to the file**,
never to the previous delta. Cumulative steps creep — ten legal tenths would
leave the fence while each one looked reasonable. Base-relative means the total
displacement is bounded by the declared range however many times a key moves.

The bound is checked twice: when the delta is written, and again when the value
is read. The file can be edited between those two moments, and a step that was
legal against yesterday's base need not be legal against today's.

## Using it

```bash
python scripts/tuning.py status
python scripts/tuning.py propose
python scripts/tuning.py apply pacing.band_scale
python scripts/tuning.py revert --all
```

`revert --all` is the command that matters. A mechanism that can change the
channel needs a way back that takes one command and no thought, and it works
whatever state the ledger is in — because the file was never rewritten, putting
things back is marking rows rather than reconstructing anything.

Today `status` prints:

```
switch          OFF
measured videos 0 (need 15 to propose)
largest step    10% of a key's declared range
cooldown        5 video(s) before a key moves again
tunable keys    14

in force        0 adjustment(s)
  nothing: every style is exactly what config/style.yaml says
```

## What a proposal is

A comparison, written down. For one key it gathers every measured video, groups
them by the value that key had **when each was cut** — P8's stamp keeps the whole
resolved body, so that value is recoverable even after the file changes — and
asks whether one group held viewers longer.

It is **not a significance test**. Fifteen videos in two groups is not a sample
anyone should compute a p-value from, and dressing a hunch in arithmetic would
be worse than saying nothing. What it produces is "these five did better than
those six by this much", and a person decides what to make of it.

It is **not a model**: nothing is fitted, nothing is predicted, and nothing
extrapolates past values that have actually been tried. A value that has never
been used cannot be proposed, because there is no evidence about it.

It is **not a licence**: a proposal changes nothing on its own. Applying it goes
through the ledger, and every guard still has to pass.

## Why the metric is what it is

`averageViewPercentage` — the share of the video a viewer actually watched. Views
measure the thumbnail and the title. Likes measure the audience's mood. The
share watched is the least gameable single number that says anything about the
edit, which is the only thing this mechanism can change.

It is still one number, and one number is a poor summary of a video. That is an
argument for the guards, not against the metric.

## Turning it on

1. Grant the analytics scope (see [ANALYTICS.md](ANALYTICS.md)) and publish
   videos through this system, so outcomes accumulate.
2. Wait for fifteen measured videos, with at least five on each side of
   whatever value you want compared. In practice this means deliberately
   cutting some videos one way and some another — a value that has only ever
   been used one way can never be judged.
3. Read `python scripts/tuning.py propose` and decide whether you agree.
4. Only then set `style.tuning.enabled: true`.

Step 3 is not ceremony. A mechanism this small can still be wrong, and the
first thing it proposes deserves to be argued with before it is trusted.
