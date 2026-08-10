# Phase 5 — Gaming Intelligence

SPEC §21–§27; §126 steps 10–11. **Acceptance: gameplay events detected with
timestamps, without a game profile (§23). A profile improves accuracy; it is
never required.**

Status: **complete and verified.** 703 tests pass, `ruff check` is clean, and
both halves of the criterion run through the whole pipeline on real files — the
same recording, once as `game: auto` and once against a profile written for it.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Game profiles (§22) | `backend/gaming/profiles.py` | `TestProfiles` — 10 tests |
| Unknown games (§23) | generic profile + fallback resolution | `TestWithoutAProfile` — 5 tests |
| OCR (§25) | `ai/ocr/`, `backend/gaming/ocr.py` | `TestFakeOcrProvider`, pipeline tests |
| Event detection (§21, §26) | `backend/gaming/events.py` | `TestDetectorsWithoutAProfile` — 10 tests |
| Correlation (§27) | `backend/gaming/correlation.py` | `TestCorrelation` — 13 tests |
| OCR stage | `backend/pipeline/workers/gaming_workers.py` | pipeline tests |
| GAME_EVENTS stage | same | pipeline tests |
| Persistence + migration 0003 | `repositories/gaming.py` | pipeline tests |

`IMPORT → … → SCENES → VISION → OCR → GAME_EVENTS` now runs end to end.
MOMENTS is the frontier.

---

## What may be claimed without a profile

§23 is the constraint that shapes everything here, and the honest answer to
"what can you detect about a game you know nothing about" is narrower than the
§21 taxonomy. So each detector reports only what its evidence supports:

| Source | What it may say |
| --- | --- |
| Vision labels | `victory_screen` → VICTORY, `low_health` → LOW_HEALTH. Screen *states* a model can report without knowing the game |
| OCR, generic | Words that mean the same thing everywhere: VICTORY, DEFEAT, ELIMINATED, MISSION FAILED |
| OCR, with a profile | This game's wording, in the box the profile says it appears in |
| Audio | `UNEXPECTED_EVENT` — a loud transient is a fact about the waveform; calling it a gunshot would be a claim about the game |
| Microphone | Laughter → FUNNY_MOMENT. The one signal that names a moment's character without knowing the game |
| Scenes | `UNEXPECTED_EVENT` at 0.25 confidence. §17 says boundaries are supporting information, so a scene change alone can never become an event |

The free-text VLM description is deliberately **not** parsed. §93 forbids taking
a pipeline decision from uncontrolled prose, so a model writing "the player gets
an incredible triple kill" produces no event — only its structured labels do. A
test asserts exactly that.

`UNEXPECTED_EVENT` is the taxonomy's own name for *something happened and we
cannot say what*, and using it is the honest generic answer. A wrong `CLUTCH`
reaches the narrative stage as a fact and gets edited into the video.

---

## Correlation: one event, not three

§27's own example is the test:

> Kill-feed change + weapon sound + "NO WAY" becomes **one** high-confidence
> gameplay moment.

Three observations in, one event out, typed `kill` — from the only source that
could know — with all three sources recorded and confidence **higher than any
single detector had**.

Two rules make that work:

**The type comes from the source that could know it.** Audio was the most
confident detector in that example, and it still does not decide: a specific
type beats a generic one outright.

**Agreement raises confidence, in two grades.** A source that agrees on the
*type* corroborates the claim. A source that saw the instant but did not name
it corroborates that something real happened there — weaker evidence for "it was
a kill", but not none: a kill-feed reading with a weapon sound under it is
likelier to be real than one over silence. It counts for half.

Confidence never reaches 1.0. No amount of agreement between inferring
detectors turns an inference into a fact, and a stored certainty would tell
every later stage this one is beyond question.

Clustering is against the group's growing span, not its first member — otherwise
a run of observations a second apart fragments into several events, which is the
"three explosions" failure §27 exists to prevent.

---

## The OCR problem on this machine, and what it changed

PaddleOCR is installed here and **does not import**: PaddlePaddle 2.6.2 ships
protobuf-generated code that protobuf 6 refuses to load. Three things followed.

**`doctor.py` was lying.** It checked `importlib.util.find_spec("paddleocr")`,
which finds a broken package perfectly well, and reported OCR as available. It
now resolves the engine the way the pipeline does, and distinguishes "not
installed" from "installed but not importable".

**Availability had to be probed in a subprocess.** A failed `import paddleocr`
does not merely raise — it leaves the interpreter's protobuf state broken, and a
subsequent `import easyocr` then fails too. An in-process probe therefore turns
one broken engine into *no* working engines, and `auto` picks nothing on a
machine with a perfectly good EasyOCR installed. The probe now runs in a
subprocess, once, cached.

**The shipped default changed to `auto`.** `engine: paddleocr` resolves to
nothing here. `auto` picks the first engine that actually imports, which is the
right default for a local-first product shipped to machines nobody has seen.

On this machine that resolves to EasyOCR, with CUDA.

---

## Other decisions worth knowing

### Profile regions are fractions, not pixels

A profile written against 1080p gameplay has to keep working on the 720p proxy
the analysis actually reads. A pixel rectangle does not; `(0.7, 0.05, 0.28,
0.25)` does.

### Region-restricted OCR is cropped, not hinted

No OCR engine's API takes a region of interest, so the accuracy gain only
becomes real if the crop does. A recogniser given a tight crop of a kill feed is
not also trying to read the minimap, the crosshair and the watermark.

### GAME_EVENTS and MOMENTS are per-media stages

They were queued nowhere. `queue_import_chain` queues only per-media stages,
`queue_project_stages` was never called, and the pipeline could not reach
GAME_EVENTS at all. The schema settles where the line belongs: `game_events`
and `moments` both carry a `NOT NULL media_id`, because an event is something
that happened *in a recording*. Only from STORY onward does the pipeline reason
across every file at once.

### OCR reads candidate keyframes

The same cascade the vision model uses, for the same reason: reading every
sampled frame of a two-hour recording is an afternoon of work to find the four
minutes that mattered.

---

## Acceptance

### Without a profile

Run as `game: auto` on a real clip through the whole pipeline:

* events produced, with timestamps inside the recording;
* every event recording `game_profile: generic`;
* on-screen text read and timestamped (§25), region `full_frame`;
* "VICTORY" becoming a **named** VICTORY event through generic wording alone;
* every event carrying the detectors that saw it, and at least one event with
  more than one source;
* **more observations than events** — correlation merging rather than
  multiplying.

### With a profile

The same clip, against a profile declaring two regions and one rule:

* OCR switching to `mode: regions` and reading only the declared boxes;
* wording only that game uses (`ROUND WON`) producing a VICTORY event that the
  generic path correctly declines to claim;
* an unknown game falling back to generic with `profile_exact: false` — the
  substitution recorded, not hidden.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| GAME_EVENTS and MOMENTS were queued by nothing | The pipeline could never reach event detection at all |
| `doctor.py` reported a broken PaddleOCR as available | The environment report would say OCR works right up until a stage tried to use it |
| In-process engine probing | One broken OCR package made every other one unavailable |
| A test wrote a profile into the real repository | `profiles/testgame/` appeared in the developer's checkout; the `paths` fixture now redirects `profiles_dir` too |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| A real game profile | §111: one game validated before more are written, and validating needs real gameplay footage rather than a colour bar |
| HUD field extraction (§24) | The regions and the reader exist; turning "83" in the health box into a tracked health value needs a real profile to be worth anything |
| `hud_change` as a cascade trigger | Reserved in the cascade; it needs §24's tracked values |
| Multi-kill / clutch inference | These are patterns *over* events — several kills close together, a kill at low health. They belong with moment formation (§28) |

---

## Verification status

| Check | Result |
| --- | --- |
| `pytest` | **703 passed**, 4 skipped |
| `ruff check .` | clean |
| Events without a profile | passed |
| A profile improving the result | passed |
| §27 merging rather than multiplying | passed |
| `doctor.py` | 1 warning — the Phase 13 LLM model |

The four skips are the opt-in real-model tests from Phases 3 and 4.

The full run is about 17 minutes because it transcodes and analyses real clips.
For the development loop:

```bash
python -m pytest -m "not slow" -q
```

---

## Gate to Phase 6

Met: `pytest` green (703), `ruff` clean, both halves of the acceptance criterion executed against real
files through the whole pipeline.

Phase 6 begins at §126 step 12: moment formation (§28), context expansion (§29),
dead time (§30), repetition (§31) and scoring (§32) — with §33 hanging over all
of it: *the highest score is not necessarily the best clip*. The scorer produces
a number; the narrative stage decides what to do with it.
