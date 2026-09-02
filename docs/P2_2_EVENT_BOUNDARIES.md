# V2-P2.2 — event boundaries, and whether anything uses them

Two pieces of work. A data-integrity fix that restored more than half the
evidence this system had been silently discarding, and a derived layer that
locates the thing a moment is about. Then a verification pass whose finding is
the most useful thing here: **the layer is read by exactly one consumer, and
the route to the others is not the one it looked like.**

## Part 1 — the evidence that was being dropped

`GameEventType.UNKNOWN_EVENT` was called `unexpected_event` until V2-P2. That
phase migrated the `game_events` table and did not migrate the `event_ids` JSON
on `moments`. The loader filtered stored names against the enum and dropped
whatever did not match, without a word.

| | before | after |
|---|---:|---:|
| event references loaded | 496 | **1,119** (all of them) |
| dropped silently | **623** (56 %) | **0** |
| moments loading with no events | **210 / 435** | **0** |
| `Moment.importance`, every type | 0.000 | surprise 0.330, skill 0.658, victory 0.901 |
| `surprise` confidence | 0.059 | **0.816** (the stored value) |

The confidence row is why this mattered beyond a count. `Moment.confidence` is
`max(event.confidence, default=0.0)`, so a moment stripped of its events read
0.059 — and V2-P1.8's audit drew a conclusion from that gap before noticing
where the number came from. The audit carries the correction.

**No edit changed.** Every consumer that counts events filters to *named* ones,
and all 623 restored references are `unknown_event`. Evidence restored,
behaviour untouched: house edit identical on all 17 projects, every style metric
identical to three decimals.

The fix is a read-time rename, so no stored row is rewritten and an older
database keeps working. Nothing is dropped without being named:
`_stored_types` returns `(resolved, unresolvable)` and the unresolvable are
logged. `_restore` matches events to a moment by span **and** type multiset
together, and falls back to placeholders rather than substituting a different
event — 362 of 435 matched, 73 fell back, and the 73 are the guard working.

## Part 2 — `EditorialEventSpan`

Derived, never stored, never a replacement. `game_events` remains the truth;
`Moment` remains the unit. This answers what neither can: inside a moment that
runs for three minutes, where is the thing it is about?

Four boundaries, each carrying a timestamp, a confidence, the store that
supplied it and a sentence. Evidence is consulted strongest-first: the events'
own spans, then the tension lane, then vision and motion, then audio.

| | established | of 435 |
|---|---:|---:|
| onset | 208 | 48 % |
| action | 208 | 48 % |
| resolution | **48** | **11 %** |
| aftermath | **17** | **4 %** |

> **Corrected 2026-09-02.** This table first read `resolution 36 (8 %)` and
> `aftermath 11 (3 %)`. Those counts were taken **before the resolution
> priority fix described two sections below** — the one where `evidence.resolves`
> was consulted ahead of the located resolution, so a tension fall of 0.016
> preempted a victory sitting at 0.89 confidence. That fix is what took
> `PAYOFF` from 3 to 9, and it necessarily raised these two rows as well; the
> table was simply not re-run after it.
>
> Re-measured through `editorial_reading.read` — the story stage's own call —
> while fixing the Replay metric. The corrected numbers are **higher**, so they
> argue *against* the conclusion the Replay measurement reached, which is the
> reason to trust them rather than a reason to doubt them.

The other 227 are moments of a single event that fills them. There is nothing
inside to locate and the span says so in `unknown` rather than producing four
boundaries around a number nobody measured.

### The case it was built for

A moment running 2935.0 → 3112.9, labelled `victory`, 177.9 seconds long:

```
onset       2935.0   conf 0.97  [events]  the first event of this moment
action      3000.8   conf 0.89  [events]  the heaviest event here is a victory (0.90)
resolution  3011.7   conf 0.89  [events]  a victory ends here, 43% through the moment
aftermath   —        not established, and not invented
```

The assertion that matters in its test is the **negative** one: the resolution
is the victory event's own end, and is not the moment's midpoint, its end, or
anything derived from the word "victory" appearing on the label.

### Two defects in the first implementation

**The aftermath was a phantom.** It returned the moment's end at confidence 0.5
whenever a resolution existed, with a reason reading "the moment runs on after
the resolution" — the raw end wearing a boundary's name. A label with no fact
under it. Removed: no speech after, no aftermath, and `unknown` says so.

**The evidence was consulted in the wrong order**, and this one changed the
numbers. `_payoff` checked `evidence.resolves` before the located resolution.
`resolves` is true for *any* tension decrease, so a fall of 0.016 returned a
payoff of 0.032 and a located victory at confidence 0.89 was never reached. A
weak signal preempting a strong one is not a fallback chain, it is a
first-match-wins list in the wrong order. Corrected, PAYOFF went from 3 to 9.

## Part 3 — the verification, and what it found

**The editorial span has exactly one consumer.** Traced through every reader:

| layer | reads the span? | reads instead |
|---|---|---|
| `_payoff`, `semantics.py:439` | **yes** | — |
| `best_in` / `best_out` | no | `self.into` / `self.out_of`, scene seams |
| `ContextPolicy.bounds_for` | no | `semantics.purpose` |
| `pacing_engine` | no | `PacingContext` |
| `SequenceReading` | no | the raw moment spans |

So it is semantic annotation with one exception, which is precisely what this
pass was asked to check.

### Why cut-point quality did not move

0.1147 → 0.1139. Three independent causes, all measured:

1. **Seams are sparse.** One every 9–15 s; the median cut is 7.2 s from the
   nearest, and only 25 % are within the 2 s drift bound.
2. **Speech forbids nearly half of the ones that exist.** 45 % of candidate
   out-seams are unsafe, and **22 % of all of them are blocked by a single
   speech span longer than 30 seconds** — 154 coarse spans (9 % of the
   transcript) blocking 877 seams.
3. **Nothing in the cut path reads the boundaries.**

### Connecting it was tried, measured, and reverted

The opportunity looked real: of 48 moments with a located resolution, 17 have a
safe seam within 20 s of it that differs from today's cut, and the shot would
stop a median of **26 seconds earlier**. On the victory case the shot runs
**110 seconds past its own resolution**.

Wiring it into `CutPolicy` produced **zero change**, and the reason is the
`max_drift` guard: moving the out-point to just after the resolution is a
54.7-second move against a 1.5-second bound.

**The guard is right.** Snapping a cut to a seam a second or two away and
ending a shot on its point are different acts. The first is what `max_drift`
bounds. The second is trimming a shot's tail, which is `ContextPolicy`'s job
and is bounded at `MAX_TRIM_FRACTION` — where the same 54.7 seconds is 30 % of
the shot, inside the fence.

So the change was reverted rather than the bound widened. Widening a defence to
let one feature through turns a guard into a formality, and the parameter stays
on the signature with the reasoning recorded next to it.

## The numbers

| | before | after |
|---|---:|---:|
| **PAYOFF** | 3 | **9** |
| **Replay candidates** | 0 | **5** (see note) |
| editorial span, where a resolution exists | 64.4 s | 56.8 s (83 %) |
| cut-point quality | 0.1147 | 0.1139 |
| pacing | 0.8205 | 0.8203 |
| judge total | 0.7025 | 0.7022 |
| reaction linkage (aftermath found) | — | **17** |

> **Note on the two rows marked above, 2026-09-02.** The aftermath count is
> corrected here for the same reason as the table in Part 2: it was taken
> before the resolution priority fix.
>
> The "5 replay candidates" row carried **no stated definition** and was an
> ad-hoc count that no code reproduces — `scripts/baseline.py` has never
> measured replay. It is left in place as the historical record it is, and it
> is **superseded**: Replay was measured in full against a definition fixed in
> advance and **closed at 0 candidates**. See
> [`P2_1_REPLAY_ANALYSIS.md`](P2_1_REPLAY_ANALYSIS.md#closed--2026-09-02).

**House edit: zero videos changed** across all 17 projects. One project,
`proj-ca4d0eac9d6`, has a changed **axis record** — `pacing 0.886 → 0.827` —
with identical selection, identical timeline boundaries and the identical
winning profile. The cause is direct and measured: PAYOFF rising from 3 to 9
changed two adjacent shots to the same purpose, so a length change between them
now counts as arbitrary by the pacing axis's definition. Not reverted by hand.

## Part 4 — the coarseness was in the question, not the transcript

The ceiling named above — 21 of 48 located resolutions with a seam nearby that
speech forbids, transcript segments running to 391.8 seconds — turned out to be
partly an artefact.

`transcript_segments` has a `words` column. That 391.8-second segment carries
word-level timings, and its first word occupies **0.16 seconds**.
`TranscriptSegment.words` holds them, `_from_row` loads them, and nothing
between the database and the evidence layer was missing anything.

**One function asked the wrong object for its span.** `_cuts` built the
forbidden list from the *segment*:

```python
spoken = tuple((said.start, said.end) for said in evidence.said ...)
```

so a container of 391.8 seconds forbade cutting across all of it. Measured
across every segment on this machine: **7,119 seconds of words inside 14,382
seconds of segment.** Half of everything marked unsafe was silence, and 989
gaps between consecutive words exceed a second.

| | before | after |
|---|---:|---:|
| seams safe to cut on | 2,193 (55 %) | **3,065 (77 %)** |
| blocked by speech | 1,780 (45 %) | **908 (23 %)** |
| **blocked by a span over 30 s** | **877 (22 %)** | **0** |
| median forbidden span | 2.6 s | **0.38 s** |
| longest forbidden span | 391.8 s | 29.0 s |
| resolutions with a safe seam within 20 s | 17 / 48 | **28 / 48** |
| resolutions the speech guard blocks | 21 | **10** |

House edit unchanged — zero videos, zero axis records. The two styles that snap
to seams gain where they should: `transition_quality` 0.0756 → **0.0834**,
cut-point quality 0.1185 → **0.1223**, with the judge flat at −0.0002. The
improvement is in where cuts land, not in a metric being courted.

A segment with no word timings still forbids its whole span. Declaring an
unmeasured stretch safe to cut through is the one outcome worse than being too
cautious, and `SPEECH_MARGIN` still applies — now per word rather than per
paragraph, so a cut a fifth of a second after somebody stops talking is still
a cut on their breath.

Ten cases remain genuinely blocked. That is real continuous speech, and no
amount of reading the data differently will move it.

## Part 5 — the boundaries reach a cut, through the right policy

Part 3 found the editorial span had one consumer and that connecting it to
`CutPolicy` changed nothing. Part 4 removed the speech blockage. This connects
it, through `ContextPolicy` rather than `CutPolicy`, and the distinction is the
whole point.

`ContextPolicy.trim_at_resolution` ends a shot shortly after the thing it is
about was decided. It is here and not in `CutPolicy` because that was measured:
wiring it there produced **zero change**, since moving an out-point to just
after a resolution is a 54.7-second move against a `max_drift` of 1.5 seconds.
The guard was right to refuse. Snapping a cut to a nearby seam and ending a
shot on its point are different acts, and the second is a tail trim — fenced
by `MAX_TRIM_FRACTION`, inside which the same 54.7 seconds is 30 % of its shot.

Four refusals, each written into the code with its reason:

- **a shot somebody responded to keeps its tail** — the response is what the
  shot is for, and this is the same protection the ordinary trim honours;
- **the aftermath wins over the resolution** where the span located one,
  because a reaction the reading placed is a fact about this shot;
- **the cut lands on a seam the footage already has**, never mid-word;
- **the whole move is fenced at `MAX_TRIM_FRACTION`**, which is what makes it
  a taste rather than a re-edit.

Only `gaming_fast` and `competitive` ask for it.

| | before | after |
|---|---:|---:|
| **house edit** | — | **0 videos, 0 axis records** |
| **the three styles that did not opt in** | — | **no change on any metric** |
| median shot, the two that did | 35.62 s | 35.28 s |
| edit length, the two that did | 745.2 s | **736.2 s** |
| cut-point quality, the two that did | 0.114 | 0.116 |

**16 shots of 435 end earlier**, by a median of 5.4 seconds and at most 20.4 —
105 seconds in total of footage that ran past the thing its shot was selected
for. The second row is the evidence that the gating works: `cinematic`,
`funny` and `minimal` moved on nothing at all.

### A test that was wrong, and a fence that was not

The first version of `test_a_shot_ends_shortly_after_its_resolution` expected a
trim to 130.0 on a shot running 100.0 → 160.0, and got 139.0. 139.0 is exactly
the `MAX_TRIM_FRACTION` floor: trimming to 130 would have removed half the
shot. **The test was corrected and the fence was not**, and the reason sits in
the test so the next reader does not repeat the argument.

## What this says about Replay

Five candidates, up from zero. That is a real rise and it is not enough to
justify loosening `_exclusive`, which was the condition set before the work
started.

> **Settled, 2026-09-02: Replay is CLOSED.** The instinct in this section was
> right and the number under it was not measurable. Replay was later measured
> against a definition fixed before the result was seen — action and resolution
> both located at 0.80 confidence inside a core span of 12 seconds — and
> returned **0 candidates across 435 moments and 10.21 hours of source**. Even
> ignoring every quality condition, only 48 moments carry an action and a
> resolution together, which is 0.78 per ten minutes against a threshold of
> 1.0: **unreachable on this data before a single filter is applied.**
> `_exclusive` is untouched. See
> [`P2_1_REPLAY_ANALYSIS.md`](P2_1_REPLAY_ANALYSIS.md#closed--2026-09-02).

And the ceiling is now visible: **21 of 48 located resolutions have a seam near
them that speech forbids**, because the transcript segments run to 392 seconds.
That is the same coarseness blocking P1.8, reached from a different direction —
and it is a better target than Replay.
