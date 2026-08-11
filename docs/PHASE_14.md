# Phase 14 — One real game

SPEC §22–§25, §111; §126 step 20. **Acceptance: with a profile, a real
recording yields events the generic path cannot produce at all — and without
one, nothing changes.**

Status: **complete and verified** on 62.6-minute Grand Theft Auto V captures
from `D:\Gaming 2026`.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| HUD state extraction (§24) | `backend/gaming/hud.py` | `test_hud.py` — 26 tests |
| Profile model for HUD (§22) | `backend/gaming/profiles.py` | `HudIndicator`, `HudChangeRule` |
| One real game profile (§111) | `profiles/gta_v/profile.json` | `TestTheShippedGtaProfile` |
| HUD changes become events (§21, §26) | `backend/gaming/events.py` | `observations_from_hud` |
| Wired into the pipeline | `backend/pipeline/workers/gaming_workers.py` | `test_gaming_pipeline.py` |
| Game Profile API (§111) | `backend/api/routers/profiles.py` | `test_profiles_api.py` — 16 tests |
| Choosing the game (§59) | `apps/web/src/screens/ImportScreen.tsx` | The browser |
| Real-footage check | `scripts/verify_phase14.py` | Two recordings, one held out |

---

## Why GTA V

The 14 recordings in `D:\Gaming 2026` turned out to be two games — mostly GTA V,
some Grounded. GTA V won on three counts: it is in §22's named list, it is most
of the corpus, and its most valuable signal is one **no amount of OCR will
read**. The wanted level is five star glyphs in a corner. There is no text.

That is the whole argument for profiles in one example. Vision can say "a car
chase"; OCR can read "MISSION PASSED"; neither can tell you the police threat
just went from two to four, which in this game is the difference between
driving and a highlight.

---

## What a HUD reader is for

**An indicator is only interesting when it changes.** A four-star wanted level
held for six minutes is not four minutes of event; the second it went from two
to four is. So the reader produces a *series* of readings and `track()` turns
the transitions into §26 events. The steady state is context, not content.

**It never decides alone.** Every reading carries a confidence, and an event
can never be worth more than the reading behind it. §27 merges detectors, and a
wanted-level rise that coincides with gunfire and a shouted reaction is worth
far more than any of them alone — which only works if the HUD reader is honest
about what it does not know.

**It reads no extra frames.** The HUD is read inside the OCR stage, which
already opens the candidate keyframes the §16 cascade chose. Decoding them
twice is exactly the waste §15 and §16 exist to prevent. It runs *before* the
OCR engine is checked, so a machine with no OCR still gets HUD events (§95).

---

## The wanted level took four attempts

Worth recording in full, because every attempt was defensible and three were
wrong.

| Attempt | Idea | Why it failed |
| --- | --- | --- |
| 1 | Brightness: lit stars are bright | An earned star is a mid-grey opaque fill. It reads *light* against night and *dark* against sky. Brightness alone is meaningless |
| 2 | Uniformity: a solid centre vs a hole | An empty star over plain sky has a centre every bit as uniform as a solid one. Read four stars as five |
| 3 | Difference from the background | The empty star is not a hole — it is an opaque *white* fill. It differs from the sky as much as an earned one does |
| 4 | **Whiteness** | Earned is mid-grey, empty is near-white, in every scene. This one holds |

And a fifth thing, which is not a discriminator at all:

> **The row flashes.** While the police are searching, GTA V drives every glyph
> bright, then every glyph empty, about twice a second. A frame caught
> mid-flash shows five lit glyphs and is indistinguishable from a genuine
> five-star level.

That is why three discriminators disagreed with each other on the same footage:
they were being asked to read a value that, in those frames, does not exist. A
count is only defined between flashes. The reader now detects the flash and
declines, which turned five wrong answers into five honest refusals.

Two more refusals were needed for the same reason:

- **The ammo counter moves into the same box** when the wanted level is zero.
  Before the shape test, `627 8` read as three stars *at full confidence* —
  the worst kind of wrong. Cells are now compared as shapes: one glyph repeated
  agrees with itself (0.83–0.88 on real frames), a number does not (0.43–0.67).
- **An unreadable frame is not a zero.** Reporting one would say the police
  left when the truth is that a lamp post was in the way. Only a genuinely
  empty region produces the profile's `absent_value`.

### Measured

Twelve hand-labelled frames from the real recording, after all of the above:

```
12/12 correct; 0 confidently wrong
```

It reads 4 of the 12 and declines 8, each with a named reason. That recall is
low and the precision is the point: a wrong high-confidence event poisons §27's
correlation, and a missed one costs a single moment.

---

## Acceptance, on the real recording

`scripts/verify_phase14.py`, 40 frames sampled across 62.6 minutes:

```
readings:  40
  usable:  7   (values seen: [0, 1, 5])
  declined: 33
             18  row flashing
             13  cells are not the same glyph
              2  low confidence

state changes: 4
   38:18  5.0 -> 0.0   (conf 0.36)
   42:58  0.0 -> 1.0   (conf 0.55)
   44:31  1.0 -> 0.0   (conf 0.55)
   56:57  0.0 -> 5.0   (conf 0.55)

events from the profile:         3
  38:18  escape             conf=0.36
  42:58  chase              conf=0.55
  56:57  unexpected_event   conf=0.55
events from the generic profile: 0
```

That is the §111 claim, measured: three events the generic path cannot produce.
The generic count is zero by construction — it declares no HUD — and that is
exactly §23 holding.

The thresholds were tuned against frames from this recording, so this run is
not an independent test of them.

### Held out

A different 96-minute capture the reader had never seen, same 40 samples:

```
readings:  40
  usable:  8   (values seen: [0, 2, 3, 4])
  declined: 32
             27  cells are not the same glyph
              3  low confidence
              2  row flashing

state changes: 5
   25:06  0.0 -> 3.0
   39:35  3.0 -> 4.0
   51:38  4.0 -> 3.0
   58:52  3.0 -> 2.0
   95:03  2.0 -> 0.0

events from the profile:         3
events from the generic profile: 0
```

This is the run that matters, and it is the better of the two. The values form
a **coherent arc** — the wanted level climbs to 4, decays to 0 over the next
half hour — on footage nothing was fitted to, and it produced intermediate
levels (2, 3, 4) the tuning recording never showed. A reader overfitted to one
recording's lighting would not do that.

Sampling every 93–145 seconds means intermediate steps are missed (`0 → 3` was
certainly `0 → 1 → 2 → 3`). At the pipeline's real frame density this improves;
the number to trust is Phase 15's, against a labelled dataset.

---

## The Profile API

Three endpoints, and one of them earns its place on its own.

| | |
| --- | --- |
| `GET /api/profiles` | Every profile, with what each actually declares |
| `GET /api/profiles/{game}` | One profile; an unknown game returns generic with `exact: false`, not 404 (§23) |
| `POST /api/profiles/validate` | Check a candidate document without installing it |

Validation is the one that pays for itself. A profile is JSON full of enum
values, fractions and regular expressions, and every one can be wrong in a way
that produces *silence* rather than an error. Writing the GTA V profile
produced two event types that read like real ones — `objective_complete`,
`objective_failed` — and are not. Finding that at validation costs a second;
finding it after a two-hour analysis costs the analysis.

Nothing writes a profile. Profiles ship with the code, and an endpoint that
installed one would be an upload path into a directory the pipeline reads
event rules from.

### And a picker, because otherwise none of it is reachable

The import screen had no way to name a game, so every project created through
the interface analysed on the generic path and the profile built by this phase
could not be selected at all. It now lists what the API returns, defaults to
**no profile**, and says in each case what the choice buys — "Adds 4 text rules
and 1 HUD indicator", or "Vision, OCR, audio and speech only. Nothing about a
specific game is assumed."

The picker also states the one thing that is *not* cheap to change later. Target
length and mode re-run the story chain in seconds (§127); the game decides what
the analysis looks for, so changing it after analysing means analysing again.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| Two event types in the profile do not exist | The profile refuses to load — caught immediately, which is the architecture working |
| The ammo counter read as three wanted stars, confidently | A fabricated police chase in most recordings, weighted as real evidence by §27 |
| A frame that could not be read reported a confident zero | "The police left" whenever a lamp post crossed the corner |
| The bar reader sampled its background from inside the bar | A half-full health bar read as full |
| `app_state` did not redirect `profiles_dir` | A test wrote a deliberately broken profile into the developer's checkout, and the next run found it |
| The HUD reader treated the frame list as objects, not tuples | Every analysis with a HUD profile fails at the OCR stage — found by the one test that runs the real stages end to end, and by nothing else |

---

## Not built, and why

| Deferred | Why |
| --- | --- |
| A second game profile | §111 says so explicitly: do not write ten before the architecture is validated. Grounded is the obvious next one — it is in the same footage folder |
| Health and armour indicators for GTA V | The `bar` reader exists and is tested, but GTA V's health arc is a *curved* bar around the minimap, which is a different shape and would be a fifth attempt at pixel geometry with no labelled data to check it against |
| Kill feed OCR | GTA V has no kill feed. The regions and rules that matter here are the centre banner and the notification box, and both are declared |
| Windowed aggregation of readings | The honest fix for the flash is to take a mode over ~1 s of frames, which needs frame density the §16 cascade deliberately does not provide. Phase 15, with the golden dataset, is where that trade gets decided |
| Auto-detecting the game | §22 lets a project name its game and §23 makes the name optional. Detecting it is a classifier, and a classifier needs the labelled dataset Phase 15 builds |

---

## Gate to Phase 15

Met: one real game, end to end, on real footage, producing events the generic
path cannot — and the API that makes the second game a data change.

Phase 15 is quality (§112, §117–§119): a golden dataset, precision and recall
for events and moments, user-edit metrics, and packaging. The wanted-level
reader is the first thing that dataset should be pointed at — its recall is
measured here at 4 frames in 12 and 7 in 40, and neither number deserves to
stand without labels behind it.
