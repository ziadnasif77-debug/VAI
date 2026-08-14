# Phase 0 — Perception: teaching VAI what happened

**Status:** specification, not yet implemented
**Written:** 2026-08-15
**Precedes:** Phase D (duration/pacing optimizer), Phase C (director), Phase E (creative critic)

---

## 0. Why this phase exists

VAI 2.0's original plan opened with an evidence store and an event graph. Both
sit *above* the event layer. Measured on real footage, the event layer is where
the loss is — so a graph built now would organise a vocabulary that barely
exists, and a Director reading that graph would be asked to tell a story about
events nobody could name.

This phase does not make VAI smarter. It makes VAI **know what happened before
it tries to interpret it**.

---

## 1. Measured baseline

Read from `gaming_editor.db` on 2026-08-15, two real recordings:

| | سبي | Ziad 2 |
| --- | --- | --- |
| source duration | 76.8 min | 41.0 min |
| frames extracted | 2027 (26.4/min) | 985 (24.0/min) |
| vision observations | 339 (**4.4/min**) | 165 (**4.0/min**) |
| audio events | 1724 | 588 |
| scene boundaries | 868 | 325 |
| correlated game events | 116 | 47 |
| **named** (not `unexpected_event`) | **45 (39%)** | **14 (30%)** |
| `kill` / `multi_kill` / `clutch` | **0** | **0** |

Event types actually produced, all projects: `unexpected_event` 343,
`low_health` 41, `defeat` 20, `objective` 10, `funny_moment` 5, `outplay` 4,
`death` 4, `comeback` 4, `objective_failure` 3, `escape` 3, `near_death` 2,
`victory` 1, `fail` 1.

Vision labels actually produced (سبي, 339 observations):

```
inventory 181   driving 86   menu 78   combat 53   exploration 42
low_health 32   loading 7    gameplay 7   quest 3   navigation 3
defeat_screen 3 interaction 3  resource_gathering 1  dialogue 1  victory_screen 1
```

---

## 2. Diagnosis — seven findings

### F1. Seven of ten requested vision labels are discarded (largest single loss)

`prompts/vision/frame_description/prompt.md` enumerates ten labels for the
model:

```
combat, menu, loading, cutscene, driving, inventory,
scoreboard, low_health, victory_screen, defeat_screen
```

`backend/gaming/events.py:54` `_LABEL_EVENTS` maps **seven strings**, of which
only three appear in the prompt (`low_health`, `victory_screen`,
`defeat_screen`). The other four (`victory`, `defeat`, `boss`, `boss_health`)
are never requested, so they only ever arrive if the model invents them.

The model is asked for `combat`, produces it 53 times on one recording, the row
is stored — and `observations_from_vision` drops it, because the dict has no
key for it. Same for `menu` (78), `driving` (86), `loading` (7), `inventory`
(181).

This is the same shape as the effects defect fixed on 2026-08-14: **a writer
with no reader.**

### F2. The one shipped game profile is never loaded

`profiles/gta_v/profile.json` exists and declares:

- `WASTED` / `BUSTED` in `centre_banner` → `death`
- `MISSION PASSED|COMPLETE` → `objective`, `MISSION FAILED` → `objective_failure`
- a `wanted_level` glyph-row HUD indicator with `chase` (rise ≥1) and `escape`
  (fall) change rules

Every real project row has `game = 'auto'` → `load_profile` returns
`GENERIC_PROFILE` → every rule above is dead, and `observations_from_hud`
returns `[]` immediately because `profile.hud` is empty.

`projects.detected_game` is a column **nothing in the backend ever writes**
(`grep -rn "detected_game *=" backend/` returns nothing outside the model and
repository). Automatic game detection does not exist.

### F3. Naming sources are outnumbered ~8:1 by unnameable ones

Per recording: 1724 audio events + 868 scene boundaries, both of which can
only ever emit `UNEXPECTED_EVENT` by design (`events.py:194`, `events.py:246`,
and correctly so — a waveform spike is not a kill). Against them: 339 vision
observations, of which three label kinds are readable.

Result on سبي: **63 of 116 events are `["audio", "scene"]` clusters** — no
naming source present at all. Those 63 are the `unexpected_event` majority.

### F4. `kill` / `multi_kill` / `clutch` have no reachable path — and are the wrong vocabulary here

There is no vision label, no generic OCR pattern, and no HUD rule that can
produce them. The only generic text path is `\beliminated\b|\bknocked\s+out\b`
(`events.py:69`), which is shooter wording.

The footage is GTA-style open world. Its real vocabulary is `combat`, `chase`,
`wanted_level_change`, `collision`, `explosion`, `escape`, `death`,
`mission_start/end`. `config/effects.yaml` triggers, meanwhile, list
`kill, multi_kill, clutch, boss_defeat` — **the effects library speaks shooter
and the footage speaks open-world**. Fixing detection without fixing that
vocabulary mismatch would still leave effects unfired.

### F5. Vision density is one frame per ~13.6 seconds

4.4 observations per source minute. A collision, an explosion or a kill lasts
1–3 seconds. At this sampling rate the *event itself* is usually not in any
analysed frame — only its aftermath, or nothing.

The candidate cascade (`config/analysis.yaml:77` `only_candidate_regions:
true`) is correct in principle and is what keeps a 77-minute analysis
affordable. The defect is that candidate regions are sampled at the same
sparse rate as everything else, instead of densely *because* they are
candidates.

### F6. `menu` and `loading` are detected and then ignored — the menu defect has been solvable all along

Content QA warns on every project: *"22 moment(s) in the edit show a menu or
loading screen"* (سبي: 40). The vision model labelled 78 `menu` + 7 `loading`
frames on that recording. Nothing between vision and moment formation reads
those labels, so menus reach the edit and QA reports them **after** the render.

### F7. The narration reader is the richest naming source and nobody planned it that way

`backend/analysis/narration.py` (the LLM transcript reader) is the sole
producer of `outplay`, `comeback`, `escape`, `near_death`, `fail` and most
`objective` events. It works because the player *says* what happened. It should
be strengthened and trusted, not replaced by the graph work.

---

## 3. What Phase 0 changes, file by file

### 0.1 — Vocabulary inventory *(this document, done)*

The table in §1 and the findings in §2 are the inventory. No code.

**Deliverable:** this section, kept current when detectors change.

---

### 0.2 — Evidence fusion: name what no single detector could name

**The detection/classification split already exists**: `gaming/events.py`
observes, `gaming/correlation.py` fuses. What is missing is that
`correlation._resolve_type` can only choose among names the detectors already
produced (`max()` over their types). If no detector could name the instant,
the cluster stays `unexpected_event` however much evidence agrees.

Phase 0.2 adds a **fusion rule table** consulted before that fallback: a rule
matches a *bundle* of evidence and names the cluster.

**Files:**

- `backend/gaming/fusion.py` *(new)* — `FusionRule`, `classify(cluster, profile)`.
  A rule declares required evidence and yields a type:

  ```python
  FusionRule(
      event_type=GameEventType.COMBAT,
      requires_labels=("combat",),          # from vision observations
      requires_sources=("audio",),          # something was heard
      min_confidence=0.5,
      confidence=0.72,
  )
  ```

  Rules are ordered; the first match wins; no match leaves the existing
  behaviour untouched. Pure function, no I/O.

- `backend/gaming/correlation.py` — `_to_event` calls `classify` when
  `_resolve_type` returned a generic type. The named result carries
  `metadata["named_by"] = "fusion:<rule>"` so §21 provenance survives.

- `backend/gaming/events.py` — `EventObservation.detail` already carries
  `label` for vision; ensure every detector puts its distinguishing evidence
  in `detail` so a rule can read it without new plumbing.

**Rules to ship (generic, no profile needed):**

| bundle | → type |
| --- | --- |
| vision `combat` + audio spike | `combat` |
| vision `driving` + audio spike + scene change | `collision` |
| vision `low_health` + audio spike | `near_death` |
| vision `menu`/`loading` alone | *suppressed, not an event* (see 0.6) |
| reaction `scream` + audio spike + any gameplay label | `unexpected_event` (unchanged) |

**Acceptance:** on سبي, `["audio","scene"]`-only clusters fall below 40% of
events; no event is named by a rule whose evidence is absent from its
`metadata.detail`.

---

### 0.3 — Profile-driven vocabulary, and a profile that actually loads

**Files:**

- `backend/core/models/enums.py` — extend `GameEventType` with the open-world
  vocabulary the footage needs: `COMBAT`, `CHASE`, `COLLISION`, `EXPLOSION`,
  `WANTED_LEVEL_CHANGE`, `MISSION_START`, `MISSION_END`. (`ESCAPE`, `DEATH`,
  `NEAR_DEATH`, `OBJECTIVE`, `OBJECTIVE_FAILURE` already exist.)

- `backend/gaming/profiles.py` — add to `GameProfile`:
  - `fusion_rules: tuple[FusionRule, ...] = ()` — profile rules are consulted
    before the generic table, same precedence principle as `event_rules`.
  - `signature_patterns: tuple[str, ...] = ()` — OCR text that identifies this
    game (`WANTED`, `WASTED`, `LOS SANTOS`, mission-name furniture).

- `backend/gaming/detection.py` *(new)* — `detect_game(ocr_frames,
  vision_labels, profiles_dir) -> str | None`. Cheap, deterministic, no model:
  count `signature_patterns` hits per profile, require a margin over the
  runner-up, return `None` when unclear. Runs inside the existing OCR or
  GAME_EVENTS worker; no new stage.

- `backend/pipeline/workers/game_events_worker.py` — when `project.game` is
  `auto`, call `detect_game`, write `projects.detected_game`, and use the
  resolved profile for this run. `Project.effective_game` (`project.py:199`)
  already reads `detected_game` — the column simply has never been filled.

- `profiles/gta_v/profile.json` — add fusion rules for `chase` (wanted_level
  rise + `driving` label), `collision`, `explosion`; keep the existing OCR
  rules; keep `unexpected_event` only as the profile's own fallback.

**Acceptance:** a project imported with `game: auto` on GTA footage ends with
`detected_game = 'gta_v'`; the wanted-level HUD indicator produces at least one
`chase` and one `escape` event on a recording where the player is chased;
detection returns `None` (not a wrong guess) on footage from an unknown game.

---

### 0.4 — Temporal bundles: classify a window, not a frame

A single frame cannot distinguish `chase → collision → explosion → reaction`
from "a car is on screen". The provider already accepts multiple frames per
call — `prompt.md` says *"You are given {frame_count} frame(s) ... in order"* —
so this is a **sampling and prompting** change, not a provider change.

**Files:**

- `backend/analysis/candidates.py` *(existing cascade)* — for each candidate
  region, emit a bundle request at offsets `-2, -1, 0, +1, +2, +4` seconds
  around the candidate peak instead of the current flat interval.

- `prompts/vision/event_classification/` *(new prompt, versioned per §92)* —
  asks a different question from `frame_description`: given this ordered
  bundle and the audio/HUD/speech evidence at the same instant, **what
  happened?** Answer constrained to the profile's vocabulary plus
  `unknown`. Same honesty rules; `unknown` must remain cheap to say.

- `backend/pipeline/workers/vision_worker.py` — route candidate bundles to the
  classification prompt, keep single-frame description for context frames.

**Guard:** the classification answer is an `EventObservation` with
`source="vision_classifier"` like any other — it does **not** bypass
correlation, and it cannot outvote a profile OCR rule.

**Acceptance:** the frames analysed per candidate rises without the total VLM
call count rising more than 2× (bundles replace singles, they do not add to
them); on the diagnostic set, ≥60% of labelled events have at least one
analysed frame inside the event span.

---

### 0.5 — Perception metrics, reported per run

**Files:**

- `backend/analysis/metrics.py` *(new or extend)* — compute and log:

  ```
  vision_frames_per_source_minute
  candidate_frames_per_source_minute
  named_event_ratio            = named / all events
  unknown_event_ratio          = unexpected_event / all events
  non_gameplay_ratio           = non-gameplay seconds / analysed seconds
  events_per_source_minute
  naming_source_coverage       = events with ≥1 naming source / all events
  ```

- `backend/pipeline/workers/game_events_worker.py` — write them into the job
  result, so they appear in the run report and can be diffed between runs.

- `scripts/evaluate.py` — print them per project; they become the Phase 0
  regression gate.

**Baseline to beat (measured today):** `named_event_ratio` 0.39 / 0.30,
`unknown_event_ratio` 0.61 / 0.70, `vision_frames_per_source_minute` 4.4 / 4.0.

---

### 0.6 — Frame state: stop editing menus into the video

The evidence already exists (F6). This turns it into a first-class signal.

**Files:**

- `backend/core/models/enums.py` — `FrameState`: `GAMEPLAY`, `MENU`,
  `LOADING`, `CUTSCENE`, `HUD_ONLY`, `PAUSE`, `TRANSITION`, `UNKNOWN`.

- `backend/analysis/frame_state.py` *(new)* — `state_for(labels) -> FrameState`
  and `spans(observations) -> tuple[Span, ...]`, merging adjacent same-state
  observations into intervals. Deterministic, label-driven, no model.

- `backend/moments/formation.py` — a candidate whose **core** span overlaps a
  non-gameplay span by more than a configured fraction is rejected before
  scoring. Context may still touch a menu (a loading screen either side of a
  mission start is honest); the core may not be one.

- `config/moments.yaml` — `non_gameplay.max_core_overlap: 0.25`.

- `backend/qa/content.py` — the existing `accidental_menu_section` check
  becomes a *regression detector* rather than the first line of defence: if it
  still fires after this, formation let something through.

**Acceptance:** on سبي and Ziad 2, `accidental_menu_section` drops from 40 and
22 moments to ≤5; no clip that a human would call gameplay is lost (checked
against the diagnostic set).

---

## 4. Diagnostic dataset (small and deliberate)

Not a benchmark. A microscope for the detector.

**Size:** 3–5 recordings from the existing `D:\Gaming 2026` set, 30–40 labelled
events total:

```
10 combat / fight moments
 5 deaths
 5 collisions or explosions
 5 chases (wanted level rising)
 5 player reactions
 5 menu / loading stretches (negative examples)
```

**Format:** `datasets/phase0/<recording>.jsonl`, one object per label:

```json
{"start": 812.4, "end": 818.9, "type": "collision", "notes": "car hits truck, player laughs"}
```

**Rules:** labels are written **before** running detection on that recording;
they record what a human sees, not what VAI produced; boundaries are generous
(±1 s) because Phase 0 measures *whether* an event was found, not yet how
precisely it was bounded.

**Cost:** roughly 2–3 hours of one person's time, against the 10–20 hours the
original §24 benchmark implied. Expand only after `named_event_ratio` moves.

---

## 5. Phase 0 acceptance gate

Phase 0 is complete when, on the diagnostic set and both real recordings:

1. `unknown_event_ratio` falls from ~0.65 to **≤ 0.35**.
2. `combat` (or the profile's equivalent) is detected with ≥0.6 recall against
   the diagnostic labels.
3. `death` is detected on GTA footage via the profile's `WASTED`/`BUSTED` rule.
4. `chase` and `escape` fire from the wanted-level HUD at least once.
5. Multi-evidence chains are visible: ≥20% of named events carry ≥3 sources.
6. Event timestamps land within ±1.5 s of the diagnostic labels.
7. Every named event's `metadata.detail` shows the evidence that named it
   (§21 provenance, already the shape correlation stores).
8. `non_gameplay_ratio` is reported, and `accidental_menu_section` drops ≤5.
9. **No VAI 1.0 regression**: full suite green, QA verdicts on the three
   existing projects no worse than today.

A gate failure is a stop, not a warning. Phase D does not start early.

---

## 6. What Phase 0 deliberately does not do

- **No evidence table.** The analysis tables are the evidence; Phase A will
  project over them, not copy them.
- **No event graph.** Relations between events are worth building once the
  events have names (Phase B).
- **No new LLM in the pipeline.** 0.4 reuses the existing VLM with a second
  prompt; 0.2, 0.3, 0.5 and 0.6 are deterministic code.
- **No new stage.** Everything lands inside the existing VISION, OCR and
  GAME_EVENTS workers.
- **No change to the §23 bargain.** A profile still improves accuracy and is
  still never required; `unexpected_event` remains a legitimate answer when
  the evidence genuinely cannot name the instant — it simply stops being the
  answer to two thirds of the questions.
