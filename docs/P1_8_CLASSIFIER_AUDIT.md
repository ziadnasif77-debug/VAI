# P1.8 — the moment classifier, audited

No code was changed to produce this. It is the analysis the owner asked for
before any redesign, and its central finding is that there is nothing to
redesign yet: **`surprise` is not a classification.**

## 1. The distribution

All 435 moments this machine has stored.

| moment type | count | share | every event unnamed | has a named event | mean confidence |
|---|---:|---:|---:|---:|---:|
| **surprise** | **224** | **51.5 %** | **224** | **0** | **0.059** |
| chaos | 71 | 16.3 % | 0 | 71 | 0.917 |
| tension | 63 | 14.5 % | 0 | 63 | 0.920 |
| skill | 28 | 6.4 % | 0 | 28 | 0.898 |
| fail | 13 | 3.0 % | 0 | 13 | 0.937 |
| defeat | 9 | 2.1 % | 0 | 9 | 0.952 |
| clutch | 7 | 1.6 % | 0 | 7 | 0.954 |
| rare | 6 | 1.4 % | 0 | 6 | 0.928 |
| funny | 6 | 1.4 % | 0 | 6 | 0.930 |
| outplay | 4 | 0.9 % | 0 | 4 | 0.878 |
| comeback | 2 | 0.5 % | 0 | 2 | 0.816 |
| boss / victory | 1 each | 0.2 % | 0 | 1 each | 0.970 |

Two columns carry the whole audit. **Every `surprise` moment — 224 of 224 —
has no named event in it**, and no moment of any other type is in that state.
And its mean confidence is **0.059** against 0.88–0.97 everywhere else, a
fifteen-fold gap.

Per project, the dominant type's share has a median of 57 % and a maximum of
92 % (`proj-dc1cf6be95a3`: 45 surprise, 4 tension, and nothing else across 49
moments).

## 2. The cause, in the code

`backend/moments/formation.py`, line 67:

```python
GameEventType.UNKNOWN_EVENT: MomentType.SURPRISE,
```

and `moment_type_for()` falls back to the same value when no event maps at all.
Its own docstring is exact:

> A group of nothing but unnamed events is a SURPRISE, which is the taxonomy's
> way of saying "something happened here"

So `surprise` is the **null label**. It is not produced by novelty, rarity,
game-state transition, sudden damage, camera motion, a score threshold or any
combination of signals. It is produced by the event correlator failing to name
what it found, and it was given an editorial-sounding word.

The 100 % figure in the table above is that line, measured rather than assumed.

## 3. The concepts this word is standing in for

The owner asked that these stop sharing a name, and they are genuinely
different questions with genuinely different evidence:

| | what it means | what could answer it |
|---|---|---|
| **unnamed** | the correlator found something and could not say what | already known — `confidence 0.059`, and the event types present |
| **novelty** | new relative to what this session has shown | the semantic timeline's `novelty` lane, which exists and nothing reads |
| **surprise** | *unexpected* relative to what the context predicted | nothing measures this today; it needs a model of what was expected |
| **highlight** | high editorial value, worth keeping | the score, and `ShotSemantics`'s five claims |
| **payoff** | the resolution of an arc that was building | `ShotSemantics.payoff`, built in V2-P0 |
| **reaction** | somebody responded to it | `ShotSemantics.reaction`, built in V2-P0 |

Four of the six are already measured elsewhere. **Only "unnamed" is what the
current `surprise` actually is**, and the one concept nothing measures — real
surprise, against an expectation — is the one the word claims.

## 4. What the golden dataset can and cannot settle

Three annotated windows, ten minutes each, **53 spans**: 24 `event` (each with
an `event_type`), 12 `highlight`, 9 `boring`, 8 `game_state`.

`scripts/evaluate.py` already scores against them:

| window | event precision | event recall | moment precision | moment recall | generic markers excluded |
|---|---:|---:|---:|---:|---:|
| gta_v 30–40 | 0.35 | 1.00 | 0.25 | 1.00 | 5 |
| gta_v 40–50 | 0.50 | 0.73 | 0.31 | 0.80 | 5 |
| grounded 20–30 | 0.60 | 1.00 | 0.80 | 1.00 | 22 |

Recall is high and precision is low: the detector finds what a person labelled
and claims a good deal more besides.

**But the last column is the problem.** The evaluator prints, by design:

> *a claim that names nothing cannot be wrong about what happened*

so it **excludes generic markers from scoring**. Thirty-two of them across the
three windows. The confusion matrix therefore omits precisely the category
under audit, and no precision or recall figure for `surprise` exists or can
exist from this dataset as it stands.

### What the labels do hint, and why it is only a hint

Moments falling inside the annotated windows, against the `highlight` and
`boring` spans:

| | highlight | boring | neither | total |
|---|---:|---:|---:|---:|
| named | 19 (68 %) | 5 (18 %) | 4 | 28 |
| **unnamed** | **0** | **3 (100 %)** | 0 | **3** |

Three samples. The direction agrees with everything else here and it is not
evidence. It is stated because it is what the existing ground truth can say,
and because inventing more from the system's own output is what the owner
explicitly ruled out.

### What needs labelling, precisely

The windows contain 3 unnamed moments; the machine holds 224. **The golden
dataset barely covers the category under audit**, which is why no precision
figure exists for it.

What would settle it, and nothing less:

1. **Annotate spans the pipeline called `surprise` and a person did not label
   at all** — the 32 excluded generic markers are the sampling frame. For each:
   is anything happening, and is it worth keeping?
2. **Two more windows on a recording where `surprise` dominates**
   (`proj-dc1cf6be95a3`, 92 % surprise, or `proj-86edde704be0`, 85 %). The
   three existing windows come from recordings where the detector does
   comparatively well, which is why 22 of the 32 excluded markers are in one
   of them.
3. **Nothing else.** `highlight` and `boring` already exist and are the right
   vocabulary; the gap is coverage, not schema.

Roughly 20 minutes of annotation on the right two recordings would take the
unnamed sample from 3 to a number worth dividing by.

## 5. What it costs downstream, today

The confidence field is honest and partly consumed: `scoring.py` applies a
`low_confidence` penalty, and `needs_review` is stored and surfaced. But
**`moment_type` is read as a meaningful editorial label, independent of
confidence**, by every layer above it:

| reads the type | what it does with `surprise` |
|---|---|
| `narrative/hook.py` | `HOOK_STRENGTH[SURPRISE] = 0.8` — the third highest of any type |
| `narrative/story.py` | `SURPRISE` is in the hook-eligible set |
| `moments/scoring.py` | weight 0.65 |
| `narrative/pacing.py` | weight 0.7, and same-type run limits |
| `moments/context.py` | context expansion (0.9, 1.3) |
| `narrative/judge.py` | the variety axis counts distinct types |
| `editorial/sequence.py` | contrast and repetition |

The result, measured over all 17 projects:

| | |
|---|---:|
| unnamed share of the pool | 51 % |
| unnamed share of selected clips | 45 % |
| **unnamed share of finished runtime** | **35 %** |
| edits opening on an unnamed shot | **6 of 17** |

A third of every finished video is footage the system could not name, and more
than a third of the videos **open** on it — because the null label was given a
hook strength of 0.8.

## 6. The smallest change that would address it

Proposed, not implemented, and deliberately not a rewrite.

**The classifier is not miscalibrated. It is correct and it is being
misread.** It already says "I do not know" twice — once as `unknown_event`, and
once as `confidence: 0.059`. What is missing is that the layers above treat the
absence of a type as a type.

So the minimal change is to make the distinction visible where it is consumed,
not to invent a better classifier:

1. **Say it once, in the model.** A moment knows whether its type was derived
   from a named event. One property, no new field to drift.
2. **Let the layers that treat type as editorial meaning consult it.** The
   hook first, because `HOOK_STRENGTH = 0.8` on a null label is the sharpest
   defect here and the one a viewer meets in the first ten seconds. Then
   variety, which counts a category that means "unclassified" as a kind of
   thing.
3. **Leave selection alone.** The score already carries the low-confidence
   penalty. Adding a second penalty for the same fact would be double-counting,
   and it would change the house edit for a reason that is not new evidence.

What that does **not** do is rename or re-derive `surprise`. Renaming it
`unclassified` would be honest and would break every stored moment, every
style doctrine that names it, and the interaction parser's Arabic vocabulary.
That is a migration, and it should follow the behavioural fix rather than
precede it.

## 7. Acceptance, restated as measurements

Per the owner's conditions — **a lower `surprise` share is explicitly not
success**:

| | how it will be measured |
|---|---|
| more accurate on the golden dataset | `scripts/evaluate.py` on all three windows; event precision must rise from 0.35 / 0.50 / 0.60 |
| sensible distribution across projects | dominant-type share, currently a 57 % median and a 92 % maximum |
| fewer false positives | "claimed and not labelled": currently 13 / 8 / 4 |
| recall must not fall | currently 1.00 / 0.73 / 1.00 |
| the house style must not break | `test_house_edit_is_unchanged`, and separately the judge test |
| P1 behaviour changes only where the meaning genuinely improved | `scripts/baseline.py --against`, with every change named |
| reproducible across two runs | two independent baseline runs, all 102 style-edits identical |

**None of these can be evaluated for `surprise` itself until the labelling gap
in §4 is closed.** That is the first piece of work, and it is not code.
