# Writing a game profile

Everything in this guide is traceable to the two profiles this repository has
shipped — `profiles/grounded/profile.json` and `profiles/gta_v/profile.json` —
and to what happened when they were written against real recordings. Where a
rule is stated, the incident that produced it is cited. Nothing here is
speculative, and a profile you contribute should be able to say the same.

## What a profile is, and §23's bargain

A profile is one JSON file, `profiles/<game_id>/profile.json`, that tells the
pipeline what a specific game writes on its screen: text that identifies the
game, wording that means death or victory, interface furniture that means
nothing, where the HUD lives, and which combinations of evidence deserve a
name no single detector could give them.

§23 sets the bargain that shapes every field: **the application must not
require a game profile.** A profile is additive. Every field is optional, an
unknown game falls back to the generic profile (a constant in
`backend/gaming/profiles.py` that declares nothing — there is no
`profiles/generic/` file to edit),
and the generic path — vision, OCR, audio, speech, temporal analysis over the
whole frame — carries the analysis on its own.

What a profile buys is measured, not promised. On the three real recordings
this project has analysed (docs/PLAN.md, 2026-08-19):

| recording | profile in force | what the profile did |
| --- | --- | --- |
| سبي (Grounded) | `grounded` | its `creature_fight` fusion rule alone named 52 events |
| Ziad 2 (Grounded) | `grounded` | detected from OCR with no configuration; `creature_fight` named 17 of the 34 named events |
| تجريب 4 (GTA V) | `generic` (fallback) | 62 of 74 unnamed-but-observed events showed `driving`, which no generic rule may name |

The GTA row is the cautionary one: `profiles/gta_v/profile.json` existed —
seven measured HUD regions, region-scoped death rules — and was never used,
because it shipped with zero `signature_patterns` and detection matches on
signatures. **A profile nobody selects is a profile that does not exist**
(`backend/gaming/detection.py`). That failure is why this guide and
`scripts/profile_report.py` exist.

Two rules from §23 stay in force no matter what a profile declares:

- **Silence beats a guess.** Detection returns nothing rather than the wrong
  profile; the wrong profile would read another game's kill feed out of this
  game's inventory screen.
- **Recognition is evidence, not identity.** A detected game is stored in
  `projects.detected_game` beside the user's own `game` field, and a user who
  names their game is never overruled by a pattern match.

## Anatomy, field by field

Field semantics live in `backend/gaming/profiles.py` (pydantic models,
`extra="forbid"` — a misspelled key is a validation error, not a silent
no-op). What follows is each field as the shipped profiles actually use it,
and why it looks the way it does.

### `id`, `name`, `description`

`id` defaults to the folder name. Use `description` as a provenance note —
both shipped profiles do. From `gta_v`:

> "Written against real 1920x1080 capture. Regions were measured from frames,
> not guessed ... The OCR-tolerant spellings are not decoration -- DIRECTOR
> MODE came back as DIFIECTOR, DINECTOH, DIPECTOR and DRECTOR across 161
> readings of the same two words."

A description that records how the profile was measured is what lets the next
person tell a measured pattern from a guessed one.

### `signature_patterns` — how the game gets recognised

On-screen text that identifies this game: item names, place names, system
labels — anything another game would not write. From `grounded` (five of its
twelve):

```json
"signature_patterns": [
  "\\bMilk\\s+Molar\\b",
  "\\bO\\.?R\\.?C\\.?\\s+guards?\\b",
  "\\bLean-?To\\b",
  "\\bDandelion\\s+Tuft\\b",
  "\\bMUTATIONS?\\b"
]
```

And from `gta_v`, the pattern the misreads forced:

```json
"\\bDI\\w{0,2}ECTO\\w?\\s+MODE\\b"
```

That is `DIRECTOR MODE` as OCR actually returns it: `DIFIECTOR`, `DINECTOH`,
`DIPECTOR` and `DRECTOR` across 161 readings of the same two words on one
recording. A pattern matching only the correct spelling matches a minority of
its own evidence.

How they are scored (`backend/gaming/detection.py`): each pattern counts **at
most once per recording** — a quest tracker holding one recognisable word on
screen for four minutes is one piece of evidence about which game this is,
not two hundred (`GameProfile.signature_hits`). A profile is claimed only
with at least `MIN_HITS = 3` patterns matched *and* a lead of
`MIN_MARGIN = 2` over the runner-up profile. One shared word ("Craft",
"Analyze") is vocabulary half the genre uses; three game-unique strings with
a clear margin is a recording only one game could have produced. Measured on
the real footage: the GTA recording scores 6 hits with 0 for the runner-up,
the Grounded recordings 12 with 0.

### `event_rules` — text that names an instant

Case-insensitive regexes over OCR text, each rule declaring what event its
match means, how much that alone is worth, and how long it covers. The two
shipped profiles show the two honest shapes:

Region-scoped, when the layout is measured — from `gta_v`:

```json
{
  "event_type": "death",
  "patterns": ["\\bWASTED\\b"],
  "regions": ["centre_banner"],
  "confidence": 0.95,
  "duration_seconds": 4.0
}
```

"WASTED" anywhere on screen is a word; "WASTED" in the centre banner is the
death screen. The region restriction is what earns the 0.95.

Whole-reading-anchored, when it is not — from `grounded`, which declares no
regions:

```json
{
  "event_type": "death",
  "patterns": ["^\\W*DEATH\\W*$", "\\bdied\\s+by\\s+misadventure\\b"],
  "confidence": 0.85,
  "duration_seconds": 4.0
}
```

`^\W*DEATH\W*$` means *the reading is the word DEATH and nothing else* —
which is what a death banner is, and what a sentence containing "death" is
not. When you cannot say *where*, say *exactly what*.

A rule is evidence, not a verdict. Its confidence is multiplied by the OCR
reading's own confidence (`backend/gaming/events.py`), and §27's correlation
still decides by agreement across sources.

### `regions` and `ocr_regions` — where to look

Rectangles as **fractions of the frame**, never pixels: a profile written
against 1080p capture has to keep working on the 720p proxy the analysis
actually reads, and on an ultrawide. From `gta_v`:

```json
"regions": {
  "centre_banner": { "x": 0.2, "y": 0.36, "width": 0.6, "height": 0.2 },
  "minimap":       { "x": 0.01, "y": 0.82, "width": 0.16, "height": 0.17 }
},
"ocr_regions": ["centre_banner", "notification"]
```

`ocr_regions` names the subset OCR should read. Region-restricted OCR against
declared boxes is both cheaper and far more reliable than scanning a whole
frame of stylised game UI (§25). Measure regions from real frames — the GTA
profile's description records that its rectangles were measured, not guessed,
because the wanted-star row and the ammo counter share a corner and only a
frame shows where the line between them is. Every region an event rule or
`ocr_regions` names must exist in `regions`; the validator refuses the file
otherwise.

### `ignore_patterns` — interface furniture

Text that is *never* an event: menu labels, editor chrome, the quest tracker.
Filtering these out is most of what makes OCR-driven detection usable. From
`gta_v`, four of its sixteen:

```json
"ignore_patterns": [
  "DIRECTOR\\s+MODE",
  "\\bDI\\w{0,2}ECTO\\w?\\s+MODE\\b",
  "^\\W*(?:SCENE|CATEGORY|TYPE|ACTORS|ANIMALS|COSTUMES|GANGS|MILITARY|STATS|BRIEF)\\W*$",
  "^\\W*\\d{1,3}\\s*/\\s*\\d{1,3}\\W*$"
]
```

Director Mode's chrome is on screen for minutes at a time — `DIRECTOR MODE`
alone was read 161 times — and without these patterns every one of those
readings is offered to the event rules and the generic fallback patterns.
From `grounded`, the sharper case:

```json
"\\bDefeat\\s+the\\b"
```

That one line exists because *"Defeat the O.R.C. guards at the Milk Molar
stash"* sat in the quest tracker for minutes (see Pitfalls below).

Note that `DI\w{0,2}ECTO\w?\s+MODE` appears in `gta_v` **both** as an ignore
pattern and as a signature. That is not a contradiction: ignore patterns veto
event rules (`GameProfile.rules_for` checks `should_ignore` first), while
signature matching is a separate question — the same chrome that must never
become an event is excellent evidence of *which game* is on screen.

### `hud` — indicators read as numbers, and `change_rules`

The HUD reader (§24) is the one detector that reads a *value* rather than
text. GTA V's wanted level is five star glyphs and no text — nothing OCR can
ever return. From `gta_v`:

```json
{
  "name": "wanted_level",
  "kind": "glyph_row",
  "region": { "x": 0.908, "y": 0.008, "width": 0.085, "height": 0.04 },
  "count": 5,
  "confidence": 0.6,
  "absent_value": 0.0,
  "absent_confidence": 0.55,
  "change_rules": [
    { "event_type": "chase",  "direction": "rise", "at_least": 1, "min_change": 1,
      "confidence": 0.55, "duration_seconds": 6.0 },
    { "event_type": "escape", "direction": "fall", "at_most": 0,  "min_change": 2,
      "confidence": 0.6,  "duration_seconds": 5.0 }
  ]
}
```

Every number here is a decision with a reason:

- `region` is a **search window**, not the exact rectangle. The star row is
  right-anchored and grows leftwards, so a fixed rectangle misreads it by a
  whole star as the level changes — silently, and in the direction that
  matters most. The reader locates the row inside the window.
- `absent_value: 0.0` — GTA hides the star row entirely at wanted level zero,
  so absence *is* the reading. In a game that hides its health bar during
  cutscenes, absence means nothing: leave `absent_value` out.
- `change_rules` fire on *transitions*, not states: a wanted level held at
  four for six minutes is context; the second it went from two to four is the
  event. "Rose to at least 1" (`chase`) and "fell to 0 by at least 2"
  (`escape`) are different events in every game that has a threat level, and
  neither is a string.
- `confidence: 0.6`, deliberately below 1.0: a pixel heuristic that claims
  certainty is lying, and §27 exists to combine it with detectors that saw
  the same instant.

### `fusion_rules` — naming what no single detector could

Measured before these existed: 61% and 70% of correlated events on two real
recordings were `unexpected_event`, and 63 of one recording's 116 events were
`["audio", "scene"]` clusters — a waveform transient beside a shot change,
neither allowed to claim anything, and correctly so. But a transient *while
the vision model reports `combat`* is something a person names without
hesitating. A fusion rule reads a cluster's whole evidence bundle — labels,
sources, weaker named types — and names it (`backend/gaming/fusion.py`). From
`grounded`:

```json
{
  "event_type": "combat",
  "name": "creature_fight",
  "labels": ["combat"],
  "sources": ["audio"],
  "confidence": 0.7
}
```

Read: an instant where the vision model labelled the frame `combat` *and*
something was heard becomes a `combat` event at 0.7. This single rule named
52 events on one real recording. `gta_v` adds the driving equivalent —
`labels: ["driving", "vehicle"]` plus `sources: ["audio", "scene"]` is the
picture of a vehicle hitting something — precisely because 62 of its
recording's unnamed events showed `driving`.

The semantics that keep fusion honest (`FusionRule`): every requirement is a
conjunction; any one label in `labels` satisfies that requirement; a rule
with no requirements at all is refused (it would name every unnamed instant
in the recording); a label reported below `min_label_confidence` (default
0.45) is the model saying it does not know; and **a rule never overrides a
detector that could see** — fusion runs only when correlation resolved a
generic type, so a profile's kill-feed reading always beats an inference.

## The measured method

§111's order of work: one real game, validated, before more are written. A
profile is written *from footage, not from documentation* — every pattern in
the shipped profiles appeared in the OCR of recordings this project analysed.

**1. Record real footage of real play.** 40+ minutes; the shipped profiles
were measured from 40–96 minute recordings. Play the way the footage you want
edited is played. The GTA profile's death and chase rules are still marked
untestable in docs/PLAN.md because the only recording available was Director
Mode set-building with invincibility on — no deaths, no wanted level. Your
footage must contain the events you write rules for, or you cannot validate
them.

**2. Run the analysis.** Import the recording and analyse it as normal (the
web app, or `scripts/dashboard.py`). The pipeline stores every OCR reading
with a timestamp (§25) and every vision observation; those stored rows are
the profile's raw material.

**3. Mine the stored analysis.**

```
python scripts/profile_report.py <project-id>
```

Read-only. It prints the top on-screen strings with counts, the top vision
labels, the strings that look signature-worthy (read at least 5 times,
contain a letter, not generic interface vocabulary), and a ready-to-paste
`signature_patterns` JSON snippet with the escaping already in the shipped
style: `\b`-anchored, whitespace as `\s+`, metacharacters escaped.

**4. Turn top strings into signatures — keeping only the game-unique.** The
rule: **a signature must be text no other game writes.** Place names
(`VINEWOOD`, `LOS\s+SANTOS`), item names (`Milk\s+Molar`,
`Dandelion\s+Tuft`), mode banners (`SCENE\s+CREATOR`). Not "Settings", not
"Craft", not "VICTORY" — those name an interface or an event, never a game.
The script's generic-vocabulary filter removes the obvious ones; the
judgement call on the rest is yours. Where the mined list shows several
misspellings of one phrase, write one tolerant pattern covering the observed
misreads (`DI\w{0,2}ECTO\w?\s+MODE`) rather than one pattern per spelling —
and no wider than what you observed, because every wildcard admits other
games' words too. Aim for 8–12 patterns: detection needs 3 hits with a
margin of 2, and OCR will not read all of yours on every recording.

**5. Add event rules for what your footage actually shows.** The death
banner, the mission-passed banner — as the *whole-reading* or
*region-scoped* shapes above. Then ignore patterns for every piece of chrome
in the report's top strings, and fusion rules for the label+source
combinations your unnamed events keep showing.

**6. Validate the way the shipped profiles are validated.** The templates
are `TestTheShippedGroundedProfile` and `TestTheShippedGtaProfile` in
`tests/unit/test_gaming.py`. The checks that matter, in order:

- The file loads and is not generic (`load_profile` raises for a malformed
  profile — a broken profile silently becoming generic would look like the
  feature working).
- `detect_game` over your recording's actual OCR strings returns **your game
  with 0 runner-up hits** — and the other shipped profiles still detect
  their own games with 0 runner-up hits. A signature that fires on the wrong
  game is worse than none: it applies rules written for footage this is not.
- Every observed misspelling of your signature phrases matches
  (parametrised, one test per spelling, as the GTA tests do).
- The chrome is ignored (`should_ignore`) and the event banners are not.
- The death banner produces a `death` through `observations_from_ocr`.

Then re-run the analysis on the same project with the profile in force and
compare: named events, `unknown_event_ratio`, and what the remaining unnamed
events show. That before/after is the profile's measurement, and it belongs
in your contribution's description.

## Pitfalls — each one paid for in this repository

**Shipping structure without signatures.** The GTA profile had seven measured
HUD regions and region-scoped `WASTED`/`BUSTED`, and none of it ever ran:
zero signature patterns meant detection could never select it, and every GTA
recording fell back to generic. On the `auto` path — which every real project
has used — signatures are the door to everything else in the file.

**Unanchored event words.** `\bdefeat\b`, unanchored, read *"Defeat the
O.R.C. guards at the Milk Molar stash"* — a quest objective held on screen
for minutes — as a DEFEAT **nineteen times in one recording**. It was the
most common named event in the whole project and every one of them was
wrong. A banner says DEFEAT and nothing else; an objective says defeat
*something*. Anchor to the whole reading (`^\W*DEFEAT\W*$`), scope to a
region, or ignore the tracker's phrasing outright (`\bDefeat\s+the\b`).

**Trusting the correct spelling.** `DIRECTOR MODE` came back as `DIFIECTOR`,
`DINECTOH`, `DIPECTOR` and `DRECTOR` across 161 readings. OCR against
stylised game fonts misreads *consistently*, so the misreads are minable —
write the tolerant pattern from what was actually read, not from the game's
manual.

**Editor and menu chrome.** Director Mode's menus and Grounded's backpack
sat on screen for minutes at a time, so their labels dominate the OCR
volume. Without ignore patterns they are offered to every event rule and to
the generic fallback patterns on every frame. The report's top-strings list
is, in practice, mostly a list of what your ignore patterns need to cover.

**Treating overlay HUD as a screen.** The vision model's most common label
on one real Grounded recording was `inventory` — 181 observations — and it
used it for *"the HUD shows health and item icons"* while the player rode
across a field. Mapping it to a menu rejected 37 of 60 moments, nearly all
ordinary gameplay; the fix maps `inventory`/`scoreboard`/`map` to
`HUD_ONLY`, which counts as gameplay (`backend/analysis/frame_state.py`).
When the report shows those labels, they mean the HUD was drawn, not that
play stopped — do not write rules, ignore entries or region assumptions that
treat HUD-overlay moments as menu time.

## Contributing a profile

Drop a folder: `profiles/<game_id>/profile.json`. That is the whole
integration — detection scans every directory under `profiles/` on the
`auto` path (`_candidates` in `backend/gaming/detection.py`), the API's
profile router lists and validates what is on disk, and the dashboard's game
picker is the directory listing. No registration, no code change.

Two behaviours to know: a profile that fails to parse is *skipped with a
warning* during the automatic scan (a third profile nobody mentioned must
not stop the analysis), but *raises* when a user selects it by name — so
validate before shipping, for example through the profile API or by loading
it in a test. And keep §111 in mind: one real game validated before more are
written. A profile contribution should carry its measurement — the recording
length, the signature hits and runner-up hits, and what the profile named
that generic could not — the way every claim in the shipped profiles does.
