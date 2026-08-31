# P1 — the joins between shots

Measured on the same 17 projects and 6 styles as [BASELINE.md](BASELINE.md),
by the same harness, on the story stage's own path.

Every reading before this phase was about **a shot**. `ShotSemantics` says what
one shot is for; the optimiser scores one moment at a time and adds a variety
term that is a share of the whole, blind to adjacency; the judge's coherence
axis counted long jumps and nothing else. None of them could see the thing an
editor sees first, which is not a shot but a **join**.

## pre-P0 → P0 → P1

Averaged over the five non-house styles.

| | pre-P0 | P0 | P1 | P0→P1 |
|---|---:|---:|---:|---:|
| dead-time ratio | 0.0000 | 0.2559 | 0.2573 | +0.0014 |
| median shot length | 53.47 s | 45.53 s | 45.15 s | −0.38 s |
| shot variance | 33.71 s | 29.63 s | 30.18 s | +0.55 s |
| repetition ratio | 0.2152 | 0.2307 | 0.2295 | −0.0011 |
| hook strength | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| ending strength | 0.4758 | 0.4772 | 0.4810 | +0.0038 |
| **judge total** | 0.6652 | 0.6558 | **0.7022** | **+0.0463** |
| — coherence | 0.9168 | 0.9195 | 0.9205 | +0.0011 |
| — structure | 0.9176 | 0.9176 | 0.9176 | 0.0000 |
| — **pacing** | 0.6091 | 0.5199 | **0.8226** | **+0.3027** |
| — variety | 0.6198 | 0.6180 | 0.6202 | +0.0021 |
| — intensity | 0.4190 | 0.4191 | 0.4190 | −0.0002 |
| — ending | 0.6664 | 0.6683 | 0.6736 | +0.0053 |
| — effect density | 0.3710 | 0.4184 | 0.4171 | −0.0013 |

**P0's one regression is gone, and not by retuning.** The pacing axis fell to
0.52 in P0 because it scored shot spread against an ideal of 1.2 and dividing
by the mean punished the styles that shorten shots. It now sits at 0.82 —
above where it started — because the axis was redefined rather than adjusted.

Style differentiation holds at **10 of 85** style-edits identical to the house
edit, unchanged from P0. P1 was not about differentiation and did not cost any.

## The sequence reading

Five relations, read at every seam. Nothing measured these before, so there is
no "before" column — the honest presentation of a new instrument.

| | house | five styles |
|---|---:|---:|
| rhythm | 0.833 | 0.835 |
| purposeful rhythm | 0.790 | 0.792 |
| contrast | 0.802 | 0.798 |
| continuity | 0.692 | 0.694 |
| repetition | 0.457 | 0.467 |
| **transition quality** | **0.047** | **0.072** |

The last row is the finding. **Fewer than one cut in fourteen lands on a
boundary the footage already has** — a scene change the analysis already found.
The two styles that snap to seams do measurably better (competitive 0.100,
gaming_fast 0.090) and everything else is close to noise. That is the largest
single number left on the table, and it is not what P1 set out to move.

## What the pacing axis means now

The definition is the contract. It holds **no ideal spread**, and names the two
ways a shot length fails to be a decision:

* **arbitrary variation** — the length changes and nothing else does;
* **a metronome** — four or more shots in a row that never change length.

Between those, a deliberately steady edit and a deliberately uneven one both
score well. That is the distinction between *uneven because badly edited* and
*uneven because cinematic*, which is the whole reason the axis was rewritten.

Six tests in `TestThePacingDefinitionItself` protect the definition rather than
the numbers, including one asserting that `IDEAL_SHOT_SPREAD` does not exist.

**The judge was also reading the sequence blind.** It ran the seam analysis
without the editorial reading, so two shots logged as one moment type read as
identical even when they were a payoff and the reaction to it. Measured here,
judging blind saw 11–13 arbitrary cuts on plans the reading scored at 2–5 — and
the judge ranks plans by what it sees. It is given the reading now.

## Where the video starts and stops

`choose_hook` moves the strongest moment to the front. That is a flash-forward,
chronology forbids it, and `chronological` defaults to true because the owner
asked for time order three separate times — so the hook is switched off in all
102 plans and hook strength is 0.0000 for that reason alone, before and after.

What a chronological edit *can* decide is where it begins and ends, because
moving the first and last index reorders nothing. `backend/editorial/bookends.py`
does that, and refuses three things:

- it never opens on an outcome, which would be a flash-forward under another
  name;
- it never drops a setup shot that shares a **situation** with the shot it
  would open on — the walk-up to the ambush is why the ambush lands;
- it never trims a reaction off the end, because a reaction is how an edit
  stops feeling like it ran out of footage.

It fires on 2 of 17 projects for `gaming_fast` — opening on a `clutch` (0.65)
instead of a `tension` (0.23), and on an `outplay` (0.54) instead of a `skill`
(0.29). Most edits already start reasonably; 2 of 17 is what a conservative
policy on real footage looks like.

Two errors in the guards were found by measuring rather than by reading. The
first checked whether the *first shot itself* was a setup, which made 14 of 17
projects unmovable; `SETUP` means several things were on screen, which is true
of most gameplay, so the guard was narrowed to a setup sharing a situation with
the candidate. The second asked whether the *surviving* last shot was a
reaction rather than the one being dropped, so it could only fire after the
reaction had already gone.

## The golden baseline moved, once

4 of 17 videos changed; 13 more have new recorded axis values with an identical
edit, because the golden file records the eight axes and one of them was
redefined. The full record, the reason, and the preserved pre-P1 file are in
[BASELINE.md](BASELINE.md#the-golden-baseline-changed-once-and-this-is-why).

Verified before freezing:

- the new baseline is **deterministic** — two independent runs produce all 102
  style-edits identically, sequence readings included;
- the frozen contract still **catches a real change** — a 0.25 s move on every
  cut point is caught on 17 of 17 projects.

## P1.4 — the reaction decision

Two hypotheses were measured and discarded before any code was written.

**Pairing by situation.** Only 14 of 33 reaction shots belong to a situation at
all, and just **2** of those situations contain another moment. There was
nothing to pair.

**Pairing by proximity.** The gap from a reaction shot to what precedes it has
a median of **21.2 s**. The gap for every other kind of shot: **21.1 s**.
Proximity distinguishes nothing, and building on it would have been the
merge-by-adjacency the situations layer already measured and refused.

Then the definition turned out to read the other way round. `ShotPurpose.REACTION`
means *somebody responded to this shot* — the response is in the seconds
**after** it, and the shot is the thing being reacted to.

### The defect underneath

`backend/evidence/projection.py` filtered records by whether their *start* fell
inside the span. For anything with a duration that is the wrong question, and
this machine's coarser transcripts have segments running to 392 seconds.

| | before | after |
|---|---:|---:|
| speech lane and transcript agree | 50 | **154** |
| lane claims speech, transcript has none | **104** | **0** |
| speech spans the cut-safety rule can see | — | **1,677** |
| cut-point quality, every style | 0.098 | **0.115** |

The middle row is the one that mattered: V2-P3's rule against cutting inside a
spoken word was blind to any sentence that began before the shot.

### ReactionPolicy

Of 22 selected shots somebody responded to, **18 stopped before the response
finished** — a median of 2.0 s short, with a median of 1,357 unused seconds of
recording sitting after the clip. So the policy holds a shot until the response
lands, bounded at 3 s. `funny` and `competitive` ask for it.

**Its first implementation was fake and the numbers said so.** It fired 33
times and every single hold was exactly 3.00 s — the bound, always — because it
took the latest end of every overlapping segment rather than the end of the
first one that *starts* after the shot. A policy whose output is a constant is
not a measurement. Corrected, it fires 9 times with real values: 1.5, 1.81,
2.6, 3.0.

| | P1 | P1.4 |
|---|---:|---:|
| cut-point quality | 0.1005 | 0.1150 |
| median shot — funny | 41.27 s | 41.51 s |
| median shot — competitive | 37.55 s | 37.78 s |
| judge total | 0.7022 | 0.7018 |
| **house videos changed** | — | **0** |

### A claim that had been shipped wrong

`semantics.py` and its tests said "fifteen of this machine's seventeen projects
have no semantic lanes". `load_timeline` says *"stored when they are current,
built when not"* — every project gets lanes and only two have them **cached**.
Both statements are corrected.

## P1.5 — rhythm and contrast

The layer that was asked for turned out to already exist. What was missing was
the ability to see it.

### Measured first, and it said no

**Rhythm and contrast barely vary by style** at the plan: a spread of 0.017 and
0.031 across six styles. **Flat runs are rare** — a longest run of unchanging
shot lengths with a median of 2 and a maximum of 5, so there was little
metronome to break.

**Same-type runs are long** — a median of 4, reaching 12 — against a configured
limit of `max_consecutive_same_type: 2`, and `repair_sequence` runs and emits
21 variety notes and still cannot fix them. That looked like the defect, and it
is not one:

| project | pool | dominant type |
|---|---|---|
| `proj-dc1cf6be95a3` | 45 surprise, 4 tension | **92 %** |
| `proj-86edde704be0` | 40 surprise, 7 tension | 85 % |
| `proj-f48db83dea86` | 13 surprise, 5 funny | 72 % |

No selection can break a run of eleven when forty-five of forty-nine moments
carry one label. `repair_sequence` giving up is the correct behaviour, and the
run is a fact about the footage.

Across the whole machine, `surprise` is **224 of 435 moments — 52 % of
everything ever detected**, and the dominant type's share has a median of 57 %.
Every contrast mechanism in the pipeline reads that field: the judge's variety
axis, `pacing.order`, `repair_sequence`, and this reading's own repetition.

**Reading contrast by purpose instead was tried and refuted.** Longest run by
moment type: median 3, mean 4.1. By shot purpose: median 4, mean 4.1. No
better. The hypothesis died before any code was written for it.

### The harness was measuring before the thing that sets rhythm

`backend.editorial.pacing_engine` re-reads every shot's length at the second it
starts on, and it is style-aware — `band_scale`, `stutter_relief`,
`stillness_relief`, `on_the_beat_seconds`. It runs at the **EDL** stage, after
the point this harness stopped. So every rhythm number reported in P0 and P1
was taken before the layer that decides rhythm.

Extending the harness through it, measuring the length that actually renders
(`min(planned, cap)`, which is how the EDL applies it):

| style | median rendered shot | spread | rhythm |
|---|---:|---:|---:|
| gaming_fast | **5.63 s** | 3.81 | **0.784** |
| funny | 6.57 s | 4.60 | 0.785 |
| competitive | 9.25 s | 4.12 | 0.641 |
| minimal | 10.03 s | 4.07 | 0.619 |
| best_moments | 10.85 s | 4.01 | 0.589 |
| **cinematic** | **13.74 s** | 5.62 | 0.628 |

| | at the plan | after pacing |
|---|---:|---:|
| rhythm spread across styles | 0.017 | **0.195** |
| median shot, shortest to longest | — | 5.63 s to 13.74 s (**2.4×**) |

### The conclusion this overturns

P0 and P1 both reported **`cinematic` as the weakest style**, at a character
distance of 0.019 rising to 0.035, and said so in three documents and to the
owner more than once. At the layer where shots are actually cut it has the
**longest shots of any style here** — 13.7 s against the house's 10.9 s — which
is exactly what its doctrine asks for and exactly what its description claims.

The style was never weak. The harness stopped one stage short of where that
style lives, and a conclusion drawn from a harness that stops before the
deciding layer is a conclusion about the harness. `docs/BASELINE.md` and
`docs/P0_RESULTS.md` carry the correction where the wrong claim was made.

**So P1.5 adds no policy.** Rhythm already differentiates by 2.4× on shot
length, contrast is bounded by a moment classifier that calls half of
everything `surprise`, and a rhythm policy layered on top of a working engine
would be a second opinion about a question already answered. What it adds is
the measurement, so the next claim about pace is checkable.

## P1.6 — visual continuity

`SequenceReading.continuity` claimed to answer "do adjacent shots belong to
each other" and answered a narrower question: same recording, gap under sixty
seconds. That is continuity **in time**. What a viewer experiences at a cut is
whether anything on screen survives it, and nothing measured that.

The vision stage records 32 activity labels — `driving`, `combat`, `inventory`,
`menu`, `exploration`, `low_health` and the rest — and 94 % of selected shots
carry them. So the reading exists to be made.

### The two are not the same question

| | share of cuts |
|---|---:|
| continuous in time **and** in look | 61 % |
| continuous in time, a jump in look | 5 % |
| **continuous in look, a jump in time** | **32 %** |
| neither | 2 % |

They agree on **64 %**. The large cell is the same activity minutes apart —
smooth to watch, and a break by the temporal reading. Collapsing the two would
lose exactly that, so `visual_continuity` sits beside `continuity` rather than
replacing it, with `looked_at` alongside because a share over three cuts and a
share over three hundred are not the same claim.

An unobserved side reads as `None`, never as a jump: the vision stage runs only
on nominated regions, and reporting "nobody looked" as "nothing survived" would
invent visual jumps out of sparse coverage.

### And then it says there is nothing to fix

| style | visual continuity |
|---|---:|
| minimal | 0.966 |
| gaming_fast | 0.962 |
| competitive | 0.958 |
| funny | 0.958 |
| cinematic | 0.952 |
| best_moments | 0.949 |

**0.95 to 0.97, a spread of 0.017.** Across all 17 projects only 15 cuts of 237
share no activity at all, and each of those is a one-off — `combat → driving`,
`exploration → combat`. Visual continuity is neither broken nor a lever a style
could pull.

### The number that looked like the biggest opportunity

`transition_quality` — the share of cuts landing on a boundary the footage
already has — is 0.047 for the house style, and P1 called it "the largest
single number left on the table". Measured properly it is largely unreachable:

| | |
|---|---|
| scene boundaries | one every 15.0 s on average, median 9.1 s |
| distance from a cut to the nearest one | **median 7.2 s** |
| cuts within the 0.5 s tolerance | 16 % |
| cuts within the 2.0 s maximum drift | **25 %** |

A cut is placed where the editing wants it, and the nearest scene change is a
median of seven seconds away. `CutPolicy` can reach a quarter of them; the rest
would need a drift bound so wide that snapping would stop improving a cut and
start choosing different footage, which is the optimiser's job and the reason
the bound exists.

So the low number is the scene detector's density against the freedom the
optimiser needs, not a defect in the cut policy. The two styles that snap do
measurably better within that ceiling — `competitive` 0.099 and `gaming_fast`
0.090 against the house's 0.047 — which is the policy working to its limit.

### Two phases, two negative findings

P1.5 and P1.6 both add a measurement and decline to add a policy, and that is
worth stating rather than dressing up. Rhythm already differentiates 2.4× at a
layer the harness could not see. Visual continuity is at 95 % and uniform.
Transition quality is capped by scene density. The remaining headroom in this
part of the system is small, and knowing that is what the measuring was for.

## Not built

**P1.4 reaction, P1.5 rhythm and contrast, P1.6 visual continuity** are
measured and do not yet act. Each would need another change to the house edit,
and stacking editorial changes before measuring them is what this whole
sequence of phases exists to avoid.

**Replay stays P2**, as `ReplayCandidate → ShouldReplay? → ReplayStrategy`,
with no general exception to the chronology constitution.
