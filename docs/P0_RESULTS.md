# P0 — what changed, in numbers

Measured against [BASELINE.md](BASELINE.md), on the same 17 projects and 6
styles, by the same harness reading the same code path the story stage uses.

```bash
python scripts/baseline.py --against .cache/baseline/<the-before-run>.json
```

## The one that had to not move

**The house edit is byte-identical on all 17 projects.** Selection and its
order, the winning counterfactual profile, the hook, the ending, the timeline's
clip boundaries after clamping, the finished length, and all eight judge axes —
unchanged, to the last decimal.

That is not a result of care. `EditingStrategy.resolve` returns the shared
neutral singletons for the house style with an unspoken brief, and `apply` then
returns **the caller's own list, by identity**. The frozen contract asserts
`shaped is moments`, so the short circuit is checked rather than assumed.

## before → after → delta

Averaged over the five non-house styles, all 17 projects.

| | before | after | delta |
|---|---:|---:|---:|
| **dead-time ratio** | 0.0000 | 0.2559 | **+0.2559** |
| median shot length | 53.47 s | 45.68 s | −7.78 s |
| shot variance | 33.71 s | 29.66 s | −4.05 s |
| repetition ratio | 0.2152 | 0.2284 | +0.0132 |
| hook strength | 0.0000 | 0.0000 | 0.0000 |
| ending strength | 0.4758 | 0.4804 | +0.0046 |
| cut-point quality | 0.1191 | 0.1297 | +0.0107 |
| sequence coherence | 0.9168 | 0.9181 | +0.0013 |
| judge total | 0.6652 | 0.6587 | −0.0064 |

**Style differentiation** — style-edits byte-identical to the house edit:

| | before | after |
|---|---:|---:|
| where the optimiser genuinely chooses | 15 / 45 | **2 / 45** |
| where all the footage fits (nothing to select) | 40 / 40 | **8 / 40** |
| overall | 55 / 85 (65 %) | **10 / 85 (12 %)** |

Character distance from the house edit, 0–1 over eight dimensions:

| style | before | after |
|---|---:|---:|
| gaming_fast | 0.058 | 0.321 |
| competitive | 0.080 | 0.314 |
| funny | 0.088 | 0.293 |
| minimal | 0.036 | 0.206 |
| cinematic | 0.019 | 0.035 |

## What the numbers mean

**Dead time went from structurally impossible to real.** It was 0.0000 on all
435 stored moments and could not be anything else: `_gaps_between` searches the
stretches no moment's context occupies, and `dead_time_ratio` then measures
their overlap with a moment's context window — empty by construction. It is not
repaired here. It is *redefined*, as the owner asked: a dead stretch is one
adding no context, no anticipation, no progression, no payoff and no reaction.
Each of the five is read from a different store, and deadness is what is left
after the strongest claim.

**The short-footage collapse is mostly fixed.** Eight projects hold less
footage than the target, so the optimiser keeps every moment and a style whose
only lever is selection has no lever at all — five styles, one identical video,
40 out of 40. The shot layer works whether or not anything was left to choose,
and 32 of those 40 now differ.

**Cut-point quality moved where a style asked it to.** `competitive` +0.055 and
`cinematic` +0.006 are the two styles that snap to seams; `funny` −0.019 is a
style that trims and does not snap, so its cuts move *off* seams. The house is
unchanged at 0.1188. The absolute level is low for everyone — about one cut in
eight lands on a boundary the footage already has — which is a number worth
attacking later and is not what P0 set out to move.

## The regression, named rather than smoothed

**The judge's pacing axis fell on the three styles that trim shots:**
gaming_fast 0.642 → 0.399, funny 0.611 → 0.492, competitive 0.580 → 0.473.
cinematic (+0.005) and minimal (+0.017) rose. The house is unchanged.

The axis scores `(longest − shortest) / mean` against an ideal of 1.2. Two
things are true about it:

1. **Absolute variance fell while relative spread rose.** Shot variance is down
   4.05 s — these edits have *more* uniform shot lengths in seconds. The axis
   divides by the mean, and trimming lowers the mean, so a style that shortens
   its shots is scored as more uneven for having done so.
2. **No edit this system makes is anywhere near the ideal.** Measured spreads
   run 1.85–2.23; the house's own is 1.888 against an ideal of 1.2. An axis
   whose target no output has ever approached is measuring distance from an
   authored guess, not quality.

`StyleJudgementConfig.ideal_shot_spread` was added so a style can declare its
own — the exact pattern `ideal_effects_per_minute` already exists for, because
the judge used to mark every minimal edit down for having no effects. Only
`funny` sets it. Three attempts to set it elsewhere were reverted, and why is
worth keeping:

- **gaming_fast and competitive at 0.9**, on the argument that a fast edit is
  steady. The measurement says they are the *most* uneven of the six (2.23 and
  2.11). The argument was wrong, so the numbers went.
- **cinematic at 1.5**, on the argument that a cinematic edit is deliberately
  uneven. It raised the pacing axis 0.61 → 0.70 and simultaneously raised the
  style's judge total enough that the house-shaped counterfactual profile
  started winning again — cinematic went back to producing the house edit
  exactly on four projects. **The judge's per-style taste decides which of the
  three profiles is rendered, so a change of taste is also a change of edit.**
  A number that improves a score and erases the style is not an improvement.

The pacing regression therefore stands, unfixed and explained. Tuning the
constant until it goes away would be fitting the metric.

## cinematic is still the weakest

0.019 → 0.035. It differs from the house edit on 5 projects of 17, mostly by a
clip or two. Its only shot-layer lever is snapping to seams, which does nothing
where no seam falls within two seconds.

What it actually asks for — *longer* shots — is something `ContextPolicy`
cannot give: that policy may decline air the moment stage created, never invent
more. Its real differentiation lives in the **pacing** doctrine
(`band_scale: 1.35`), which is consumed by the EDL stage, *after* the point
this harness measures. So every number here understates cinematic, and the
harness's limit is the finding rather than the style's.

## What was built

| | |
|---|---|
| `backend/editorial/semantics.py` | `ShotSemantics` — five editorial claims and the deadness left over, from stores that already exist. No model, no new table. |
| `backend/editorial/strategy.py` | `EditorialIntent`, `EditingStrategy`, `ContextPolicy`, `CutPolicy`, `DeadTimePolicy`, and `apply` — their single consumer. |
| `config/style.yaml` → `shots:` | What a style may ask of the shots themselves. The layer the Style Bible was missing. |
| `tests/integration/test_style_differentiation.py` | The complement of the frozen contract: five styles may not become one. |

Two defects were found and fixed on the way:

**`Moment` had no `id`.** It lives in `metadata`, which four call sites knew and
the editorial layer did not. `getattr(moment, "id", "")` returned the default,
so `EditorialReading` came back **empty on every real project for the whole of
V2-P11** — the Director's editorial clause, the situations, the shots, all of
it. No test caught it because every fixture declares an `id` field the real
class did not have: the stub was more capable than the thing it stood for.

**`dead_time_policy` and `context_preservation` had no consumer.** Both are
parsed from what the owner types, echoed back in the confirmation, stored, and
learned as preferences — and nothing in the editing pipeline had ever read
either. Someone could write "احذف الأجزاء الميتة", be told the policy was now
aggressive, and receive byte-identical footage. `EditingStrategy` is the
missing consumer; P0 added no new brief settings, it gave two existing ones
their first effect on a video.

## Not built, deliberately

**Replay** stays P2, as the corrected chain
`ReplayCandidate → ShouldReplay? → ReplayStrategy`, with no general exception to
the chronology constitution.

**`SequenceReading`** — rhythm, contrast, continuity, repetition and transition
quality *between* shots — is not here. P0's layers all read one shot at a time;
relations between shots are a different reading and the numbers above are what
the single-shot layer alone is worth.

**Hook and ending as independent editorial decisions**, built on the existing
`choose_hook`, are likewise not here. Hook strength is 0.0000 before and after
because `HookSelection.moment` is empty in every plan this harness builds —
worth understanding before anything is changed about it.
