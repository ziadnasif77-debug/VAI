# The Style Bible

`config/style.yaml` holds what this channel's taste is, in numbers, with a
version on each style and a declared range on each value.

## What was here before

A style has existed since V1's Phase 8, and it reached exactly one place:

- the presets in `config/interaction.yaml` set `intent.style`,
- `config/effects.yaml: style_profiles` says how strongly that style decorates,
- `backend/effects/planner.py` reads it when ranking candidate effects.

Nothing else in the editorial stack knew a style existed. Cut lengths, audio
decisions, what the counterfactual judge valued and what the post-render critic
called a defect were module constants in Python — identical for every video
this machine will ever make. The channel's identity was one dial on the effects
library.

P8 is the body behind that name. **It is the same namespace**: a style is still
called `best_moments` or `cinematic`, and every style the effects library knows
has an entry here. There is no second vocabulary for the same idea.

## What belongs in it, and what does not

| Belongs | Does not |
|---|---|
| How long this channel holds a shot | What a level *is* (`editorial.yaml`) |
| Which music bed sits under which level | What silence is (`SILENCE_DB = -40.0`) |
| How much speech the judge expects | How speech is measured |
| How long a stretch may run before it reads as tired | The 2 Hz grid the lanes are sampled on |
| How many effects in ten seconds is a pile | The names of the effects |

The test is simple: **if changing the number would break a reading rather than
change a video, it is a measurement and it stays in code.** Vocabulary — levels,
roles, lanes, defect codes, correction verbs — is the language the stages speak
and never appears here either.

## The two rules that make it a bible

**Every style carries a `version`.** A change of taste becomes an event you can
point at. Every edit records the style, its version, the digest of the resolved
values and the whole resolved body in the `edit_styles` table, written by the
EDL stage at the moment the timeline is stored. That record is what lets a later
question — *did the patient cut hold viewers longer?* — be answered from the
database rather than from memory, and it is why the row keeps the resolved body:
`config/style.yaml` can be edited tomorrow, and what a video was cut with cannot
be re-derived from a file that has since changed.

**Every tunable declares its legal range once, under `limits`.** A style that
sets a value outside its range refuses to load, naming the style and the key —
clamping silently would make the file a suggestion. The fence exists before the
thing it fences: P10's controlled tuning is allowed to move these numbers and
nothing else, and only inside these bounds.

## Reading it

```yaml
style:
  default: default             # the body for a project that named no style
  limits:
    pacing.band_scale: { min: 0.5, max: 2.0 }
  bible:
    cinematic:
      version: 1
      pacing:
        band_scale: 1.35       # holds every shot 35% longer than the bands say
```

An entry that overrides nothing is not an oversight. It says this style cuts the
way the machine has always cut, and the defaults it inherits are the exact
constants that lived in Python before the file existed. **Adopting the bible
changed no video**; only editing it does. `tests/unit/test_style.py` asserts
that directly — a shot resolved with the house style measures the same as a shot
resolved with no style at all.

## Who reads what

| Section | Consumer | Effect |
|---|---|---|
| `pacing.band_scale` | `backend/editorial/pacing_engine.py` | multiplies the level bands from `editorial.yaml` |
| `pacing.*_relief` | same | how much a stutter, a still frame or an ending lengthens a shot |
| `pacing.on_the_beat_seconds` | same | how close an event has to be for a cut to land on it |
| `audio.shelves` | `backend/audio_director/plan.py` | which music bed each level gets |
| `audio.min_section_seconds` | same | how long a bed must last to be worth a swap |
| `audio.silence_before_payoff` | same | the held breath before a payoff |
| `judgement.ideal_*` | `backend/narrative/judge.py` | what the counterfactual judge scores against |
| `critique.*` | `backend/critic2/watch.py` | what the post-render critic calls a defect |

The stages after the render — the renderer, QA, the post-render critic — read
the **stamp**, not the brief. A person who changes preset after a render has not
changed the file on disk, and judging that file by the new style would report
defects the edit was never trying to avoid.

## Adding a style

1. Add an entry under `bible:` whose name matches a profile in
   `effects.yaml: style_profiles` — the test in `tests/unit/test_style.py`
   requires the two sets to be identical, because a name that decorates and
   cannot cut is the half-a-style this phase exists to finish.
2. Override only what you have an opinion about. Say so in the description when
   you do not: `funny` and `competitive` are marked unauthored on purpose.
3. Start at `version: 1`. Raise it when you change a number, so the videos cut
   before and after are distinguishable in `edit_styles`.
4. If you need a tunable that has no `limits` entry, add the bound first.

## What this is not

No part of this file was learned. The numbers were authored, and most were
authored by being copied out of the Python that already held them. Nothing in
this system reads outcome data, and nothing adjusts these values on its own —
P10 will, inside these bounds, with a record of every change and a way back.
