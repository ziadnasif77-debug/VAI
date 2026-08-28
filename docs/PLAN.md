# Master Plan and Progress

**The file to read first when resuming work on this project, from any machine.**

| | |
| --- | --- |
| Product | AI Gaming Video Editor — local-first |
| Specification | [`docs/SPEC.md`](SPEC.md) — every `§N` reference in the code points there |
| Branch | `claude/local-ai-youtube-editor-ixsrt8` |
| Last updated | 2026-08-12, end of Phase 15 |
| Current phase | **All 15 phases complete.** Next: whatever the numbers in PHASE_15 point at |
| Tests | 1314 passing (4 opt-in model tests skipped by default) |
| Backend code | ~38,000 lines across `backend/` and `ai/`, plus the `remotion/` project |

---

## 1. Resume here

Run it:

```bash
python scripts/serve.py          # everything → http://127.0.0.1:8765
```

One command: the API, the job worker, and the built interface, which the API
serves itself. `VAI.bat` does the same by double-click, and picks an
interpreter that has the dependencies — on a machine with several Pythons,
plain `python` is whichever is first on PATH and is rarely the right one, so
`py -3.11 scripts/serve.py` is the reliable spelling by hand.

For UI work, `npm run dev -w apps/web` gives Vite's reloading dev server on
port 5173 instead.

```bash
git clone https://github.com/ziadnasif77-debug/VAI.git
cd VAI
git checkout claude/local-ai-youtube-editor-ixsrt8

python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip

.venv/bin/python -m pytest               # expect 1314 passing (~24 min)
.venv/bin/python -m pytest -m "not slow" # the fast development loop
.venv/bin/ruff check .                   # expect clean
.venv/bin/python scripts/doctor.py       # what this machine is missing
.venv/bin/python scripts/db_init.py      # create/migrate the database
```

**Everything this project writes stays inside the repository.** The data root
defaults to the repository root, and `pyproject.toml` pins
`--basetemp=.pytest-tmp` so test artefacts -- transcoded proxies, extracted
audio, frame dumps -- land here too rather than in the system temp directory.
On the development machine `C:` has under 20 GB free against recordings that
run to gigabytes each, so this is a hard constraint, not a preference. Model
weights follow the same rule, with one exception that is not ours to make:
faster-whisper downloads into `models/`, while Ollama's store is wherever the
machine's `OLLAMA_MODELS` says — read and reported here, never written. See
[`SHARED_MODELS.md`](SHARED_MODELS.md).

The overlay renderer needs Node (already present) and its own install:

```bash
cd remotion && npm install
```

Without it the pipeline still runs; overlays are skipped and the video has no
captions (§95). `scripts/doctor.py` reports which it is.

FFmpeg is required from Phase 2 onward. On Windows, in an **elevated** shell:

```
choco install ffmpeg-full -y
```

If `pytest` is green and `doctor.py` reports only warnings, the checkout is
healthy, the checkout is sound. What to do next is in
[`docs/PHASE_15.md`](PHASE_15.md): the first quality numbers, and what they
point at.

---

## 2. Status at a glance

Development order follows SPEC §126.

| # | Phase | Scope | Status |
| --- | --- | --- | --- |
| 1 | **Foundation** | repo, config, logging, database, project + media models, API, job system | ✅ **done** |
| — | *Interaction layer* | editing intent, Q&A, commands, conversation, versions | ✅ **done** (added mid-Phase-1) |
| — | *Effects engine* | 22-effect library, planner, budgets | ✅ **done** (added mid-Phase-1) |
| — | *Publishing seam* | local-file target, YouTube slot | ✅ **done** (added mid-Phase-1) |
| 2 | **Media Engine** | FFmpeg/FFprobe, proxy, audio, frames, chunking | ✅ **done** |
| 3 | **Speech / Audio** | Whisper transcript, audio events, reactions | ✅ **done** |
| 4 | **Vision** | scene detection, keyframes, VLM analysis | ✅ **done** |
| 5 | **Gaming Intelligence** | OCR, HUD, game events, correlation | ✅ **done** |
| 6 | **Moments** | formation, context expansion, scoring, dead time, repetition | ✅ **done** |
| 7 | **Narrative** | story / best-moments / compilation, hook, pacing, duration optimizer | ✅ **done** |
| 8 | **EDL & Timeline** | clips, tracks, effects placement, captions | ✅ **done** |
| 9 | **Remotion** | overlay composition | ✅ **done** |
| 10 | **Final Render** | FFmpeg encode, audio mix, YouTube preset | ✅ **done** |
| 11 | **QA** | technical + content verification | ✅ **done** |
| 12 | **UI** | dashboard, import, analysis, moments, timeline, export, chat | ✅ **done** |
| 13 | **NL editing (LLM)** | LLM fallback for unparsed instructions and questions | ✅ **done** |
| 14 | **Game Profiles** | one real game, then a profile API | ✅ **done** |
| 15 | **Quality** | golden dataset, precision/recall benchmarking, packaging | ✅ **done** |
| C | **The Director** | a model proposes the shape; code selects, orders and cuts | ✅ **done** |
| E | **The Critic** | the pipeline reads its own edit and may trim it before rendering | ✅ **done** |
| F | **Preferences** | what the person keeps asking for becomes the next project's default | ✅ **done** |
| A | **Evidence projection** | one read across the analysis stores, one definition of "near" | ✅ **done** |
| B | **Episodes** | one situation, however many events it was reported as | ✅ **done** |
| P1 | **YouTube upload** | device-flow OAuth + resumable chunked upload, publish as a job | ✅ **done** |
| P1 | **Shorts 9:16** | strongest moments as vertical cuts through the same stack | ✅ **done** |
| P1 | **Distribution** | `scripts/package.py` → 296 MB zip around the self-repairing launcher | ✅ **done** |
| P2 | **Timeline UX** | trim/split/move/delete controls in the UI over the §42 API, refusals shown verbatim | ✅ **done** |
| P2 | **Upload metadata** | evidence-built title/description/tags/chapters/thumbnail, `POST /metadata/suggest`, wired into Export | ✅ **done** |
| P2 | **Profile authoring** | docs/PROFILES.md + `scripts/profile_report.py` signature miner | ✅ **done** |

### Phase F — preferences ✅ done

The lesson written next to `chronological` in `EditingIntent`, generalised:

> a default the user has to re-defeat per project is not a default

That one was settled by changing the shipped value, which works exactly once
and only for a preference the author happens to share. Everything else someone
re-types every project — "make it faster", "fewer effects", "no fails" — was
still re-typed every project.

A preference is a change made in **several separate projects**. One instruction
is a mood; three across three projects is how they want their videos. It is
read from `editing_intent_updates`, which §4 has kept since Phase 13 — no new
table, no writer, no migration.

Four rules, each of which the alternative gets wrong:

- **Separate projects, not repeated instructions.** Saying "faster" three times
  in one project is one opinion repeated, usually because the first two did
  not take.
- **The value they settled on.** A project that went faster, then slower, then
  fast contributes *fast*, not a vote for each.
- **Recent projects only.** Nothing is unlearned by a rule; a habit simply
  falls out of view once enough newer projects disagree.
- **Nothing inferred from silence.** A project with no instructions is much
  more often nobody looking than agreement.

It sits between the preset and the instructions, which is the whole ordering
question in one line: a preference beats what shipped in the box, and anything
said about the current project beats a preference. "Keep it slow this time"
gets it slow this time and unlearns nothing. `GET /preferences` exposes what
was learned, with the projects it came from and the person's own words — a
preference nobody can see is indistinguishable from a bug.

Measured on this machine's database: 12 projects considered, **nothing
learned**, because the real projects were driven by defaults rather than typed
instructions. That is the correct answer and the reason `considered` is carried
next to `learned`.

**Found while building it:** the instruction parser read "avoid fails" as a
*priority* for fails. `_NEGATIONS` held `no`, `without` and the comparatives
but none of the plain verbs, so someone asking for fewer of something got more
of it and nothing in the output said so. Fixed, with `avoid`, `skip`, `remove`,
`exclude`, `تجنب`, `احذف` and `شيل`. It mattered enough to fix inside this
phase because Phase F would have seen the same sentence in three projects and
made the inversion the default for every project after them.

### The import screen learns delivery — captions, a folder, and hands-free publish (2026-08-28)

Three owner requests, one shape: choices made once, at the import screen,
recorded on the project row (migration 0004, schema 4).

**Captions became opt-in.** `captions_enabled`, off by default: a new project
writes nothing on the frame — the long video and the Shorts both — while the
speech is still transcribed, because the edit is built from what was said.
Every project that already existed was created when captions were
unconditional and keeps the behaviour it was made with (the migration
backfills true). The gate sits where the captions are born: the EDL worker
returns no captions for a project that said no, and the Shorts worker ships
the plain cut.

**The finished video lands where the person asked.** `output_directory`,
validated at the model (absolute path; C: refused by this machine's own
standing rule), applied at the render: a successful render copies the file
there under the project's name. The copy is a delivery, not the render — a
full disk at the chosen folder becomes a note on a green job, never a failed
one (§95).

**Auto-publish, with §51 intact.** `auto_publish`, off by default. When
ticked, a green QA queues the YouTube publish by itself, and the publish
worker writes the metadata the way the Export screen's Suggest button would
have — from the analysis. Nothing is delivered *unasked*: the tick at the
import screen is the asking, made once, explicitly, per project. The Export
screen also gained the one-press version — **Publish to YouTube now** —
suggest and publish in a single click for projects that did not pre-ask.

The publishing target ships enabled now (the flag and the button need it to
exist); what must never ship stays null — an unconfigured machine gets the
remedy message, not an upload. 1,671 unit tests; the web app rebuilt.

### Proved live, then shipped — Shorts, the gate, the grammar (2026-08-27)

Three live runs on real footage closed the day, each exercising what the day
built.

**Shorts, for real this time.** The stage had only ever run against fixtures.
On the Grounded recording it cut three finished verticals in 296 s — clutch,
boss and victory, sixty seconds each at 1080×1920 with captions burned — into
`renders/shorts/`. Nothing was skipped and no §95 note was needed.

**The certification render, with the day's features on.** The GTA
out-of-sample project ran story → edl → critique → render → qa in 5.3
minutes: the Director consulted, the live 7B Critic reviewing (it flagged the
menu opening and an over-long first clip — taste improving, guardrails
unchanged), a 552.5 s 1080p60 file, and **QA passed** with two honest
warnings (5.8 s of black that the recording itself contains — the death
fade). The VRAM gate was consulted on a card showing 7,029 MB free and
correctly did nothing; the card ended at 7,033 MB with nothing resident. The
J/L planner was consulted at the render's one internal boundary and correctly
kept it **hard** — no speech opens or tails either side there, and the
grammar only offsets a cut that speech justifies. The audible proof of the J
lead lives in the spectral integration test; the certification proves the
consult path and that QA's sync check holds with the feature live.
`jl_cuts.enabled` ships **on** after that pass.

**The package, rebuilt.** `dist/VAI-0.1.0.zip`, 296 MB with a fresh sha256,
now carries the detector wave, both render features and the updated profiles.

What live YouTube publishing still needs is the one step that is not code's
to take: an OAuth client from the owner's own Google Cloud console
(`client_id` into `config/publishing.yaml`, the client secret into
`.credentials/`), after which Connect in the Export screen runs the device
flow end-to-end. The 39 publishing tests cover everything up to Google's
front door.

### The detector wave — every gap the golden set named, answered with evidence (2026-08-27)

Each of the expansion's misses was diagnosed from stored rows before anything
was written, and each fix is the smallest thing the evidence supports.

**Grounded was inventing action, and the mechanism had three parts.** The 2.5 s
correlation window chains transitively, and on dense footage (vision every
5 s, ambient transients under it) it built clusters of 59–96 seconds; one
observation then named the whole thing (a single narration `outplay` named a
minute of base-building), and `_combine` rewarded the pile-up with 0.9+
confidence. Three rules now hold the line: **context does not bridge** (a
screen description attaches to the instant beside it but never extends the
chain), **claiming evidence is capped at 15 s** (`MAX_CLUSTER_CLAIM_SECONDS`,
under the episode layer's 20 s knee — past that it is two events, and whether
they are one situation is the episode layer's question), and **a state read by
one kind of sensor is context, not an event** (vision-only `low_health` is the
model reading Grounded's always-on hunger dials; demoted to generic, it can
still become `near_death` when audio corroborates through fusion).

**Profiles can now contradict the generic table.** The generic
`combat_seen_and_heard` rule exists because GTA's vision `combat` labels are
real; Grounded's are a player *holding a bow*. `suppressed_generic_rules`
lets the profile that knows better say so by name — Grounded vetoes
`combat_seen_and_heard` and `driving_impact`, and its real fights are caught
instead by what the evidence actually shows: the creature's own health bar in
OCR (`WEEVIL` at 27:24, `ORB WEAVER` at 28:08 — new anchored event rules) and
a tightened `creature_fight` that needs two high-confidence combat frames
plus a creature name in the prose. `WOOZY`, the game's own text, replaces the
invented `hurt_and_heard` as the `near_death` signal.

**Rules can read what the model wrote, not only how it labelled.**
`FusionRule` gained `description_pattern` and `min_label_count`. The fire the
golden set marked and the pipeline missed was in the prose at every fire —
"vehicle on fire", "engulfed in flames" — while the *label* stayed `combat`.
A generic `visible_destruction` rule and a GTA `burning_wreck` rule (placed
ahead of `shootout`, because profile rules are consulted in order) now name
them: ten `high_damage` events on the out-of-sample cut, four of them inside
labelled spans, including the freeway wrecks at 49:41 the window ends on.

**The metric stopped counting the unjudgeable.** Generic claims
(`unexpected_event` — the correlator saying *something happened here*) now
get the boundary-straddler treatment: they may find a label, but an unmatched
one is reported as a marker, not a false positive, because a person cannot
label "unexpected". Ties in greedy matching break on type agreement — a
death prediction spanning a rare-loot pickup and the death eight seconds
later now matches the death (it previously matched the pickup and reported
the found death as missed).

Events at situation granularity, every label, across the day's three passes:

| window | expansion baseline | after episode scoring | after the detector wave |
| --- | --- | --- | --- |
| Grounded 20–30 | 0.42 / 0.83 / f1 0.56 | 0.56 / 0.83 / 0.67 | **0.60 / 1.00 / 0.75** |
| GTA 40–50 (OOS) | 0.35 / 0.82 / 0.49 | 0.53 / 0.82 / 0.64 | **0.50 / 0.73 / 0.59** |
| GTA 30–40 (seed) | 0.26 / 1.00 / 0.42 | 0.35 / 1.00 / 0.52 | **0.35 / 1.00 / 0.52** |

Grounded moments went from 0.60 / 0.75 to **0.80 / 1.00** — the poison arc,
the double-death comedy and both creature fights are all found, with one
unlabelled claim. The out-of-sample recall honestly *fell* to 0.73: the three
remaining misses are documented limits, not regressions — the police rams
left no impact evidence any sensor captured (night, no scene cut), the
ambulance fire was found at 47:19–47:35 where the 5-second labelling grid
put it at 47:35–47:50 (sampling parallax between two grids), and the
four-star night freeway run defeated every sensor that looked (star row too
dim for OCR, vision saw two frames of plain driving). Raw-row recall on the
same window is 0.91. The Grounded unknown-event ratio rose from 0.27 to 0.63
— which is the suppression telling the truth: most of what used to be
"named" there was wrong, and §23 always preferred an honest unknown to a
confident invention. 1,660 unit tests green.

### Fragmentation was the metric's problem, and the metric now speaks situations (2026-08-27)

The expansion's central finding — 20 of the GTA window's 26 event claims sat
inside or beside labelled spans and were penalised anyway — pointed at a
granularity mismatch, not a detector defect. The product already reads the
correlator's rows through `backend/gaming/episodes.py` (the Critic's
evidence, the metadata description); the labels are written about
*situations*; only the evaluator was still scoring raw sightings, so every
extra sighting of a found firefight counted as a false positive.

**The fix is one reader used one more time.** `read_as_episodes` in
`backend/quality/metrics.py` runs predictions through the same episode
reader with its measured 20-second knee: same-type runs become one
prediction spanning what the run covered at the run's best confidence;
generic types (`unexpected_event`, `rare_event` — the correlator saying it
could not name this) pass through unchanged, and so do labels the enum does
not know, because hiding a claim is not merging it. `scripts/evaluate.py`
buckets per media (a run must never span recordings), headlines the
situation-level score and prints the as-stored line beside it — the distance
between the two *is* the fragmentation, measured.

**Merging found a second bug at the window's edge.** The Grounded collision
episode now ran 19:53–20:48, straddling the watched window's start, and
`within_window`'s containment rule discarded it whole — turning the buggy
fail at 20:41, which its raw part had been finding, into a miss. The rule
now matches first and judges after: a straddler that finds a label inside
the window is a true positive (the label sits in watched footage); one that
finds nothing is discarded as out-of-window rather than counted wrong,
because its claim may live in the part nobody watched. `boring_overlap`
likewise counts a straddling moment's in-window boring seconds (Grounded:
139 s was really 155 s).

Events at situation granularity, every-label, against as-stored:

| window | precision | recall | f1 | as stored |
| --- | --- | --- | --- | --- |
| GTA 30:00–40:00 (seed) | **0.35** | 1.00 | 0.52 | 0.26 / 1.00 |
| GTA 40:00–50:00 (out-of-sample) | **0.53** | 0.82 | 0.64 | 0.35 / 0.82 |
| Grounded 20:00–30:00 | **0.56** | 0.83 | 0.67 | 0.42 / 0.83 |

No recall was paid anywhere, and what remains on the claimed-not-labelled
lists is now mostly real: the 41:42–42:24 collision run is the chase
crashing into Playa Vista (the label ended too early), the 45:34 collision
is the Surano carjack, the 46:09 `escape` is semantically correct. The
genuine detector frontier, unchanged by any scoring: the misadventure death
at 26:36, the night freeway chase whose star row OCR never read, fire, and
Grounded's invented action on quiet footage — `outplay` during
base-building, `combat` during foraging — which is where the 155 boring
seconds come from too. 54 quality tests, two of them written from the
boundary incident.

### The golden set triples — two windows, two games, out-of-sample (2026-08-27)

The precision numbers had a documented excuse: 16 labelled spans in one
ten-minute window of one game is a lower bound, not a measurement. The excuse
is now spent. Two new windows were labelled from fixed 5-second contact sheets
(`scripts/annotate.py`), read from pixels and on-screen text only, pipeline
output never consulted — and the GTA window was labelled **before the pipeline
ever analysed it**, so its numbers are out-of-sample by construction. The set:
**53 spans across three windows and two games** (Grounded 20:00–30:00, 15
spans; GTA 40:00–50:00, 22 spans; the seed's 16). `scripts/analyse_cut.py`
closes the workflow gap — cut → project → analysis through MOMENTS, nothing
rendered — and the dataset tests now parametrise over `datasets/*` so a new
window is covered the day it lands.

**GTA, out-of-sample** (proj-30f5d0b6b6f3, the 40:00–50:00 cut, offset 2400):
events **precision 0.35, recall 0.82** (9 of 11), moments **0.33 / 0.80**
(4 of 5), unknown-event ratio **0.19**, and only 42 s of selected moments over
stretches marked boring. Decomposing the 17 unmatched claims changes the
story: **20 of 26 predictions sit inside or beside a labelled event span** —
the penalty is fragmentation (five combat micro-events across one labelled
65-second firefight; the episodes layer exists to merge exactly this), not
hallucination. Of the 6 truly outside, at least four look like events the
5-second labelling grid under-captured (the 45:34 collision is the Surano
carjack; the 46:09 `escape` is semantically right). What it actually missed:
the burning wreck at 47:35 (fire still has no detector) and the four-star
freeway run at 48:25–49:30 (night footage, tiny star row — the wanted-level
fusion never fired).

**Grounded, first measurement ever** (proj-87a4213b248e, full-recording
analysis, offset 0): events **0.42 / 0.83** (5 of 6; missed the second death —
"died by misadventure" at 26:36), moments **0.60 / 0.75**, but **139 s of
selected moments overlap stretches marked boring**, and 5 of the 8 unmatched
event claims sit inside those stretches. The failure modes are opposite by
genre: GTA over-fragments real action; Grounded invents action where there is
none (combat claims during foraging, `outplay` during base-building). The
next precision work is genre-aware: episode-level scoring for dense footage,
and stricter audio-only nomination on quiet footage.

Labelling honesty, recorded in the dataset descriptions themselves: labelled
by Claude from 5-second contact sheets; the audio was never heard, so
sound-only events are systematically under-labelled; short events between
tiles are inferred from aftermath and say so in their notes.

### Product phase 2, in parallel — and the package proven (2026-08-27)

Three agents worked disjoint files at once; everything below is theirs plus the
wiring, merged and green in one pass (1,602 unit tests).

**The distribution was tried like a user.** sha256 verified, unzipped in 4 s to
a fresh location, launched from there: server up, prebuilt UI served, bundled
ffmpeg 9.0 detected, `publishing/targets` honest on a fresh install, a project
created, and a **fresh database inside the package tree with the dev repo
untouched**. The one substituted step — dependency install — was substituted
because it writes gigabytes into a C: Python, which this machine forbids; the
junction stated it plainly.

**Timeline editing UX** (agent A): per-clip trim ±1 s on either edge, split at
midpoint, move, delete/restore with §78 styling — over the §42 operations API
exactly as it is, backend refusals shown verbatim on the clip row that asked.
Undo deliberately skipped: no revert endpoint exists, and inventing one from
the UI is how contracts rot.

**Upload metadata** (agent B): `POST /projects/{id}/metadata/suggest` builds a
`VideoMetadata` from evidence only — episodes (not duplicate reports) for the
description, STORY clips for chapters (first at 0.0 by construction), bilingual
templates keyed by the transcript's script, tags under YouTube's 500-char
budget by dropping least-frequent-first, thumbnail frame at the best moment's
peak into `assets/`. Export screen gained "Suggest from the analysis"; the
publish payload now carries description, tags and thumbnail.

**Profile authoring** (agent C): `docs/PROFILES.md` — every claim cited to a
measured incident in this repo — and `scripts/profile_report.py`, the
signature miner run twice by hand this month, now a tool: top OCR strings,
label counts, candidates filtered of chrome, and a paste-ready escaped-regex
snippet. Verified against the real GTA project; the misspellings surface
individually, exactly where the guide says to collapse them.

**Precision, measured at the new density on the golden set** (the eval cut
re-created from the original recording — its temp source had been cleaned):
events recall **0.86 → 1.00** (the missed fire at 35:50 is now found) and
`unknown_event_ratio` 0.81 → **0.33**; measured precision moved 0.38 → 0.26 as
claims rose 16 → 27. Against a 16-span seed that is the documented lower-bound
effect, not a verdict — §118's own caveat. The real precision work needs more
labelled spans first, and that is now the top of Phase 2's remainder.

### Product phase 1 — the loop closes (2026-08-20)

The product-strategy report named three product-killing gaps; all three are
shut, each through a seam that already existed.

**YouTube upload.** The publishing seam promised a destination would cost one
publisher class; it did. Device-flow OAuth (a code at google.com/device, one
token request per UI poll, the refresh token in one owner-only file under the
data root), resumable chunked upload where **the server owns the offset**,
quota and auth errors that carry their remedy, and thumbnail/playlist as notes
on success rather than failure modes. Publishing is a job: the person's
instruction rides in the payload verbatim, QA's blocking verdict is honoured
(§76), and the job history is the publication history (§81). One graph edit —
`PUBLISH` now depends on QA, not EXPORT: the two are parallel deliveries, not
a chain. 39 tests, no network anywhere in them.

**Shorts.** A Short is a tiny edit run through the same stack at 9:16: NVENC
cut of a top moment, centre-cropped (the HUD corners going away is a feature
at 606 px wide), captions built by the long-form engine on a one-clip
timeline, the same Remotion overlay at 1080×1920, audio kept. `SHORTS` is a
third manual delivery beside EXPORT and PUBLISH — §51 is a test: running the
pipeline never cuts them unasked. Proved against a real encoder: a plan
becomes a 1080×1920 file of the planned length with audio in 2.9 s.

**Distribution.** `scripts/package.py` builds `dist/VAI-<version>/` (+ zip,
**296 MB**, sha256 beside it) around the self-repairing launcher that already
existed: bundled ffmpeg and node, prebuilt web UI, INSTALL.txt in Arabic and
English. What never ships is enforced twice — excludes by name, then a guard
that kills the build if `.credentials` or the builder's database leak through.
Verified: the packaged tree boots and wires all 19 stages. The honest limit is
stated in the note: Python 3.11 once, from python.org; everything after is
automatic.

### The certification run — everything at once, on a real recording (2026-08-20)

The complete VAI 2.0 chain, live in one pass on *Ziad 2*: Director consulted,
Critic reviewing with apply on, segmented overlay rendering, contention
reported, QA judging the file. The point of running it was to find what only
the whole chain can show, and it did, immediately.

**Find: a stage inserted mid-graph strands every project that predates it.**
CRITIQUE arrived between EDL and RENDER; the real projects have no row for it;
RENDER refused to start — *"critique incomplete"*, forever, with nothing on the
screen to run. Fixed where §47 already makes state true: startup recovery now
backfills rows for stages the graph gained after a project ran. The status is
the decision — a **finished** project gets the row as completed-with-`skipped`
(the truthful record that the stage did not exist; queueing it would have an
upgrade silently re-editing a finished video, which §78 forbids), a project
still before its render gets it **queued**, and one that never reached the gap
gets nothing.

**The run itself, end to end (11.8 minutes):**

| stage | took | what happened |
| --- | --- | --- |
| moments | 0.1 s | 29 moments from the re-analysed events |
| story | 10.3 s | Director consulted, **rejected** — it named a moment outside the list; the guardrail held, the plan says so in its notes |
| edl | 0.0 s | 13 clips, 45 captions, 10 effects |
| critique | 19.5 s | live 7B: 9 trims applied (0.75 s floor and §39 respected), 0 refused, verdict recorded |
| render | 672.6 s | 1080p60 h264+aac, **overlay as 22 stretches covering 3,423 of 17,784 frames** — 81% of the Chromium pass gone on a real render |
| qa | 45.8 s | 3 warnings for the human, no failures |

Output: `rnd-e676b5c63198.mp4`, 592.8 s against a 600 s request (inside
tolerance *after* the Critic's trims), 1.14 GiB. The card ended at 7,295 MB
free with nothing resident — every model released itself.

Honest notes on model quality, distinct from correctness: the Critic's nine
trims were near-uniform ("1.0 s off the start", same reason) — a 7B
pattern-matching more than judging. Every one passed the floor, the cap and
the duration veto, which is the design working: mediocre taste cannot become a
broken video. And the Director's rejection is the index guardrail earning its
place a second time on real footage.

### Who else is on this machine (shared infrastructure 5.4, 2026-08-20)

Inspected with permission, read-only. Three programs on this machine want the
same 8 GB card, and only one of them is this project.

| program | models | runtime | endpoint |
| --- | --- | --- | --- |
| **VAI** | `qwen2.5:7b-instruct`, `qwen2.5vl:7b`, whisper `large-v3-turbo` | Ollama + faster-whisper | `127.0.0.1:11434` |
| **Agent factory** (OpenHands, third-party, in Docker) | `qwen2.5-coder:7b`, and Gemini in the cloud | Ollama | `host.docker.internal:11434` — **the same daemon** |
| **nav** | nomic embeddings, whisper | torch / llama.cpp | its own process |

Two findings worth the look.

**The mystery from 2026-08-15 is solved.** `qwen2.5-coder:7b`, found resident
with an expiry in the year 2318 while a render waited, is OpenHands' configured
local profile. It reaches the same Ollama daemon from inside Docker, so VAI's
`release_everything_we_loaded` — which asks by name for the three tags VAI
configures — correctly leaves it alone.

**And that is the right behaviour, so 5.4 is a reporting problem, not a
releasing one.** Another program's model may be mid-request, and taking its
memory is exactly the discourtesy this project asks not to receive. What was
missing is that nothing *named* the holder until Chromium failed to start
twenty minutes into a render. `gpu.contention()` now answers "what is on the
card and how much of it is not ours" in one sentence, and the render worker
says it before the twenty minutes rather than after.

`nav` uses a different runtime entirely — torch and llama.cpp hold VRAM that
Ollama's API cannot see or release, which is why an earlier diagnosis found the
card full with `resident_models()` empty.

**5.3 (capability-based model selection) is not being built.** The shared store
holds exactly the tags VAI names, `qwen3-coder:latest` is 18.6 GB and cannot
fit this card at all, and a matcher for a shortage nobody has is speculation.
The measurement is the deliverable.

### Phases A and B — measured first, then built (2026-08-20)

Phase 0 deferred both and gave a condition: *"Relations between events are
worth building once the events have names."* They have names now, so the
question was asked of the data before anything was designed.

**What 255 named events on three real recordings say.** Consecutive named
events sit a median 12 seconds apart, and half to two-thirds of neighbouring
pairs fall within fifteen. The commonest neighbour by a wide margin is the same
type again — `low_health → low_health` nineteen times, `combat → combat`
eighteen, `collision → collision` eighteen.

And the load-bearing negative result: **the gap distribution for same-type
pairs is indistinguishable from different-type pairs** — median 12.0 against
11.4, first quartile 8.0 for both. Time cannot tell "this fight is still going"
from "something else happened nearby", so an episode is not a time-window
merge. Type identity is the signal; the window only stops a run reaching across
the recording.

**Phase B — `backend/gaming/episodes.py`.** A run of one named type, each
within 20 seconds of the last, is one episode. The window was chosen off the
curve rather than picked: 23% of named events absorbed at ten seconds, 33% at
fifteen, 38% at twenty, 44% at thirty, and then it flattens. Twenty sits at the
knee. Different types close together are **linked, not merged** — a `combat`
and a `low_health` ten seconds apart are two true things about one situation,
and which was which is what an editor needs.

Read against the three recordings: 255 named events become **159 episodes**,
37–38% absorbed on every one of them, with 77 links. The commonest links are
`low_health ↔ combat` (hurt in a fight) and `collision ↔ combat` (GTA).

**Phase A — `backend/evidence/`.** No table, no writer, no migration, exactly as
Phase 0 required. What it replaces is four hand-rolled answers to one question:
the Critic gathered per clip, the perception report per event, and two
throwaway scripts per instant, each with its own idea of "near" and its own
silent failure when an observation could not be attributed to a recording.

**First consumer: the Critic's evidence rows**, which now read `combat x2`
where they used to list `combat` twice. A model told that three things happened
writes a review of an edit that does not exist.

### `maxLength` is not enforced, and the narration fix was the wrong one (2026-08-20)

Verified against the real model rather than assumed, and the assumption was
wrong. Same 77-minute Arabic transcript, fifteen windows, both schemas back to
back:

| | windows read | lost |
| --- | --- | --- |
| unbounded (before the fix) | 12/15 | 3 |
| every string bounded | 12/15 | **3** |

Identical. Bounding the strings changed nothing, because **Ollama's structured
output constrains the *shape* of an answer — types, required keys, enums — and
not the length of its strings.** `enum` and `maxItems` are real constraints;
`maxLength` is documentation.

What the three failures actually were, read off the raw responses: all three
end mid-string inside `quote`, with the model looping on repeated characters
in Arabic and never closing it. `done_reason: null`, `eval_count: 0`, while
every window that succeeded reported `stop` and a real count.

So the field was removed from what the model is asked for. Measured on exactly
those three windows: truncated → parsed, 3/3. Then end to end on the whole
transcript: **0 windows lost, down from 3 of 15**, in 80 seconds, with the
model unloaded afterwards by itself.

Nothing is lost with it. `Incident.quote` stays on the type for its readers,
and the player's words are recoverable where they always were — every incident
carries the span they were said in, and the transcript is stored.

The bounds added across the other five prompts stay. They cost nothing, they
document the intent, and `maxItems` and `enum` among them are enforced. But the
Critic's 1-in-3 → 4-in-4 improvement should be credited to dropping `keep` from
its action enum and to asking for fewer notes, not to `maxLength`.

### Six prompts were letting the model write without a limit (2026-08-19)

Chasing one logged line — *"Could not read a transcript window"*, repeated
through the 77-minute re-analysis — found the same defect the Critic had, in
five more places.

Ollama compiles a prompt's output schema into the grammar it decodes with, so
a string with no `maxLength` is an invitation to fill the output budget; when
the budget runs out mid-string the JSON never closes and the whole answer is
lost. Every shipped prompt is now bounded — `vision.frame_description`,
`critique.edit_review`, `narrative.blueprint` and the three `interaction.*` —
and one test checks all of them at once, because checking one prompt at a time
is how this was found one prompt at a time.

**Narration lost the most and said the least.** `_read_window` catches its own
failure, logs a line, and returns nothing, so a failed window means the
recording is analysed as though the player said nothing for six minutes.

And it had a second copy of its schema in Python that had silently drifted from
the file: the file constrained times to be non-negative and importance to 0–1,
and the copy actually being sent constrained neither, and neither had the
bounds that mattered. The duplicate is gone — the schema that runs is now the
one in `prompts/analysis/narration/`. The Critic and the Director keep their
Python schemas, because those derive their enums from the types they validate
into, and a test now holds each equal to its file.

### The GTA profile, and what it could not fix (2026-08-19)

`profiles/gta_v/profile.json` had everything except a way to be found. Seven
measured HUD regions, a wanted-star glyph row with `chase`/`escape` change
rules, region-scoped `WASTED`/`BUSTED` — and **zero signature patterns**, so
game detection, which matches on signatures, could never identify it. Added:
ten signatures, three fusion rules, and the Director Mode chrome the recording
actually shows.

The signatures are OCR-tolerant on purpose. `DIRECTOR MODE` came back as
`DIFIECTOR MODE`, `DINECTOH MODE`, `DIPECTOR MODE` and `DRECTOR MODE` across
161 readings of the same two words, so a pattern matching only the correct
spelling matches a minority of its own evidence.

Detection now separates the two shipped games cleanly: the GTA recording scores
6 signature hits with 0 for the runner-up, the Grounded recording 12 with 0.

**And it did not move the number.** `unknown_event_ratio` stayed at 0.430,
because the events it cannot name are not events. Of 84 unnamed, **71 carry
`scene` + `vision` and no audio at all** — a shot boundary plus a frame
description reading *"the player is driving a police car through a
cityscape"*. Nothing happened. The recording is Director Mode scene-building
with invincibility on: no deaths, no wanted level, no missions, and the
profile's death and chase rules have nothing to fire on.

So Phase 0's criteria 3 and 4 (`death` from `WASTED`/`BUSTED`, `chase`/`escape`
from the wanted-level HUD) are **written and untestable against this footage**
rather than met. They need a recording of someone playing GTA rather than
building sets in it.

The honest state of the gate, then: criterion 1 passes on one of three real
recordings, and the reason the other two miss is different in each case —
Ziad 2 by 0.008 with every remaining event genuinely ambiguous, and تجريب 4
because most of what its detectors nominated was a camera cut.

### All three real recordings re-analysed — the gate is a profile problem (2026-08-19)

4.2 hours of VLM across the two remaining projects. Coverage is now solved
everywhere: the median gap from an unnamed event to the nearest analysed frame
is **0.0–0.2 s**, and **94–96%** of unnamed events have a look within two
seconds. Nothing is unwatched any more.

| project | game | profile | before | **after** | gate ≤0.35 |
| --- | --- | --- | --- | --- | --- |
| سبي | Grounded | `grounded` | 0.447 | **0.227** | ✅ |
| Ziad 2 | Grounded | `grounded` | 0.609 | **0.358** | ✗ by 0.008 |
| تجريب 4 | GTA V | **`generic`** | 0.503 | **0.430** | ✗ |

**The remaining spread is the profile, not the footage and not the looking.**
The two Grounded projects use a profile with 12 signature patterns and 2 fusion
rules, and its `creature_fight` rule alone named 52 events on سبي. تجريب 4 fell
back to `generic` — and of its 74 unnamed-but-observed events, **62 show
`driving`**, which no generic rule can name.

It fell back because `profiles/gta_v/profile.json` has **zero signature
patterns** and **zero fusion rules**. Game detection matches on signatures, so
it can never identify GTA, and even when told it would add nothing to
correlation. The recording is unmistakable: the most-read on-screen text is
`DIRECTOR MODE` (161), `STUNT RAMPS`, `INVINCIBILITY`, `ACTING UP`.

Phase 0's own gate anticipated this — criteria 3 and 4 ask for `death` from
`WASTED`/`BUSTED` and for `chase`/`escape` from the wanted-level HUD, both of
which are GTA profile rules that do not exist. Building that profile is the
next concrete step, and §23's bargain is intact either way: a profile improves
accuracy and is never required.

Criterion 5 (≥20% of named events carrying ≥3 sources) passes on all three at
0.74–0.92.

### The gate, re-measured at the new density (2026-08-19)

*Ziad 2* re-analysed at 720 frames per source hour. 38.6 minutes of VLM for a
41-minute recording — roughly real time — plus 6 minutes of correlation.

| | before | after |
| --- | --- | --- |
| vision observations | 165 | **428** |
| candidate regions analysed | 42 of 107 | **107 of 107** |
| regions dropped for budget | 65 | **0** |
| coverage | 0.96 | **1.00** |
| events | 46 | 53 |
| named | 18 | **34** |
| **`unknown_event_ratio`** | **0.609** | **0.358** |
| median gap, unnamed event → nearest look | 12.2 s | **0.2 s** |
| unnamed events with a look within 2 s | 25% | **95%** |

The diagnosis holds exactly. Coverage was the whole problem, and the gap it
left collapsed from twelve seconds to two tenths.

**The gate is missed by 0.008.** `unknown_event_ratio` 0.358 against ≤ 0.35 —
one event short of 0.35, and that is where it is being left rather than closed
by inventing a rule. The 18 events still unnamed were *all* looked at: their
frames report `inventory` (16), `exploration` (6), `combat` (3), `drinking`
(2), `driving` (1), and each carries audio, a scene boundary and a vision
observation. Something happened while the player was exploring, and nothing on
screen said what. `unexpected_event` is the honest answer for those, which is
what §23 says it is for — and more looking cannot help them, because looking
is no longer what they lack.

Criterion 5 (≥20% of named events carrying ≥3 sources) passes at 0.74. The
Grounded profile is live and doing the work: game detection identified it from
the OCR text with no configuration, and its `creature_fight` fusion rule named
**17 of the 34**.

Not re-analysed: the other nine projects, at roughly one minute of VLM per
minute of source (Ziad 4 ≈ 2.2 h, سبي ≈ 2 h).

### Raising the vision budget, and what it uncovered (2026-08-19)

`max_frames_per_source_hour: 240` is four looks per source minute — one every
fifteen seconds — and it was **fully spent on every real project**:
`frames_planned` equalled `frame_budget` every time, and **57–71% of the
regions the cheap detectors nominated received no frame at all**.

| project | regions | analysed | dropped | planned = budget |
| --- | --- | --- | --- | --- |
| Ziad 4 | 224 | 97 | **127 (57%)** | 385 |
| Ziad 2 | 107 | 42 | **65 (61%)** | 165 |
| سبي | 242 | 77 | **165 (68%)** | 308 |
| — | 230 | 68 | **162 (70%)** | 270 |
| — | 235 | 68 | **167 (71%)** | 270 |

Those unwatched regions are where the unnamed events live. Raised to **720**,
which is what the densest real recording needs to give every nominated region
its four frames (242 regions in 84 minutes = 688/hour). It is a ceiling, not a
target: a quiet recording nominates fewer regions and costs proportionally
less.

**Two bugs surfaced immediately, and one of them meant vision was broken on
this machine right now.**

`OllamaVisionProvider` never sent `num_ctx`, so every request ran in Ollama's
4,096-token default. One 1080p frame is roughly 1,400 tokens, so a batch of
four plus the prompt measured **5,712** — and current Ollama answers HTTP 400
rather than truncating. Every vision request failed all three attempts in 1.7
seconds, which the pipeline reported as *"the vision model returned no usable
result after 3 attempts"*: a sentence about the model that was never about the
model. `VisionModelConfig` gained `context_tokens: 8192` and the provider now
sends it. Verified: a four-frame batch describes in 27.9 s.

`OllamaLLMProvider` had the same defect and had simply not fired — its
`context_tokens: 32768` has been configured since Phase 13 and never sent, so
every LLM call has run in 4,096 as well. Text prompts fit; a forty-clip edit
review would not have.

### Phase E against the real model (2026-08-19)

Run on *Ziad 2* and *Ziad 4* with `qwen2.5:7b-instruct` from the shared
`F:\Models` store. Four defects, all found by running it and none visible to a
scripted provider.

**§54's unload did nothing.** `OllamaLLMProvider.unload()` returned early
unless `load()` had been called — and no caller in this pipeline calls it,
because Ollama loads a model to answer whether or not it is asked to.
Measured: the Critic answered in 23 s, called `unload`, and left
`qwen2.5:7b-instruct` resident with **4,528 MB of an 8 GB card** held, through
the render that was about to start. It is now unconditional and never raises;
verified on the card at 2,623 MB → 7,328 MB, resident list empty. The existing
test passed throughout because it called `load()` first.

**The model described actions instead of taking them.** Prompt v1 asked for one
note per clip; the model answered `keep` eleven times with reasons reading
"remove the first 2 seconds". Zero changes reached the edit. v2 asks only for
clips that need work and the grammar no longer offers `keep` — a clip that is
fine is one the review omits.

**Unbounded strings truncated the JSON.** Ollama compiles the output schema
into the grammar it decodes with, so a string with no `maxLength` is an
invitation to fill the output budget; when it runs out mid-string the JSON
never closes. Measured: **two runs in three lost all three attempts**, each
taking 59 s. With every string and array bounded to match the Pydantic model:
**four runs in four, in 7–13 s** — an 18× speed-up as a side effect of
correctness. The Director's schema had the same latent defect and is fixed with
it.

**Sub-second trims.** The model returned 0.23 s, 0.48 s and 0.59 s alongside
two real trims — a required field being filled, not a judgement. `§29` snaps
cut points and `§41` moves them at that scale for reasons made of evidence, so
a floor of 0.75 s demotes them to "no change". The model also drifted into
Chinese on one verdict; v4 anchors the output language, as the Director's
prompt already did.

What it then produced on *Ziad 4*, unedited: five clips called "entirely a
menu", of which §39 let three be dropped and **refused two** because they would
have taken the edit under 9:15. That is the §77 `accidental_menu_section`
defect caught before the render rather than after it.

### Phase 0's acceptance gate — measured, and **failed** (2026-08-19)

Measured across all ten projects on this machine, from stored analysis, no
models involved:

| | measured | gate |
| --- | --- | --- |
| `unknown_event_ratio` | 0.42 – 0.93 (all events: **0.63**) | ≤ 0.35 |
| named events with ≥3 sources | 0.60 – 1.00 | ≥ 0.20 ✅ |
| `vision_frames_per_source_minute` | **4.0 on every project** | — |

The cause is not the naming rules. For every project, the median gap from an
event to the nearest frame anybody looked at:

| | named events | unnamed events |
| --- | --- | --- |
| median gap to nearest look | **0.7 – 3.2 s** | **2.6 – 15.0 s** |

Of 389 events nobody could name, **285 (73%) had no frame within two seconds
of them**. Naming rules cannot name what nobody saw. Four observations per
source minute is one look every fifteen seconds.

The remaining 104 were looked at, and what those frames reported was
`inventory` 45, `menu` 28, `loading` 2 against `driving` 31, `combat` 16,
`exploration` 9 — most of them are not naming failures at all, they are the
scene detector and the audio detector firing on an interface. **Fixed:**
correlation now drops an *unnamed* event whose instant the vision pass says
was a menu, a loading screen or a pause. Named events keep their names
wherever they were read (`defeat` is read off a defeat screen) and `HUD_ONLY`
is not a screen (`inventory` over a firefight is a health bar). Replayed over
the stored events: 69 dropped, `unknown_event_ratio` **0.672 → 0.627**.

Which leaves the gate 28 points short, and the remaining distance is **entirely
a coverage decision**. Getting most events within ~2 s of a look means roughly
a look every 4 seconds instead of every 15 — about **3.5–4× the VLM work per
project**. On the 77-minute recording that is ~307 frames today against ~1,200.
That is GPU time per project, and whose call it is is not the code's to make.

**Phase A (evidence projection) and Phase B (the event relationship model)
stay closed until it is made.** Phase 0's own text is the reason: "Relations
between events are worth building once the events have names." A graph over a
vocabulary that is 63% `unexpected_event` would organise the gap rather than
close it.

### Phase E — the Critic ✅ done

The first stage that reads the pipeline's own output. Everything before it
judges the *source*: the scorer read events, the optimiser read durations, the
Director read a list of moments. None of them ever saw the assembled edit,
which is the only object a viewer meets -- so a defect that lives in the
assembly is invisible to all of them by construction.

`CRITIQUE` sits between EDL and RENDER, deliberately: a criticism of a timeline
costs a database write, and the same criticism of a finished MP4 costs a
re-render. It reads the edit clip by clip from analysis that already exists --
what is on screen, what was said, what events were named, how long it runs,
what is drawn over it -- and asks for one note per clip: `keep`, `trim_start`,
`trim_end` or `drop`.

What makes it safe is what it is *not* allowed to do. A clip number outside the
list it was shown is a rejection, not a nearest-neighbour repair. §42's
operations are the only vocabulary, so nothing can be asked for that the
timeline would not already do. A trim longer than half a clip is capped. And
§39 keeps its veto: a review may improve a video, never shorten it out of the
length that was asked for -- with a second regime for an edit that never
reached the target, where the floor has already been missed and what is
protected instead is the proportion.

`critique.apply: false` produces the same review and changes nothing, which is
§78's "the human has the last word" as a setting rather than a slogan.

Measured on *Ziad 2* before any model was consulted: the evidence rows are
built from stored analysis alone, and one of eleven clips -- thirty seconds of
the finished video -- came back with **no observations, no words and no
events**. The row says so in as many words, because "nothing is recorded here"
and "nothing happens here" are different statements and only one of them is a
reason to cut.

**Rule that governs the order (§126):** do not build a large UI before the
pipeline produces a convincing video. If `gameplay → analysis → events →
moments → EDL → render` does not work, no interface saves the project.

---

## 3. What exists today

### 3.1 Code inventory

| Package | Contents | State |
| --- | --- | --- |
| `backend/core` | `duration` (the 10–60 min policy), `errors` (typed codes), `logging` (5 channels), `ids`, `fs`, `cache_keys`, `versions`, `models/` | complete |
| `backend/config` | `schema.py` (every YAML typed), `loader.py` (merge + env overrides), `paths.py` (§43 layout) | complete |
| `backend/database` | `connection.py` (WAL, FK on), `migrator.py`, `migrations/0001_initial.sql`, `repositories/` | complete |
| `backend/services` | `project_manager`, `media_ingestion`, `job_manager`, `health`, `worker` | complete |
| `backend/interaction` | `models`, `intent`, `parser`, `knowledge`, `qa`, `store`, `service`, `llm_fallback` | complete |
| `backend/effects` | `models`, `library`, `planner` | complete (renderers pending Phases 9–10) |
| `backend/publishing` | `base` (Publisher protocol + registry), `local_file` | local target complete |
| `backend/api` | `app`, `dependencies`, `routers/` × 7 (health, projects, media, jobs, interaction, editing, files) | complete for current scope |
| `ai/providers` | `base.py` — Speech / Vision / LLM protocols, registry | interfaces only |
| `ai/llm` | `ollama_provider` (§93, §94), `fake_provider`, factory | complete |
| `backend/media` | `ffmpeg` (process layer), `probe`, `proxy`, `audio`, `frames`, `chunking` | complete |
| `backend/pipeline` | `runner`, `workers/` for **every stage the runner queues** | complete through QA |
| `backend/analysis` | `signal`, `audio_events` (§18), `reactions` (§19, §20), `scenes` (§17), `candidates` (the §15/§16 cascade) | complete |
| `ai/speech`, `ai/vision`, `ai/ocr` | real provider + deterministic fake + factory each | complete |
| `backend/gaming` | `profiles` (§22, §23), `ocr` (§25), `events` (§21), `correlation` (§27), `hud` (§24) | complete |
| `prompts/` | §92 versioned prompts, loader in `backend/core/prompts.py` | vision + the three interaction prompts |
| `backend/moments` | `formation` (§28), `context` (§29), `dead_time` (§30), `repetition` (§31, §33), `scoring` (§32) | complete |
| `backend/narrative` | `optimizer` (§39), `story` (§35, §36), `hook` (§37), `pacing` (§38) | complete |
| `backend/timeline` | `models`, `builder`, `operations`, `validation` (§40-§42), `captions` (§71) | complete |
| `backend/rendering` | `composition`, `remotion`, `encoder`, `audio_mix`, `ffmpeg_renderer`, `composite` | complete |
| `remotion/` | `OverlayLayer`, captions, 7 overlay effects | complete |
| `backend/qa` | `report` (§76-§79), `technical` (§76), `content` (§77) | complete |

### 3.2 Configuration (13 files in `config/`)

`application` · `output` · `analysis` · `models` · `moments` · `narrative` ·
`effects` · `rendering` · `audio` · `captions` · `qa` · `interaction` ·
`publishing`

All typed with `extra="forbid"`, so a typo is a startup error. Override any
value with `VAI__SECTION__KEY=...`.

### 3.3 Database (`0001_initial.sql`)

§45 core entities — `projects`, `media`, `media_tracks`, `analysis_jobs`,
`scenes`, `frames`, `audio_events`, `transcript_segments`, `game_events`,
`moments`, `timeline_clips`, `timeline_effects`, `captions`, `music`,
`renders`, `qa_results` — plus `analysis_cache`, `video_metadata`,
`publications`, `editing_intents`, `editing_intent_updates`,
`conversation_messages`, `edit_versions`.

**Every table exists already.** Later phases fill them; none should need a
schema change beyond adding a migration for genuinely new concepts.

### 3.4 API (25 endpoints, all under `/api`)

```
GET    /health                             GET    /capabilities
GET    /presets

POST   /projects                           GET    /projects
GET    /projects/{id}                      PATCH  /projects/{id}
DELETE /projects/{id}                      GET    /projects/{id}/status
POST   /projects/{id}/analyze              POST   /projects/{id}/cancel

POST   /projects/{id}/media                GET    /projects/{id}/media
GET    /projects/{id}/media/{media_id}     DELETE /projects/{id}/media/{media_id}

GET    /projects/{id}/jobs                 GET    /jobs/{job_id}
POST   /projects/{id}/reanalyze

POST   /projects/{id}/chat                 POST   /projects/{id}/ask
GET    /projects/{id}/intent               POST   /projects/{id}/intent/preset
POST   /projects/{id}/intent/reset         GET    /projects/{id}/chat/history
GET    /projects/{id}/edit-versions
```

All of these now exist: `generate-edit`, `timeline` and `timeline/operations`,
`render`, `render-status`, `qa`, `events`, `moments`, plus `preview` and
`files/{category}/{filename}` for the browser. Still to add:
`POST /projects/{id}/publish` (§89), which waits for a real destination.

### 3.5 Tests (414)

| File | Count | Covers |
| --- | --- | --- |
| `unit/test_interaction.py` | 77 | intent, parsing, Q&A, commands, versions |
| `unit/test_duration.py` | 54 | the 10–60 min band, adversarially |
| `unit/test_core.py` | 50 | errors, logging, ids, fs, cache keys |
| `unit/test_jobs.py` | 34 | stage graph, lifecycle, cancel, resume |
| `unit/test_config.py` | 31 | loading, overrides, semantic validation |
| `unit/test_effects.py` | 30 | library, planning, budgets, engine split |
| `unit/test_project_manager.py` | 27 | CRUD, §43 tree, re-edit invalidation |
| `integration/test_api.py` | 27 | endpoints + **Phase 1 acceptance** |
| `unit/test_database.py` | 24 | migrations, constraints, transactions |
| `unit/test_media_ingestion.py` | 23 | validation, checksum, dedup |
| `unit/test_publishing.py` | 21 | registry, local target, metadata limits |
| `integration/test_interaction_api.py` | 16 | chat, intent, presets, history |

---

## 4. Contracts that must not break

Any change that violates one of these is an architecture change, not a
refactor. Full rationale in [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

| # | Contract |
| --- | --- |
| C-1 | **AI decides, renderers execute.** The timeline never names an engine. |
| C-2 | **The model executes nothing.** LLM output is validated data that ordinary code applies. No shell, no file writes, no FFmpeg strings from a model. |
| C-3 | **Sources are immutable.** Written once at import, read forever after. |
| C-4 | **Analysis is chunked.** No stage loads a whole recording into RAM. |
| C-5 | **Re-editing never re-analyses.** Only `STORY → EDL → RENDER → QA` re-run. |
| C-6 | **Cache identity is exact.** `video_hash + model_version + prompt_version + analysis_version`, built by one function. |
| C-7 | **Answers are grounded.** Q&A cites stored records; before the data exists it refuses. |
| C-8 | **One model in VRAM at a time.** Load → process → unload → empty cache. |
| C-9 | **Nothing leaves the machine on its own.** Publishing is always explicit. |
| C-10 | **The chat is optional.** No instructions → default preset → finished video. |

Where each is currently enforced:

- C-5 — `backend/core/models/jobs.py` (`ANALYSIS_STAGES` / `EDIT_STAGES`), tested
- C-6 — `backend/core/cache_keys.py`, tested
- C-9 — `MANUAL_STAGES` excludes `EXPORT`/`PUBLISH`, tested
- C-10 — `IntentResolver.resolve()` with zero updates, tested
- C-2, C-3, C-4, C-8 — declared in config and interfaces; **enforced when the
  stages that could violate them are written (Phases 2–10)**

---

## 5. Remaining work, phase by phase

Each phase: goal, files to create, acceptance criterion, and the traps.

### Phase 2 — Media Engine ✅ done

**Goal (§99, §126 steps 05–06):** open the recording, produce everything the
analysis stages read.

**Create**
- `backend/media/ffmpeg.py` — command builder. Explicit `list[str]` argv,
  `shell=False`, timeout, stderr captured into typed errors.
- `backend/media/probe.py` — ffprobe → `MediaMetadata` + `MediaTrack` rows.
- `backend/media/proxy.py` — 720p proxy per `config/rendering.yaml:proxy`.
- `backend/media/audio.py` — 16 kHz mono WAV analysis stream.
- `backend/media/frames.py` — hierarchical sampling per `analysis.frame_sampling`.
- `backend/media/chunking.py` — chunk iterator with overlap (§7).
- `backend/pipeline/workers/` — `PROBE`, `PROXY`, `AUDIO`, `FRAMES` stage workers.
- `backend/pipeline/runner.py` — claim job → run → report → advance.

**Acceptance: passed.** Measured rather than asserted — the same pipeline over
a source 150× longer moved peak memory by **+0.2 MB** (39.5 → 39.6 MB).
`tests/integration/test_long_source.py`, run on Windows 11 with FFmpeg 9.0.

**Traps — all three hit, all three handled**
- The frame ceiling **widens the interval**; truncating the list would sample
  the first forty minutes of a two-hour recording and abandon the rest.
- fps is parsed as a rational. `0/0` means unknown and returns `None`, not zero.
- The proxy is built in resumable segments, then concatenated **and its
  duration verified** — a proxy short by one segment still plays, and every
  timestamp after the gap would point at the wrong part of the recording.

**Also delivered:** `JobManager.requeue` (§90 re-analysis had no way to return a
finished stage to the queue), `MediaTrackRepository`, `FrameRepository`.

Full write-up: [`docs/PHASE_2.md`](PHASE_2.md).

---

### Phase 3 — Speech and Audio ✅ done

**Goal (§14, §18, §19, §20):** transcript with word timestamps, audio events,
reaction candidates.

**Create**
- `ai/speech/faster_whisper_provider.py` implementing `SpeechProvider`.
- `ai/speech/fake_provider.py` — deterministic, for tests without the model.
- `backend/analysis/audio_events.py` — RMS/peak/LUFS, silence, spikes, transients.
- `backend/analysis/reactions.py` — correlate microphone audio with gameplay.
- `backend/pipeline/workers/transcript.py`, `audio_events.py`.

**Acceptance: passed.** Both halves executed against real files. Chunked
transcription verified with a chunk size small enough that a six-second clip
produces several chunks — offsets land on the source timeline and no utterance
is stored twice across an overlap. Microphone independence verified on a
recording carrying a real laugh and a real scream on its second audio track,
each correlated with the gameplay impact that preceded it.

**Traps — all three hit, all three handled**
- `start_offset` is applied per chunk, and a segment belongs to the chunk whose
  core contains its midpoint, so an overlap is never counted twice.
- VAD stays on. Verified against the real model: 30 s of silence returns
  nothing, where an unfiltered Whisper invents captions.
- The model loads once per stage and is released; asserted, because the
  regression is invisible otherwise.

**Also found:** every media file was writing `analysis.wav` into the same
directory, so a second gameplay file silently overwrote the first's analysis
audio. Audio is now namespaced per media, as frames already were.

Full write-up: [`docs/PHASE_3.md`](PHASE_3.md).

---

### Phase 4 — Vision ✅ done

**Goal (§15, §16, §17):** scene boundaries and frame understanding, cheaply.

**Create**
- `backend/analysis/scenes.py` — PySceneDetect wrapper.
- `backend/analysis/candidates.py` — the cascade: audio spikes + frame
  difference + transcript + scene/HUD changes → candidate regions.
- `ai/vision/ollama_provider.py` implementing `VisionProvider`.
- `backend/pipeline/workers/scenes.py`, `vision.py`.

**Acceptance: passed.** Scene boundaries asserted at their known times on a
three-shot fixture, and the frame count counted **at the provider** — the number
of frames reaching a vision model is the number it was handed. Verified at every
§7 source length, 30 min through 8 h, under a relentless stream of nominations:
`frames_planned` never exceeds `max_frames_per_source_hour × hours`.

**Traps — both hit, both handled**
- The cascade is the design. Unbounded merging nearly defeated it: on a
  recording with something loud every 30 s, every nomination merged into one
  region spanning two hours, which then received four keyframes while reporting
  100 % coverage. Region size is now bounded by the sampling config.
- Scene boundaries are stored as supporting information with a **measured**
  change score, never as edit points.

**Also delivered:** §92's prompt architecture (`prompts/`, versioned, loader
refuses a version that disagrees with the registry), migration 0002, and a test
asserting `SCHEMA_VERSION` tracks the migrations — which `versions.py` claimed
but nothing enforced.

Full write-up: [`docs/PHASE_4.md`](PHASE_4.md).

---

### Phase 5 — Gaming Intelligence ✅ done

**Goal (§21–§27):** the product differentiator.

**Create**
- `backend/gaming/ocr.py` — region-restricted, PaddleOCR by default.
- `backend/gaming/hud.py` — confidence-based field detection.
- `backend/gaming/events.py` — event detection from all sources.
- `backend/gaming/correlation.py` — merge agreeing detectors (§27).
- `backend/gaming/profiles.py` — profile loader; `profiles/generic/` first.
- `backend/pipeline/workers/ocr.py`, `game_events.py`.

**Acceptance: passed.** The same clip run through the whole pipeline twice:
once as `game: auto`, producing timestamped events recorded against the generic
profile; once against a profile declaring two regions and one rule, which
switches OCR to region mode and reads wording the generic path correctly
declines to claim. An unknown game falls back with `profile_exact: false` — the
substitution recorded, not hidden.

**Traps — all three hit, all three handled**
- Still exactly one profile directory, and it is `generic/` (§111). Validating
  a real one needs real gameplay footage, not a colour bar.
- Every OCR row has a `NOT NULL` timestamp, because text without a time cannot
  become an event.
- Correlation merges: §27's own example — kill feed + weapon sound + "NO WAY"
  — produces **one** event, typed by the only source that could know, with
  confidence higher than any single detector had.

**Also found:** GAME_EVENTS and MOMENTS were queued by nothing, so the pipeline
could not reach event detection at all; and `doctor.py` reported a broken
PaddleOCR as available because it only checked that the package existed.

Full write-up: [`docs/PHASE_5.md`](PHASE_5.md).

---

### Phase 6 — Moments ✅ done

**Goal (§28–§34):** ranked moments with defensible scores.

**Create**
- `backend/moments/formation.py` — group correlated events.
- `backend/moments/context.py` — adaptive pre/post roll, snap to boundaries.
- `backend/moments/scoring.py` — the ten dimensions of §32.
- `backend/moments/dead_time.py`, `repetition.py`, `variety.py`.
- `ai/llm/ollama_provider.py` implementing `LLMProvider`.
- `backend/pipeline/workers/moments.py`.

**Acceptance: passed.** Ranked moments through the whole pipeline on real
files, every one carrying all ten §32 dimensions plus the penalties and the
multiplier stored separately, and an explanation in sentences that says
something about the evidence rather than repeating the number.

**Traps — all three hit, all three handled**
- §33 shaped the design: variety is a saturation *penalty* fed into the score,
  never a filter, and nothing in this phase selects anything.
- Dead time is scored, and a segment adjacent to a kept moment is protected —
  the walk up to the ambush is what makes the ambush land.
- Scoring is rule-based end to end; a test asserts it works with an empty
  scoring context, i.e. on a machine with no model at all.

**Also found:** the runner could not queue the project-wide stages, so the
pipeline stopped after MOMENTS with STORY unreachable; and the speech-boundary
rule was using the scene snap window, so a clip could still open mid-word.

Full write-up: [`docs/PHASE_6.md`](PHASE_6.md).

---

### Phase 7 — Narrative ✅ done

**Goal (§35–§39):** a coherent video of the requested length.

**Create**
- `backend/narrative/story.py` — all three §35 modes, because they share one
  input and one duration constraint; three files re-deriving "pick moments to
  fill 20 minutes" would be three places for that to drift.
- `backend/narrative/hook.py` — selects an existing moment, invents nothing (§37)
- `backend/narrative/pacing.py`
- `backend/narrative/optimizer.py` — **constrained optimisation, not a sort** (§39)
- `backend/pipeline/workers/story_worker.py`

**Acceptance: passed.** The arithmetic runs against a 200-moment, two-hour-plus
session and lands inside the tolerance with a hook, a climax and at least five
distinct moment types — repeated across **every duration preset §6 offers**, 10
through 60 minutes, from the same source. The stage itself runs end to end on a
decoded recording.

**Traps — both hit, both handled**
- §39 is an optimisation problem. The greedy sort the spec warns about was
  built and measured against the optimiser on the same 150-moment session:
  1192.6 s / 8 distinct types versus 1255.1 s / **15**. Variety had to live
  *inside* the objective — a per-moment bonus cannot express "this is the fourth
  kill in a row", so the search carries the type mix of each partial solution.
- The last-resort duration clamp belongs at the EDL boundary, so this stage
  reports missing the tolerance rather than silently correcting it.

**Also found:** pacing re-sorted chronologically, which made all three §35 modes
produce **identical output** — the user picks "compilation" and gets the story
edit. And a circular import through the repositories package: a domain module
had come to depend on the persistence layer, fixed by moving the type rather
than deferring the import.

Full write-up: [`docs/PHASE_7.md`](PHASE_7.md).

---

### Phase 8 — EDL and Timeline ✅ done

**Goal (§40–§42):** the non-destructive description of the finished video.

**Create**
- `backend/timeline/models.py` — clips, tracks, transitions (engine-neutral)
- `backend/timeline/builder.py` — narrative plan → timeline
- `backend/timeline/operations.py` — split, trim, move, delete, restore
- `backend/timeline/validation.py` — timestamps, bounds, no gaps or overlaps
- `backend/timeline/captions.py` — from transcript timestamps
- `backend/pipeline/workers/edl_worker.py`

**Acceptance: passed.** Every planned clip becomes exactly one timeline clip,
in the plan's order, with its source span unchanged, summing to the planned
duration, contiguous from zero, and validating — checked directly, then run
through the real pipeline on a decoded recording.

**Traps — both handled**
- The §6 clamp is enforced here as a last resort, downward only, trimming the
  tail rather than dropping clips from the middle of the arc, and logged loudly
  when reached. Nothing pads a short edit: that would mean inventing footage.
- The stage reads the STORY job result rather than re-deriving the plan (§81).
  Re-running the optimiser would usually agree, and the one time it did not the
  EDL would describe a different video from the one the user approved.

**Also found:** the interaction layer disabled clips with a raw `UPDATE` and no
re-flow, so "delete clip 5" in chat left a hole in the video exactly the length
of clip 5; effects were stored at absolute positions against a schema
documenting them as clip-relative; `clip_index` updates collided with their own
unique index on every reorder; the STORY stage reported `clips` as a list
normally but as the count `0` when it skipped, so the first project with no
moments took the EDL stage down; and a second import cycle stopped
`scripts/doctor.py` from starting while the suite stayed green. Packages are
now imported one per subprocess in `tests/unit/test_imports.py`, which is the
only arrangement that can catch that.

Full write-up: [`docs/PHASE_8.md`](PHASE_8.md).

---

### Phase 9 — Remotion overlay ✅ done

**Goal (§66, and decision D-008):** captions and motion graphics only.

**Create**
- `remotion/` — Remotion project, `OverlayLayer` composition, transparent canvas
- `backend/rendering/remotion.py` — write `composition.json`, invoke the render
- `backend/rendering/composition.py` — the §64 description, built in Python
- overlay renderers for the Remotion half of the effects library

**Acceptance: passed.** Measured rather than asserted: compositing the overlay
over known footage changes **zero** pixels before the caption appears, and 7 %
of the frame during it.

**Traps — both handled**
- Overlay only. The gameplay is never drawn through Chromium; the composition
  carries no video, and only Remotion-engine effects cross the boundary.
- The pass is skipped when the plan has no Remotion-engine effects and no
  captions — `Composition.is_empty`, which an FFmpeg-only effect plan satisfies.

**Also found:** VP9 in WebM carries alpha as a side channel, so `ffprobe`
reports `yuv420p` and FFmpeg's *native* VP9 decoder discards it silently — the
overlay composites as an opaque rectangle with nothing reporting a problem.
`overlay_input_arguments()` names `libvpx-vp9`, and a test asserts the failure
still exists so the fix is not mistaken for superstition. Also: the Remotion
sequence offset was being subtracted twice, making captions invisible for most
of their duration.

**Licence settled:** free for individuals and companies up to three employees,
read from the licence text rather than the docs page. This project is inside
the free tier.

Full write-up: [`docs/PHASE_9.md`](PHASE_9.md).

---

### Phase 10 — Final Render ✅ done

**Goal (§65, §72–§75):** the MP4.

**Create**
- `backend/rendering/ffmpeg_renderer.py` — cut, concat, resumable segments
- `backend/rendering/audio_mix.py` — mix, ducking, normalisation (§72–§74)
- `backend/rendering/composite.py` — overlay + final encode, one pass
- `backend/rendering/encoder.py` — NVENC with libx264 fallback
- `backend/pipeline/workers/render_worker.py`

**Acceptance: passed.** The finished file decodes end to end with empty stderr,
is an MP4 with the configured codecs, carries both a picture and a sound, and
lasts as long as the edit said it would. The §6 band is checked where it is
decided, in the timeline.

**Also found:** `ffmpeg -encoders` lists `h264_nvenc` on this machine while the
driver is one nvenc API version too old to open it — every render would have
failed minutes in, and `doctor.py` reported hardware encoding as available
because it read the same list. Both now *try* the encoder rather than asking
it, which is the PaddleOCR lesson from Phase 5 applied again. Also: music
looped with `apad` pads with silence rather than repeating, and `amix` left to
normalise drops the whole mix ~9 dB the moment music is added.

Full write-up: [`docs/PHASE_10.md`](PHASE_10.md).

---

### Phase 11 — QA ✅ done

**Goal (§76, §77):** catch bad renders automatically.

**Create**
- `backend/qa/technical.py` — decode, duration, resolution, fps, streams,
  black/frozen frames, A/V sync, loudness
- `backend/qa/content.py` — menus in the edit, extreme silence, broken
  sequence, flash cuts, captions covering HUD
- `backend/qa/report.py`
- `backend/pipeline/workers/qa_worker.py`

**Acceptance: passed.** Five deliberately broken renders — black, frozen,
silent, no audio stream, wrong length — are each caught **by name**, and a good
render comes back clean. Technical failures block export; content warnings go
to human review and never stop the file.

**Also found:** `freezedetect` prints no duration for a freeze still running at
the end of the file, so pairing starts with durations dropped exactly the
entirely-frozen case — the most obviously broken video was the one that passed.
QA also *raised* when the render had legitimately skipped, so a recording with
nothing worth editing produced a failed project rather than a plain "there was
nothing to make a video from". And adding the RENDER worker made every earlier
phase's integration test encode an MP4; `tests/conftest.py::workers_through`
now limits each file to its own phase.

**Revisited since:** the frozen-frames check was measuring the encoder rather
than the video. Its noise floor is configuration now (`freeze_noise_db`, -45 dB,
chosen by measuring real footage through both encoders), and a freeze is
checked against the *recording* before it blocks — so a menu that survived into
the edit warns, and only a render that stopped on its own fails.

Full write-up: [`docs/PHASE_11.md`](PHASE_11.md).

---

### Phase 12 — UI ✅ done

**Goal (§57–§62):** the smallest interface that makes the pipeline usable.

`apps/web` — React + TypeScript + Vite against the local API. Screens:
Dashboard, Import, Analysis, Moments, Timeline, Preview, Export, and the Chat
panel.

**Acceptance: passed**, against the real 21-minute recording rather than a
fixture — imported, analysed, moments reviewed with their reasoning, a clip
removed and restored, rendered to 852 MB, QA'd, and played back in the browser
with seeking.

**Also found:** the API queued jobs and **nothing ran them** — every test had
called the runner directly, so the missing half was invisible until a browser
was pointed at it. `backend/services/worker.py` is that half, and
`scripts/serve.py` now starts the whole application with one command. Recovery
of interrupted jobs then raced the worker for a job it had just claimed, and
lost: a render two clips in was reported as "queued". Recovery now runs on the
worker's own thread, before its first poll.

Full write-up: [`docs/PHASE_12.md`](PHASE_12.md).

---

### Phase 13 — LLM fallback for interaction ✅ done

**Goal (§63, §85, §92–§95):** the one thing rules are genuinely bad at —
reading a sentence someone typed.

The rule parser already reported `confidence == 0.0` on text it could not read;
this phase wires that signal to a local `qwen2.5:7b-instruct` that returns a
validated `IntentDelta`, `EditCommand` or grounded `Answer` — never free prose,
never a file operation. Rules run first, so a machine without Ollama loses the
unusual phrasings, not the feature (§95).

**Acceptance: passed**, both halves. In the suite, "give it the feel of a
wildlife documentary" and "delete the part right after the opener" — both
rejected by the parser, both asserted so — change the stored brief and shorten
the real edit, while writing no file and re-queuing no analysis job. Against
the real model on Ollama, the same sentences through the same service.

**Also found**, and only by running the real model: **four enum values in the
prompts did not exist** (`dead_time_policy: keep_context` is really `keep`;
`captions: animated` never existed). Ollama enforces a schema as a *grammar*,
so the model was forced to emit the wrong value and then rejected for it —
four of eight dimensions failed permanently, and the only symptom was a refusal
that blamed the model. The same mechanism turned "make it 30 seconds" into a
50-minute video: `minimum: 600` made 30 unemittable, so the model produced
3000. Durations came out of both prompts entirely — that is arithmetic, which
the rule parser does exactly.

Also: a model reading that changed nothing reported "updated the editing
brief"; and an edit command naming no clip landed on the *instruction* path, so
the instruction path now escalates to the command prompt before giving up.
Preference first, then edit.

Full write-up: [`docs/PHASE_13.md`](PHASE_13.md).

---

### Phase 14 — Game profiles ✅ done

**Goal (§22–§25, §111):** one real game validated before any others are
written, then the API that makes the second one a data change.

`profiles/gta_v/profile.json` — regions measured off real 1080p frames, not
guessed. `backend/gaming/hud.py` reads the indicators a game shows *without
words* (§24): GTA V's wanted level is five star glyphs in a corner, and no
amount of OCR will ever read it. Changes become §26 events; the steady state is
context. `backend/api/routers/profiles.py` lists, fetches and validates.

**Acceptance: passed**, on a 62.6-minute GTA V capture the reader was not tuned
against: **3 events the generic profile cannot produce at all** — an escape, a
chase, and a 3+-star spike — from 40 sampled frames. The generic count is zero
by construction, which is §23 holding.

**Also found:** the wanted-star row **flashes** while the police search, driving
every glyph bright then every glyph empty about twice a second, so a frame
caught mid-flash carries no count at all. That is why three successive pixel
discriminators disagreed on the same footage — they were reading a value that
does not exist in those frames. The reader now detects the flash and declines.
It also declines when the **ammo counter** slides into the same corner at
wanted level zero, which before a glyph-shape test read as three stars *at full
confidence*. Twelve hand-labelled frames: 12/12, zero confidently wrong. On a **held-out**
96-minute recording the reader had never seen, the wanted level traces a
coherent arc — 0 → 3 → 4 → 3 → 2 → 0 across 95 minutes — with intermediate
values the tuning footage never produced.

Full write-up: [`docs/PHASE_14.md`](PHASE_14.md).

### Phase 15 — Quality ✅ done

**Goal (§112, §117–§119):** the first numbers about whether the moments it
picks are the moments a person would have picked.

`backend/quality/` — the dataset format (§117), the metrics (§118), and §119
read from the edit history the interaction layer already keeps. `scripts/
annotate.py` builds contact sheets at a **fixed** interval so labels cannot
agree with the system by construction; `scripts/evaluate.py` scores a project
and prints the cases, not just the ratios.

**First measurement**, ten minutes of real GTA V against 16 labels written
before the pipeline ran: **events P 0.38 / R 0.86**, **moments P 0.33 /
R 1.00**, and 6 seconds of selected footage overlapping stretches marked
boring. Recall is strong, precision is weak — and precision is a *lower bound*,
because 16 labels over ten minutes is sparse and many unlabelled detections are
probably real.

**Also found:** the measurement tool was wrong first. It reported a death as
missed that the pipeline had found at 0.97 confidence — §27 merges detectors,
so the death arrived as a 26-second span whose midpoint sat 8 seconds from the
label. Matching now works in either direction and recall went 0.71 → 0.86 on
the same data. And `scripts/serve.py` exits silently when its port is taken,
**taking the worker with it**, which cost one wasted analysis.

Full write-up: [`docs/PHASE_15.md`](PHASE_15.md).

---

## 6. Definition of done for the whole product (§127)

```
INPUT    2–3 hour gameplay recording
USER     game selected or auto-detected · Mode: Story · Target: 20 minutes
SYSTEM   analyse video + audio → transcribe → scenes → keyframes → game events
         → reactions → correlate → moments → score → remove repetition and dead
         time → preserve context → narrative → hook → optimise duration → EDL
         → captions → effects → Remotion composition → FFmpeg render → QA
OUTPUT   ~20-minute YouTube gaming video
         + editable project + timeline + moment list + analysis metadata + QA report
```

And: **regenerating the video after any edit must not re-analyse the source.**
That is a design requirement, not an optimisation.

---

## 7. Environment and verification

### Build machine used for Phases 1

Ubuntu 24.04, 4 vCPU, 15 GiB RAM, **no GPU**, FFmpeg 6.1.1, Python 3.11,
Node 22. No Whisper, no Ollama, no OCR engine installed.

Consequences to keep in mind:

- `doctor.py` reports GPU, NVENC, speech, Ollama and OCR as **warnings** and
  exits 0 — §52/§95 require the pipeline to run on CPU.
- NVENC is reported *unusable* rather than *available*: the encoder is compiled
  into this FFmpeg build but has no GPU behind it.
- **Every acceptance test that needs a real model must be re-run on the target
  Windows / RTX 3070 machine before the MVP is signed off.** Phases 3–6 can be
  written and unit-tested here with fake providers; they cannot be *accepted*
  here.
- **Run against real recordings, not only fixtures.** `D:\Gaming 2026` holds 14
  sessions, 7–96 minutes, 1080p60. `scripts/run_real_source.py <file>` takes one
  end to end and reports what each stage cost and produced. The first such run
  found two defects that 919 passing tests had not — see
  [`docs/FIRST_REAL_RUN.md`](FIRST_REAL_RUN.md).

### Target machine setup

Installed already: FFmpeg 9.0 (gyan.dev full build, NVENC present),
faster-whisper 1.2.1, PySceneDetect 0.7.1, PaddleOCR, OpenCV, numpy/scipy,
Ollama with `qwen2.5vl:7b`. Remaining:

```powershell
ollama pull qwen2.5:7b-instruct # Phase 13 LLM
python scripts/doctor.py        # expect all OK
```

### Verification gate for every phase

```bash
.venv/bin/python -m pytest      # all green, slow tests included
.venv/bin/ruff check .          # clean
.venv/bin/python scripts/doctor.py
```

`pytest` runs the acceptance tests by design: excluding them by default would
mean a green suite that never checked the thing the phase was for. Use
`-m "not slow"` in the development loop.

A phase is complete only with implementation **and** tests **and** an executed
acceptance criterion. Never report a phase done without running its tests.

---

## 8. Open decisions

Not blocking, but worth settling before the phase that needs them.

| Question | Needed by | Current lean |
| --- | --- | --- |
| Which game is validated first? | Phase 14 | Whichever the user records most. Generic path works regardless. |
| Speaker diarization for multiplayer voice chat? | Phase 3 | Skip for MVP; microphone vs gameplay separation is enough. |
| Do we ship model weights or download at setup? | Packaging | Download at setup; the repo stays small. |
| Desktop shell — Tauri or plain localhost? | Phase 12 | Localhost web app first (§9 allows it), shell later. |
| Multi-recording projects (multicam) | later | Sources are already independent; true sync is out of MVP scope. |
| ~~NVIDIA driver too old for this FFmpeg's NVENC~~ | ~~when convenient~~ | **Settled 2026-08-11:** updated 581.15 → 610.88. NVENC now opens, and a 10.4-minute render fell from **942 s to 293 s** — 3.2× faster. The fallback path stays, because the machine this ships to may not have it. |
| ~~Remotion licence for commercial use~~ | ~~Phase 9~~ | **Settled 2026-08-11:** free for individuals and for-profit organisations with up to 3 employees; above that, a company licence. This project is inside the free tier. |
| ~~The worker leaves queued jobs untouched~~ | ~~when reproduced~~ | **Settled 2026-08-12:** cancelling flagged queued jobs and left them queued; `next_runnable` skips a cancelled job, so nothing ran them and nothing said so. One project sat at "queued" for eight hours beside a healthy idle worker. A queued job is now cancelled outright, and startup settles rows an older build abandoned. |
| ~~The overlay pass renders 599 s to cover 115 s~~ | ~~next speed pass~~ | **Settled 2026-08-19:** the overlay now renders only the stretches that draw something. `backend/rendering/overlay_plan.py` merges the composition's spans (already recorded since Phase 9 and never read), caps the count so the composite's filter graph stays small, rewrites the elements into a shorter composition, and hands FFmpeg the offsets to put each stretch back. Timed on *Ziad 4* against the same composition, same machine, same settings: the whole layer took **1,284 s** for 18,287 frames; the segmented layer took **15.5 s** for 98 -- **83x**, and the overlay stops being the render at all. Measured across all nine real projects: **43.8% to 99.5% of the Chromium frames removed**, median 88%; on *Ziad 2* itself, 31 spans become 23 segments and 80.4% of the pass disappears. The segment ceiling of 24 costs almost nothing — the worst project saves 43.8% at 24 against 45.3% uncapped. Proved against a decoder rather than a filter string: two one-second stretches from a two-second overlay land on exactly their frames, the gaps stay untouched, and the video keeps its full length. Original measurement below. | Measured 2026-08-12 on *Ziad 2*: of a 9:58 programme, captions and effects occupy **115 s (19%) across 52 islands**, and Remotion screenshots every frame of all 599 s anyway. The overlay took **22 of the render's 26 minutes**; the cut and concat took 1.4 s on NVENC. Rendering only the islands (merged with a join tolerance, then composited at their offsets) is the largest single speed win left. The risk is A/V alignment, so it needs the frozen-frame and sync QA checks as its gate. |
| **J-cuts / L-cuts (audio leads or trails the cut)** | next editing pass | Researched 2026-08-14: the standard alternative to hard A/V cuts inside scenes. Needs overlapping audio across segment boundaries, which the segment-concat render pipeline cannot express today -- a real but contained rework of the audio assembly. Dip-to-black time-jump grammar shipped instead (same research pass); LUFS was already at YouTube's -14; effects budget (6/min) already exceeds the 30-60s pattern-interrupt cadence the retention literature recommends -- the sparse *triggers* are what limit effect count, same root as the menu-detection density note below. |
| **The render assumes an empty card and never checks** | next render pass | Measured 2026-08-15: a `qwen2.5-coder:7b` model left resident by *another program* held 4.7 GB of the 3070's 8 GB — with `expires_at` in the year 2318, so nothing would ever release it. Chromium then could not start, timing out after 25 s, and **19 render-dependent tests failed** across two full suite runs that each took 45 minutes instead of 22. §54's "one heavy model at a time" is honoured *between our own stages* and assumes nothing else is on the machine. The overlay pass should read free VRAM before starting Chromium and either lower `--concurrency` or say plainly that the card is full — a render that fails after twenty minutes because something else is resident is the worst way to learn it. |
| ~~This repository has no LICENSE file~~ | ~~before any release~~ | **Settled 2026-08-20:** MIT, at the owner's choice. |

---

## 9. Change log

| Date | Change |
| --- | --- |
| 2026-08-10 | Original spec replaced by the gaming-first specification. Nothing had been committed, so the tree was rebuilt against it rather than migrated. |
| 2026-08-10 | Publishing seam added ahead of need, so YouTube auto-publish will not require a pipeline change. |
| 2026-08-10 | Engineering review applied: `faster-whisper`, region-restricted OCR, candidate-only vision, Remotion as overlay-only, strict sequential VRAM use. |
| 2026-08-10 | Interaction layer added as an independent layer above the pipeline. |
| 2026-08-10 | Effects engine added as an independent module with a 22-effect library. |
| 2026-08-10 | **Phase 1 complete** — 414 tests, committed and pushed. |
| 2026-08-11 | NVIDIA driver 581.15 → 610.88, so NVENC opens. The same 10.4-minute render fell from 942 s to 293 s. Also revealed that the frozen-frames QA check gives different verdicts per encoder — recorded as a known sensitivity. |
| 2026-08-11 | **First run on a real recording** (21 min, 1080p60). Found two defects 919 tests had missed: the transcript read the gameplay track instead of the microphone (§19), and both audio tracks of every recording on the machine are byte-identical, so the copy was being analysed twice and labelled "microphone". Write-up: [`docs/FIRST_REAL_RUN.md`](FIRST_REAL_RUN.md). |
| 2026-08-14 | **Research → audit → comparison → implementation pass** (three-agent workflow). The audit's central find: seventeen planned effects across three real projects and zero reaching pixels — the render worker passed `effects=()` to the overlay and no FFmpeg realiser existed. Shipped: (1) `TimelineRepository.list_effects` + an FFmpeg realiser for the duration-neutral five (zoom, punch_in, cinematic_bars, flash, camera_shake) baked into segments with the effect set hashed into the segment name for §47 reuse; (2) the Remotion wire closed with reader-side content guards; (3) Stanford's α=0.9/β=0.1 pause split (Leake et al., SIGGRAPH 2017) replacing midpoint cut placement, with a 0.2 s trailing floor; (4) caption direction from the first strong-directional letter when the stored language is NULL, so the two legacy Arabic projects render RTL. Verified on real footage: punch-in edges land at the stored centiseconds (YDIF spikes at 19.45 s/20.33 s), bars darken exactly 138 px rows, the VICTORY pop has non-zero alpha in the overlay, QA green on all three projects. |
| 2026-08-15 | **Phase 0.3 — the profile that never loaded, and the game nobody checked.** `projects.detected_game` had existed since the schema was written with nothing to fill it, so every project resolved to the generic profile and `profiles/gta_v` had never once been used. Worse, the footage is not GTA: the OCR says *Milk Molar*, *Lean-To*, *Dandelion Tuft*, *Soldier Ant Egg* — Grounded, in both real projects. Shipped: a deterministic game recogniser (three signatures and a clear margin, or it stays silent), a `grounded` profile written from measured on-screen text, profile-level fusion rules, and the correction that mattered most — `\bdefeat\b` had been reading the quest tracker *"Defeat the O.R.C. guards at the Milk Molar stash"* as a defeat **19 times in one recording**, making it the most common named event in the project and every one of them wrong. Honest named-event ratio on سبي: **0.22 → 0.55**. Also found: the renderer was starting `node` from `C:\Program Files` rather than the bundled `tools/node`, against the standing rule that nothing depends on the system drive. |
| 2026-08-14 | **Engineering review of the pass** (8-angle finder fleet, 25 candidates). Fixed: Remotion effects on disabled/trimmed clips now filtered by the same single-owner rule as the FFmpeg half (they previously drew at placeholder positions over unrelated footage); moment event types plumbed from the moments table into the planner (every `events:` trigger list was dead input); text_pop labels only from trigger-listed events (never `UNEXPECTED EVENT`); kill_counter needs ≥2 countable events and carries its tally; stingers obey the effects/realisation switches; legacy `strength` popped, not read; filter chain orders camera moves before frame furniture (a zoom after bars visibly thickened them); letterbox skipped on portrait targets (would cover 76%); realisers are a registry the realisable set derives from; `min_trailing_seconds` floor made honest (0.2) and sub-clearance gaps yield no pause candidate at any `min_pause`; caption RTL decided by UAX#9 first-strong letter, so code-switched Latin-first lines stay LTR. |
| 2026-08-27 | **Golden set 16 → 53 spans** across three windows and two games; GTA window labelled before analysis (out-of-sample). GTA events P 0.35 / R 0.82 with fragmentation, not hallucination, as the dominant penalty; Grounded first-ever numbers P 0.42 / R 0.83 with 139 s of moments over boring stretches. `scripts/analyse_cut.py` added; dataset tests parametrised over `datasets/*`. |
| 2026-08-27 | **Evaluation moves to situation granularity.** `read_as_episodes` scores events through the product's own episode reader (generics pass through); straddling predictions match first, judged after (a boundary episode had turned a found label into a miss). Seed 0.26→0.35, GTA out-of-sample 0.35→0.53, Grounded 0.42→0.56 precision, recall unpaid everywhere. |
| 2026-08-27 | **Detector wave from the golden set's misses**: cluster discipline (context never bridges, claiming span capped 15 s), vision-only low_health demoted, `suppressed_generic_rules` (Grounded vetoes the two rules its footage contradicts), creature-bar OCR events, `WOOZY` as near_death, `description_pattern`/`min_label_count` on fusion rules, fire named from the prose (`visible_destruction`/`burning_wreck`), generic markers unjudgeable in the metric, type-aware tie-break. Grounded events 0.60/**1.00**, moments **0.80/1.00**; every remaining out-of-sample miss carries its documented reason. |
| 2026-08-27 | **Live proofs + ship**: three real Shorts with captions in 296 s; certification story→QA in 5.3 min with QA green, VRAM gate consulted, J/L planner correctly keeping a speechless boundary hard; `jl_cuts` ships enabled; `dist/VAI-0.1.0.zip` rebuilt (296 MB). Live YouTube publish awaits only the owner's Google OAuth client. |
| 2026-08-28 | **Delivery choices at the import screen** (owner requests): `captions_enabled` opt-in (off = nothing written on the frame, Shorts included; legacy projects backfilled on), `output_directory` (validated, C: refused; render copies the finished file there, failure is a §95 note), `auto_publish` (green QA queues the upload with analysis-written metadata — the tick is §51's asking), plus a one-press **Publish to YouTube now** on the Export screen. Migration 0004, schema 4, 1,671 tests. |
