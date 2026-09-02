# V2-P2.4 — the sound hierarchy, audited

Measured rather than read. The question was whether **Speech > Important Game
Audio > Music** is real in the rendered output or exists as constants and
configuration, and whether a style can change how a video sounds.

The short answer: the hierarchy is real, the styles do differ, and there is
exactly one gap.

## 1. The path, end to end

`style → audio doctrine → audio plan → audio_mix → rendered envelope`, traced
and exercised at every hop.

`render_worker._audio_plan` resolves the project's style through
`style_bible.for_project` and passes it to `plan_audio`, which reads
`style.audio` for section length and both silence settings. The plan reaches
`audio_mix`, which builds a per-sample gain envelope and multiplies it into the
filter graph. Nothing on that path drops the style and nothing re-derives it.

## 2. The priority, read off the envelope

Not off the constants — off the gain curve `build_envelope` produces, which is
the array the renderer multiplies into the graph:

| instant | music | game audio |
|---|---:|---:|
| quiet | 0.0 dB | 0.0 dB |
| the player is speaking | **−14.0 dB** | **−4.0 dB** |
| a loud game event | **−8.0 dB** | 0.0 dB |

During speech the music steps aside by 14 dB and the gameplay only leans back
by 4: **speech on top, game beneath it, music beneath that.** During a game
event the music drops 8 dB and the gameplay is untouched: **game over music.**

The ordering is exactly `speech_duck_db (−14) < game_event_duck_db (−8) <
game_under_speech_db (−4)`, and it holds in the rendered curve.

## 3. Nothing downstream overrides it

The static track gains are applied *before* the envelope and compose with it
rather than replacing it — `volume={gains.game}dB` then `amultiply` with the
envelope. So the effective separation is larger than the duck depths alone:

| track | base gain |
|---|---:|
| game | 0.0 dB |
| microphone | +2.0 dB |
| effects | −3.0 dB |
| **music** | **−18.0 dB** |

Music already sits 18 dB under the game before any ducking, so under speech it
is 32 dB down and under a game event 26 dB down. The limiter (`ceiling_db:
−0.3`) is a peak guard against clipping, not a level decision, and it cannot
mask the hierarchy because it acts only at the ceiling.

**Clipping**: guarded by construction. **Masking**: music at −18 dB cannot
mask gameplay at 0 dB.

## 4. The styles genuinely differ

Measured on one 4,035-second recording with 666 speech spans, the same inputs
under five styles:

| style | sections | median section | silences | total silent | lead |
|---|---:|---:|---:|---:|---:|
| best_moments | 13 | 100.5 s | 5 | 4.0 s | 0.80 s |
| **funny** | 13 | 100.5 s | **4** | **6.4 s** | **1.60 s** |
| **competitive** | 13 | 100.5 s | 5 | **1.5 s** | **0.30 s** |
| **cinematic** | **5** | **762.9 s** | 4 | 5.6 s | 1.40 s |
| minimal | 13 | 100.5 s | 5 | 4.0 s | 0.80 s |

And the silence positions show it as an editing decision rather than a number:

```
best_moments  164.9-165.8  751.7-752.5  1301.0-1301.8  2502.9-2503.8  2505.2-2506.0
funny         164.2-165.8  750.9-752.5  1300.2-1301.8  2504.4-2506.0
competitive   165.4-165.8  752.2-752.5  1301.5-1301.8  2503.4-2503.8  2505.7-2506.0
```

`funny` starts each held breath 0.8 s earlier and merges the last two into one
longer hold — the comic pause, in the layer that can actually hold it.
`competitive` holds 0.3 s, barely a breath, because the scoreline explains
itself. `cinematic` changes its bed five times instead of thirteen.

**Not byte-identical.** `minimal` is, and that is a missing policy rather than
a broken path: it declares no `audio` block, so it inherits the house's.

## 5. Where the ducking comes from, and why it is right

`_event_spans` reads the **audio lane**, not the event list — a loud stretch,
not a named event. That looked like a gap until the reasoning was checked, and
the docstring has it: *"an explosion's sound is what competes with the bed, and
the audio lane is the measurement of exactly that."*

So the answer to whether V2-P2.2's located resolutions should now drive the
ducking is **no**. The editorial resolution is where a *moment* concludes; the
music competes with what is *audible*, and those are different instants. Using
the boundary here would duck where the edit resolves rather than where the
sound is.

## 6. The one gap

**707 loud spans across four recordings duck the music by exactly −8.0 dB —
every one of them.**

Their peak on the audio lane runs from 0.75 to 1.00:

| | |
|---|---|
| at the very top (≥ 0.99) | 38 of 707 |
| barely over the threshold (< 0.80) | 120 of 707 |
| median | 0.89 |

`_event_spans` thresholds the lane at 0.75 and returns spans. **The magnitude
that decided each span is measured, used for the comparison, and then
discarded**, so a footstep just over the line and a full explosion duck the bed
identically.

> **Classification: MISSING CONSUMER.**
> Not a bug — nothing is broken and the priority holds. Not a measurement gap —
> the number exists and is read. Not purely a missing style policy, though a
> style would reasonably want to scale it. The value is available at the moment
> the depth is chosen and is thrown away.

A proportional depth would be a small change to one function, and it would
change how **every rendered video sounds** while changing no edit — the frozen
contract covers selection, boundaries and judge axes, none of which touch
audio. So it needs its own before/after, on rendered output rather than on the
plan, and that is not this pass.

> **Fixed in V2-P2.6.** The peak now travels with the span and the depth is
> interpolated between a new `game_event_duck_floor_db: -3.0` and the
> configured `-8.0`, which becomes the depth at full scale rather than the
> depth for everything. Re-measured across every recording rather than four:
> **1,900 spans, 1 distinct depth before and 845 after.** On rendered output,
> 24.6 % of one 4,035-second video carries a different music level and the bed
> sits 2.40 dB louder at the median changed sample — with the deepest point
> unchanged at −8.00 dB, because a real explosion still gets what the setting
> was written for. See [`P2_6_DUCK_DEPTH.md`](P2_6_DUCK_DEPTH.md).

## Verdict

**SOUND HIERARCHY: COMPLETE**, with one non-blocking gap recorded above.

The priority is real in the rendered envelope, the configured depths are
consumed and not overridden, clipping is guarded, and four of five styles
produce audibly different soundtracks from the same footage. Nothing here needs
a new abstraction: the wiring exists and works.

One item was recorded rather than fixed here, and has since been fixed; one
remains:

1. ~~**Duck depth is flat regardless of how loud the event is**~~ (§6).
   **Done in V2-P2.6**, with the before/after on rendered output that this
   section asked for.
2. **`minimal` declares no audio doctrine**, so it sounds exactly like the
   house style. That is a one-line addition to `config/style.yaml` whenever
   somebody decides what a minimal edit should sound like.
