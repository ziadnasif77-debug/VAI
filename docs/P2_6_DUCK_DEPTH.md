# V2-P2.6 — the duck depth, proportional at last

The one gap the sound audit recorded and did not fix. It said the change
"would change how **every rendered video sounds** while changing no edit", and
that it needed "its own before/after, on rendered output rather than on the
plan". This is that.

## The defect, re-measured at full scale

The sound audit found 707 loud spans across four recordings, every one ducking
the music by exactly −8.0 dB. Measured across every recording on this machine:

| | |
|---|---:|
| loud spans | **1,900** across 19 recordings |
| distinct duck depths among them | **1** |
| peak on the audio lane: min / median / max | 0.750 / **0.893** / 1.000 |
| barely over the threshold (< 0.80) | **304** |
| at the very top (≥ 0.99) | **110** |

`_event_spans` thresholds the lane at 0.75 and returns spans. The value that
crossed the threshold — the thing that separates a footstep from an explosion
— was measured, used for the comparison, and dropped on the next line.

> **Classification: MISSING CONSUMER.** Not a bug: nothing was broken and the
> §72 priority held. The number simply existed at the moment the depth was
> chosen, and was thrown away.

## The fix

Three changes, and one of them is a deletion of duplicated logic.

**The peak travels with the span.** `_event_spans` now returns `LoudEvent`, a
named triple of `(start, end, peak)`. It is a *measurement*, deliberately not a
decision: how far the bed steps aside is decided where every other depth is,
against `audio.ducking`.

**The depth is interpolated.** `game_event_duck_db` keeps its meaning and
becomes the deep end of a range rather than a single value — it is what an
event at full scale gets, and **nothing ducks further than it**. The shallow
end is a new `game_event_duck_floor_db: -3.0`, for a span that only just
crosses the threshold. Not zero: a span that qualified as competing with the
music and then changed nothing would be a span for no reason.

```
depth = floor + clamp((peak − 0.75) / 0.25) × (configured − floor)
```

Clamped at both ends rather than extrapolated. A lane value above 1.0 is a lane
worth looking at, not a louder-than-possible explosion, and inventing a deeper
duck on the strength of it would be the wrong response.

**One code path instead of two.** `audio_mix.event_spans()` already existed for
exactly this and had no caller but a test — the same family as the original
defect — while `render_worker` built its `DuckSpan`s inline at the flat depth.
The worker now calls the function. The depth is decided in one place, and the
duplicate is gone rather than fixed twice.

## What it does to the plan

The same 1,900 spans, before and after:

| | before | after |
|---|---:|---:|
| distinct depths | **1** | **845** |
| median depth | −8.00 dB | **−5.87 dB** |
| mean depth | −8.00 dB | **−5.74 dB** |
| spans whose depth changed | — | **1,881 of 1,900** |

And by how loud the event actually was:

| the event | n | depth now | was |
|---|---:|---|---|
| barely over the line (< 0.80) | 304 | **−3.00 … −4.00 dB** | −8.00 dB |
| ordinary (0.80–0.99) | 1,486 | −4.00 … −7.80 dB | −8.00 dB |
| **full scale (≥ 0.99)** | 110 | **−7.80 … −8.00 dB** | −8.00 dB |

The bottom row is the one that matters most: a real explosion still ducks the
bed by the full configured 8 dB. Nothing was taken away from the moments the
setting was written for. What changed is that a footstep stopped borrowing
their depth.

## What it does to the rendered output

Not the plan — the gain curve `write_envelope` writes and the filter graph
multiplies in, read back off the WAV. One real recording, 4,035 seconds, 324
loud spans:

| | before | after |
|---|---:|---:|
| samples ducked | 93,853,800 (24.2 %) | 93,138,648 (24.0 %) |
| **deepest point** | **−8.00 dB** | **−8.00 dB** |
| mean level while ducked | −7.01 dB | **−5.34 dB** |

**95,206,490 samples — 24.6 % of the video — carry a different music level,
and the music sits 2.40 dB louder at the median changed sample.** The deepest
point is identical, which is the fix behaving as designed rather than a
softening of the whole mix.

## What did not change

**No edit.** Selection, boundaries, shot lengths, the judge's axes and the
winning profile are untouched — this reads the audio lane and writes a gain
envelope, and nothing on that path feeds back into what gets cut. The frozen
house edit covers exactly those things, and it passes.

**The §72 hierarchy.** Speech still takes the music down 14 dB and the gameplay
4; a full-scale game event still takes the music down 8. The ordering the sound
audit verified on the rendered envelope is unchanged at every extreme, and only
the interior of the event range moved.

## Tests

`tests/unit/test_rendering.py` gains eight, `tests/unit/test_audio_director.py`
two, and one existing planner test gains an assertion on the peak. The ones worth naming:

- a **full-scale event ducks by exactly the configured depth** — the setting
  keeps its meaning;
- an event **at the threshold ducks by exactly the floor**;
- **a footstep and an explosion are no longer identical**, which is the whole
  point stated as an assertion;
- **nothing ducks deeper than configured**, at a peak of 1.4;
- **a span with no peak keeps the configured depth**, which is what every
  caller got before;
- the **peak is the loudest instant, not the first** — a span that opens at
  0.78 and builds to 0.99 is an explosion, and reading its opening value would
  price it as a footstep;
- **merging a quiet event into a loud one keeps the loud depth**, because the
  bed cannot climb back in the fifth of a second between them;
- and one that ends at the **envelope** rather than the `DuckSpan`, because
  that is the number that reaches the video.

Checked by reverting `_duck_depth` to return the flat depth: **4 failed**, then
green again when restored.
