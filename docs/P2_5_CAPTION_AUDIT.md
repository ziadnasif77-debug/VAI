# V2-P2.5 — caption intelligence, audited

Measured before building, on the same terms as the sound audit. The question
was whether captions are actually intelligent — timed from the transcript,
gated on confidence, laid out away from the HUD, rendered in the right
direction — or whether that is configuration nobody reads.

The short answer: **the caption path is complete and correct, including the
part most likely to be wrong (Arabic direction), and there are two gaps —
one dead field and one missing doctrine.**

## 1. The path, end to end

`transcript segments → build_captions → Caption → composition payload →
Caption.tsx`, traced and exercised at every hop.

| hop | evidence |
|---|---|
| timing from the transcript (§71) | `captions.py` maps word timings onto the timeline; nothing re-derives them |
| confidence gate | `captions.py:176`, `segment.confidence < config.min_confidence` |
| line wrapping | `wrap()` in Python, **not** the browser — sidecars and burn-in cannot disagree |
| direction | `_script_language()`, then `config.is_rtl(language)` → `Caption.tsx:78` |
| word highlighting | `composition.py:349` → `Caption.tsx:94`, guarded by `style.wordHighlighting` |
| HUD avoidance | `qa/content.py:339`, checked against the game profile's declared regions |

Nothing on that path drops a setting, and nothing re-derives what an earlier
stage already decided.

## 2. What the numbers say

On 1,741 transcript segments across nine projects:

| | |
|---|---:|
| captioned | **1,469 (84 %)** |
| withheld below `min_confidence: 0.30` | **272 (16 %)** |
| carrying word-level timings | **1,739 of 1,741** |
| stored captions | 312 |

The gate is real: 16 % of what was transcribed never becomes a caption,
because the recogniser was not sure enough to put words on screen. That is the
setting doing its job rather than sitting in a file.

## 3. The Arabic direction is solved, and by the right rule

This was the finding most likely to be a defect, and it is not one.

**1,619 of 1,741 segments have a NULL `language` column.** A system that
trusted that column would render almost every Arabic caption left-to-right.

`_script_language()` reads the script off the text itself and applies the
UAX#9 paragraph rule — the first strong-directional letter decides the
direction of the paragraph:

| | |
|---|---:|
| resolve to `ar` and render right-to-left | **1,714 (98 %)** |
| resolve to `en` | 4 |
| resolve to nothing | 23 |

The 23 unresolved are not failures. They are genuinely English strings —
`'born.'`, `'protest'` — sitting in an Arabic transcript, and left-to-right is
the correct direction for them. **No caption is rendered in the wrong
direction.**

## 4. Gap one — `Caption.style` is a dead field

`Caption.style` is declared at `captions.py:74`, serialised at 93, and
round-tripped through the repository at 135 and 335. It is persisted on every
caption ever produced.

**All 312 stored captions have `style = {}`. Every one, since the beginning.**

And it would not matter if they did not, because the field never reaches the
renderer: `_describe_caption` builds `id, index, from, durationInFrames, text,
lines, words, language, rtl, clipId` — and no `style`. Nothing anywhere reads
it.

> **Classification: DEAD FIELD.** Not a missing consumer, because there is no
> producer either. It is a storage round-trip to nowhere: written empty,
> stored empty, loaded empty, dropped before the render. It costs a column and
> it promises a capability that does not exist.

## 5. Gap two — captions have no style doctrine

**The word "caption" does not appear in `config/style.yaml`.** Not once.

| style | doctrine sections |
|---|---|
| cinematic | selection, shots, pacing, audio, judgement, critique |
| funny | selection, shots, pacing, audio, judgement, critique |
| competitive | selection, shots, pacing, audio, judgement, critique |
| gaming_fast | selection, shots, pacing |
| minimal | selection, shots, judgement |
| **every one of them** | **no captions** |

So `build_captions(timeline, segments_by_media, config)` takes no style, and
cannot: there is nothing to pass. Every style produces byte-identical
captions — same `animated`, same word highlighting, same emphasis, same 0.30
confidence floor, same safe area.

> **Classification: MISSING STYLE POLICY.**

This is the same shape as `minimal` having no audio doctrine, except universal:
captions are the one editorial layer where *no* style has an opinion.

Whether that is worth fixing is a taste question with a real argument on both
sides, and it is the user's call rather than mine:

- **For**: `competitive` doctrine already says clarity over decoration, and
  animated word-by-word highlighting is decoration competing with the play.
  `funny` already holds a 1.6-second comic pause in the audio layer; captions
  are where a punchline lands visually.
- **Against**: the current settings are defensible for every style, and unlike
  the sound gap there is no *measured* flattening here — no equivalent of 707
  spans all ducking by exactly −8 dB. The case is reasoning, not evidence.

## 6. What was built

Both gaps were closed. Neither needed a new abstraction, which was the
condition set before the work started.

### The dead field is gone

`Caption.style` is removed from the dataclass, from `as_row`, from both
reconstructions, from `_CAPTION_COLUMNS`, from the insert and from the load.
Migration `0016_drop_caption_style.sql` drops the column, and `SCHEMA_VERSION`
follows it to 16.

Nothing referenced it -- not a test, not the renderer, not a report -- which is
what made deleting it the honest default rather than wiring it. Per-style
caption appearance is a taste, and tastes live in the Style Bible with the
other tastes, not on a per-row column that every caption carries and no caption
fills.

### Captions have a doctrine

`StyleCaptionsConfig` holds four settings, and every field is `None` by
default. `None` means *this style has no opinion*, not *this style wants the
default*, and the difference is the whole design: `applied_to` returns the
caller's own configuration object, **by identity**, when nothing is set.

| style | captions |
|---|---|
| default, cinematic, minimal, gaming_fast | the house's own object, `is` not `==` |
| **funny** | 52 px -> **60 px**, gold -> **#FF4FA3** |
| **competitive** | fade and rise **off**, word highlighting **off** |

So four of six styles cannot drift from the house, because there is no copy of
the house's configuration for them to drift from.

`funny` declares only what it changes. It wants the house's animation and the
house's word highlighting, so it says nothing about either and inherits both --
a doctrine that restated the defaults would be decoration, and this file has
argued against that everywhere else.

`competitive` turns the caption layer's two moving parts off. This is the same
argument its `ideal_effects_per_minute: 1.5` and `effects_per_ten_seconds: 2.0`
already make, applied to the one overlay that was exempt from it: the house
fades every caption in and rises it a sixth of its height, directly over the
play, at exactly the moment something is happening -- because a caption arrives
when somebody speaks, and somebody speaks when the play is worth talking about.

### What is deliberately not in it

**`emphasis`**, and this is a finding rather than an omission. It is declared
in `CaptionsConfig`, passed through `composition.py:351`, and declared in the
renderer's own TypeScript schema with a default -- and **no component reads
it**. It crosses the entire backend-to-renderer boundary and is dropped on
arrival. A style declaring it would be declaring nothing, so the doctrine
covers only the four settings traceable to a line that draws something:
`animated` and `wordHighlighting` to `Caption.tsx:44,55,98`, `font_size_ratio`
to the resolved `fontSizePx`, and `highlight_color` to the lit word.

That makes `emphasis` the second dead thing in this layer, still recorded and
not fixed -- it needs somebody to decide what caption emphasis should *look*
like, which is a design question rather than a wiring one.

### Tests

`tests/unit/test_caption_doctrine.py`, 20 tests in four groups: neutrality by
identity, the doctrine being read, the doctrine reaching a value the renderer
draws with, and the dead field being gone.

They were checked by removing both `captions:` blocks from `config/style.yaml`
and re-running: **6 failed, 14 passed**, then green again when restored. A
doctrine test that still passes with the doctrine deleted proves nothing, and
that is the failure mode this phase was most exposed to.

### A harness trap, found and fenced

`python scripts/baseline.py --against tests/golden/house_edit.json` reported
**all seventeen projects as `[selection changed]`**, and nothing had changed.
`--freeze` writes the house edit flat -- one entry per project -- while
`--against` expects a run keyed by style, so every lookup missed and every
metric printed a change from nothing.

It was believed for about a minute. `_diff` now recognises the frozen shape and
says so, pointing at `test_house_edit_frozen.py`, which is the check that
actually answers the question.

### The numbers

| | before | after |
|---|---:|---:|
| **house edit** | -- | **0 videos, 0 axis records** |
| styles rendering the house's captions | 6 of 6 | **4 of 6, by identity** |
| caption settings with no consumer | 2 | **1** (`emphasis`, recorded) |
| dead database columns | 1 | **0** |
| `funny` caption size | 52 px | **60 px** |
| `competitive` caption motion | fade + rise + travelling colour | **none** |
| tests | 2,671 | **2,691** |

The house edit is unchanged and cannot have changed: captions are built after
the cut and feed nothing back into it. `test_house_edit_frozen.py` and
`test_style_differentiation.py` both pass, and four styles resolve to the
house's configuration object itself.

## Verdict

**CAPTION INTELLIGENCE: COMPLETE.** Both gaps in the measurement are closed
(§6), and one finding is recorded and deliberately not fixed.

The timing derives from the transcript, the confidence gate withholds 16 % of
segments, the wrapping cannot drift between sidecar and burn-in, HUD collision
is checked against real profiles, and 98 % of captions render right-to-left
through a script rule rather than through a database column that is empty
1,619 times out of 1,741.

Nothing here needed a new abstraction, which was the condition. The doctrine is
four settings on an object that already existed, resolved through the same
`style_bible.for_project` call the audio plan already made, reaching the same
`_style` payload the renderer already read.

**Closed:**

1. **`Caption.style` is gone** (§4) — deleted from the model, the repository
   and the schema. Nothing read it, so deleting it was the honest move rather
   than inventing a use for it.
2. **`funny` and `competitive` have caption doctrine** (§5) — and the other
   four styles receive the house's own configuration object by identity, so
   they cannot drift from it.

**Recorded, not fixed:**

3. **`captions.emphasis` has no consumer.** It is passed to the renderer and
   declared in the renderer's schema, and no component reads it. Wiring it
   needs somebody to decide what caption emphasis should look like, which is a
   design question and not this pass's. It is kept out of the doctrine for
   exactly that reason: a style declaring it would be declaring nothing.
