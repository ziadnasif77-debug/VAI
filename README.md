# AI Gaming Video Editor

A local-first AI video editor for gameplay. It ingests long recordings,
understands what happened through multimodal analysis, ranks the moments worth
keeping, builds a coherent 10–60 minute YouTube video, and verifies the result —
entirely on your own machine.

Not a video cutter. The system watches the footage, detects gameplay events and
player reactions, forms and scores moments, removes dead time and repetition
while preserving context, constructs a story, and renders it.

```
3 hours of gameplay  →  Story / Best Moments  →  20 minutes  →  YouTube-ready MP4
```

**Status:** the 15 foundation phases, the 2.0 plan and the **V2 editorial arc
(P0–P11)** are complete. A real recording goes in and a finished, QA'd video
comes out, entirely through the browser, and the machine can now run a night on
its own.

2.0 added perception dense enough to watch every nominated region
(`unknown_event_ratio` 0.61→0.36 on one real recording, 0.45→0.23 on another),
a **Director** that proposes the shape of the edit, a **Critic** that reviews
the assembled timeline before rendering, learned per-user preferences, and an
overlay pass that renders only the frames that carry something — measured
1,284 s → 15.5 s on a real ten-minute edit.

V2 turned the editor from a system that applies rules into one that reads the
session it was given, and then into one that **decides differently depending on
the style asked for**: a semantic spine every stage shares, cut lengths that
answer to the second they start on, emphasis that composes into gestures,
sound that hears the session, three candidate edits judged against each other,
a critic that watches the finished video, an explicit style with declared
bounds, outcome data joined to the edit that produced it, and a tuning
mechanism that is fenced, reversible and — deliberately — dormant.

Every model answer is checked against evidence and every fallback is the
deterministic pipeline that shipped first. Progress, measurements and what
remains: [docs/PLAN.md](docs/PLAN.md).

---

## What makes it different

**It decides, then executes.** The AI produces structured decisions; ordinary
code applies them. No model ever runs a shell command, writes a file or builds
an FFmpeg filter graph.

**It works within 8 GB of VRAM.** One model is resident at a time. Vision runs
only on candidate regions nominated by cheap detectors, so a two-hour recording
does not become six hours of inference.

**It never re-analyses to re-edit.** Change the duration, the mode, or a single
clip, and only the story → EDL → render → QA chain re-runs.

**It explains itself.** Every selected moment carries the score breakdown and
the events that justified it. Ask why a clip was chosen and the answer comes
from the data, not from a language model's imagination.

**It works without talking to it.** Import, pick a duration, press analyze. The
chat is a control surface, not a requirement.

**It answers in the language you asked in.** Arabic question, Arabic answer;
English question, English answer — decided per message, not by a setting. The
moment types, event names and score dimensions are translated too, so an
explanation reads as one sentence rather than a frame around English terms.

**It reviews its own work.** Before rendering, a model reads the assembled
edit clip by clip — what is on screen, what was said, what happened — and may
trim a dead opening or drop a clip that is all menu. Code holds the veto:
a note about a clip that does not exist is discarded, and no review may take
the video out of its requested length.

**It learns what you keep asking for.** "Make it faster" in three separate
projects becomes the starting point of the fourth — and anything you say about
the current project still beats it.

**It reads the HUD when it knows the game.** A profile declares where the game
puts its state, and the pipeline reads what no OCR can — GTA V's wanted level
is five star glyphs and no text. Without a profile, nothing changes: vision,
OCR, audio and speech carry the analysis on their own.

---

## What V2 added

**It reads the session, not just the clips.** A semantic timeline samples nine
lanes at 2 Hz — intensity, tension, motion, audio, events, speech, scene
changes, novelty, dead zones — percentile-normalised *within the session*, so a
quiet game still has a shape. It is a pipeline stage of its own, stored under a
digest of its inputs' values, and every stage downstream reads the same spine.

**A cut length is a decision with reasons, not a lookup.** Each shot's length is
re-read at the second it starts on: the level's band, then sustained tension,
then never cutting inside a spoken word, then landing on the beat, breaking a
stutter, cutting on movement, the hook and ending roles, and a readability
floor. Every shot carries the list of rules that set it, so "why is this shot
four seconds" has an answer.

**A moment says what it is doing, or says it does not know.** Setup,
anticipation, escalation, payoff, reaction, dead — measured from the session's
own lanes with a confidence anchored to the refusal threshold. When it cannot
tell, it says `unknown` instead of guessing.

**Effects compose into sentences.** An anchor at a real timestamp with members
at signed offsets and declared dependencies. A composition is admitted whole or
not at all — the emphasis engine will not ship half a gesture — under a cluster
budget and a no-repeat rule.

**Sound hears the session.** A music bed per section, ducking under the game's
own loud moments, speech read from the transcript rather than from opt-in
captions, and silence used as a tool that QA is told about in advance.

And the ducking hears *how* loud. The audio lane's value is what defines a loud
span, and it used to be compared against the threshold and then discarded — so
across 1,900 spans on this machine there was exactly **one** duck depth, and a
footstep a hair over the line took the bed down as far as a full explosion. The
peak now travels with the span and the depth is interpolated: the configured
`-8.0 dB` is what an event at full scale gets, and nothing gets more. On one
4,035-second recording that changes the music level under **24.6 % of the
video**, with the deepest point unchanged.

**It refuses footage that is not the game.** A recording is not a stream of
gameplay: it contains menus, loading screens, pause screens, the game's own
intro, and the screen a player sees when they die. Every stage used to treat
all of it as material to be scored and cut, and a real 88-minute session
proved what that costs — seventeen seconds of a `MISSION FAILED` menu, a
"restart the mission?" prompt and a loading screen played at 1:47 of a
finished video.

The evidence had been on disk the whole time. At that instant the OCR had read
`MISSIONFAILED`, `AGENT DOWN:` and `EXIT TO MENU` and stored every word, and
the vision model had labelled neighbouring frames `loading`. **Nothing
consumed either.** `backend/gaming/content.py` is the consumer that was
missing: it merges the two into one `GameplayState` per stretch — a span, not
an instant, because OCR samples a frame every seven seconds and a menu on
screen for twenty is read once — and the moment and timeline stages refuse
what it names.

Merged rather than run in parallel, and that was measured rather than assumed:
on that session the text reads and the vision labels refuse **sixteen and ten**
clips of the shipped render and share only **four**. The labels see menus with
nothing written on them; the text sees the ones whose whole identity is
written. Neither alone was ever going to be enough.

The vocabulary is per-game with a generic fallback, reusing the mechanism
`GameProfile` already had for events: regex over OCR, restricted to named HUD
regions, carrying how far the state reaches either side of the frame that
proved it. A game adds its own wording — HITMAN's loading screen names its
targets and says nothing generic — and may disable any generic rule by name,
the same escape hatch the fusion table already offered.

Two corrections are worth recording because both were mine. The first cut of
this trusted the vision half at 0.45, below the threshold that refuses
footage, on the grounds that the model had called a `MISSION FAILED` screen "a
combat situation". That was the wrong lesson from the right observation: the
sentence was a *description*, and the layer reads the model's **labels** —
which at that instant said `loading`, correctly. Distrusting labels for a
description's error cost six real menus. And the generic pattern for a game's
opening had to become `WELCOME T[O0]`, because the OCR read "Welcome t0" three
times out of four and "Sapienza Ilaly" for Italy. That is a property of
reading text off a compressed frame, not of one game.

**It argues with itself before rendering.** Three candidate edits are built from
the same moments under different profiles and scored by a deterministic judge on
eight axes. The winner is rendered; all three are kept in the job result, with
the reasoning.

**It watches the video it made.** After QA, frames of the *finished render* are
described by the vision model and measured against the programme lanes —
repetition, tails, a weak hook, effect piles, fatigue. It may correct the edit
once, under three locks: one cycle, no reordering (there is no verb for it), and
no degradation (a corrected edit that scores lower is restored from a snapshot
of the clips **and** the effects).

**The style has a body.** `config/style.yaml` holds taste and only taste, in the
same namespace the effects library already used, versioned, with every tunable's
legal range declared once. Every edit is stamped with the resolved body that
made it, so "which videos were cut this way" survives a later edit of the file.

**Outcomes are joined to the edit that produced them.** Retention curves and
per-video totals stored against the project, with a projector that places a dip
on the shot that was on screen for it. This is **outcome correlation**, not
retention prediction — see below.

**A moment is read as a shot.** What the session was doing before it, during
it and after it; which seams the footage already offers as cut points and which
of them fall inside speech; whether the tension it carried let go; whether
somebody starts speaking afterwards. Derived from stores the analysis stages
already filled — there is no evidence table, because the analysis tables *are*
the evidence.

**Related events are read as one situation.** `combat → low_health → healing →
combat → victory` is five episodes to the correlator and one editorial
situation to an editor: an attack that went wrong, a recovery, a win. Grouped
by the relations the correlator already found, never by proximity — that merge
was measured across 255 events on three recordings and deliberately refused,
because time alone cannot tell "this fight is still going" from "something else
happened nearby".

**The style decides what gets selected, not just what gets decorated.** A style
reaches the optimiser as five bounded multipliers on the objective it already
has, so the optimiser's own code is untouched and still deterministic. Measured
across 17 projects and 6 styles before a single effect is placed: style-edits
byte-identical to the house edit fell from 55 of 85 to **10 of 85**.

**And how a shot is cut, not only which shots.** Selection alone is not enough
to make a style, and the number that proved it is stark: on the eight sessions
holding less footage than the target length, the optimiser keeps every moment,
so five different styles produced **one identical video, 40 times out of 40**.
A style now also says how much run-up a shot keeps, where its edges land, and
whether a stretch that earns nothing is priced. That works whether or not
anything was left to choose, and 32 of those 40 now differ.

**Dead time means something.** `dead_time_score` was zero on all 435 stored
moments and could not be anything else — the pass that finds dead stretches
searches the gaps *between* moments, and the penalty that reads it measures
*inside* one. Rather than repair the arithmetic, the question changed: a dead
stretch is one that adds no context, no anticipation, no progression, no payoff
and no reaction. Each is read from a different store, deadness is what is left
after the strongest claim, and no style sees it until it asks.

**It reads the joins, not only the shots.** Every other reading is about one
shot. This one is about what happens *between* two — rhythm, contrast,
continuity, repetition, and whether the cut lands on a boundary the footage
already has. It is what lets the judge tell a held payoff followed by a brief
reaction from two shots of one kind at two different lengths, which is the
difference between editing and noise.

**Where the video starts is an editorial decision.** The cold-open hook moves
the strongest moment to the front, which is a flash-forward, so a chronological
edit refuses it — and every edit here is chronological, because the owner asked
for time order three times. What is left is choosing where to *begin*: moving
the first index reorders nothing. It opens on a stronger shot when there is one
worth reaching, never on an outcome, and never past the setup that explains what
it would open on.

**Chronology is constitutional.** No engine may reorder events; a stronger
moment never precedes what happened before it. The single exception is the
cold-open hook at the start, and the rule is checked on the built timeline
rather than assumed from the inputs.

---

## Four styles, one recording

```bash
# the same session, four editorial decisions
"high energy"   → gaming_fast   shorter shots, tight run-ups, dead time expensive
"cinematic"     → cinematic     the longest shots of any style, 13.7s median
"funny"         → funny         the walk-up trimmed, the reaction never
"competitive"   → competitive   tight at both ends, cut on seams, dead time costly
"minimal"       → minimal       no decoration, and the hardest price on dead time
```

The difference is in **selection, context, cut points and pace**, which is the
part that makes it an edit rather than a filter. Measured at the point shots
are actually cut, the median shot runs from 5.6 s under `gaming_fast` to 13.7 s
under `cinematic` — a 2.4× range on the same footage.

That number took three phases to see. The harness that measures style
differentiation stopped at the *plan*, and a style's most direct say over pace
is the pacing doctrine, which the EDL stage consumes after it. So `cinematic`
was reported as the weakest style for two phases running, on a measurement
taken one stage before the layer that style lives in. The same doctrine then reaches pacing, audio,
the counterfactual judge and the post-render critic — a `cinematic` edit is not
called fatigued at forty seconds of one level, and a `minimal` edit is not
marked down for having no effects.

Two promises hold it together, and both are tested:

- **The house style is exactly unchanged.** A style that asks for nothing
  returns the caller's own configuration object — by identity, not by equality
  — so `best_moments` selects precisely what it selected before styles could
  reach the selection at all. Not nearly precisely.
- **A doctrine cannot exceed its declared range.** Every multiplier is fenced
  in `config/style.yaml`, composing two legal policies cannot produce an
  illegal one, and the bound is checked again when the value is read.
- **And the same promise now covers the captions.** `funny` lands its punchline
  a sixth larger and lights it in its own colour; `competitive` turns off the
  fade, the rise and the travelling highlight, because that motion sits over
  the play and arrives exactly when something is happening. The other four
  styles receive §71's configuration object *itself*, so they cannot drift
  from the house rather than merely happening to match it.
- **A cut never lands inside a spoken word — and now it can see them.** The
  evidence projection filtered records by whether their *start* fell inside the
  window, which for a transcript segment running from 1951s to 2070s means an
  eight-second look-ahead at 2015s found silence in the middle of somebody
  talking for two minutes. Fixed, the speech lane and the transcript went from
  disagreeing 104 times to zero, and the rule against cutting mid-sentence
  gained 1,677 spans it had been blind to.
- **A shot can end once the thing it is about is decided.** Inside a moment
  running three minutes, the victory it is named for occupies eleven seconds
  at 43 % of the way in — and the shot used to run 110 seconds past it. The
  event boundaries locate the resolution from the event's own timestamp, never
  from the label, and a style may ask for the tail to end there: on a seam the
  footage already has, never through a reaction, and never past 35 % of the
  shot.
- **The house edit is frozen, project by project.** `tests/golden/house_edit.json`
  holds the exact edit this machine made for all 17 of its projects — selection
  and order, the winning profile, the hook, the ending, the timeline's clip
  boundaries after clamping, the finished length, the eight judge axes. A change
  that moves one boundary by 40 ms fails it. Only the default style is frozen:
  the other five exist to differ, and `tests/integration/test_style_differentiation.py`
  is the complement that says they may not stop.

  It fails in **two** places, because two different things can move. One is the
  video. The other is the judge's opinion of a video that did not change — an
  axis is a rating, not a property, and improving the judge moves every recorded
  axis while every frame stays put. Both still fail; a failure now says which.

## What it does not claim

This project is deliberately careful about the difference between a mechanism
and a claim.

**It does not learn from your audience — yet.** The plumbing exists and is
connected: the analytics scope, the fetcher, the tables, the join to the style
that cut each video. What has not come through it is data. On the machine this
was built for, all four published videos are now measured — and one reports
zero views while three report *nothing at all*, because they are still private.
Those are different states and the store keeps them apart; writing `0` for all
four would have made an unwatched video and an unmeasured one
indistinguishable. Zero retention points, because a curve needs an audience.

**It does not predict retention.** A retention curve is a measurement of one
video that has already been watched. Nothing here treats it as a forecast for
the next one.

**Controlled tuning is built and dormant.** It may move one style value inside
its declared range, by at most a tenth of that range, on at least fifteen
measured videos with five on each side of the comparison, with a required reason
and evidence, reversible by marking a row — and the switch is off. A proposal is
a comparison written down, not a significance test, not a model, and not a
licence. `python scripts/tuning.py status` prints the real state, which today is
`0 of 15`.

**Editorial safety is not a matter of taste, and no style may vote on it.**
Refusing a menu is not an opinion two styles could reasonably differ on, any
more than "do not cut mid-word" is. A style decides *how* valid material is
cut; it does not get to decide what counts as valid. `funny` and `competitive`
and `cinematic` will edit the same footage differently, and none of them can
make a `MISSION FAILED` screen into content. That separation is deliberate:
the default style declares no opinion at all, and a rule that were merely
"off by default" would have shipped the video that started this.

**A style changes decisions before it changes appearances — and reaches the
optimiser never.** It has picked a decoration profile since V1, and since
V2-P2.5 it also says how the captions look; what it may never do is reach
inside the thing that chooses the shots.
The optimiser is the most delicate code here and it is deterministic,
which is why any plan it produces can be argued with. So no style ever enters
it: a doctrine is translated into bounded multipliers in one place
(`backend/editorial/doctrine.py`), and the optimiser receives what it always
received. A taste that could reach inside it would be a taste that could break
it.

**No configuration key describes a capability the code does not have.**
`scripts/config_coverage.py` runs as a test: every YAML leaf must have a
consumer outside the schema. It found 51 orphans on its first run — settings
that read as enabled and were never read by anything — and the branch either
wired them or deleted them.

That check has a blind spot worth naming, because it hid two settings for a
year. It reads YAML. `dead_time_policy` and `context_preservation` are not YAML
leaves — they are fields of the editing brief, parsed from what you type,
echoed back in the confirmation, stored, and learned as preferences. Nothing in
the editing pipeline had ever read either one. You could write "احذف الأجزاء
الميتة", be told the policy was now aggressive, and receive byte-identical
footage. Both are wired now; the coverage tool still cannot see their kind.

`captions.style` was a third of that kind, found by auditing the caption layer
rather than by any tool. Not a YAML leaf either -- a dataclass field and a
database column, written on all 312 captions this system has produced, stored,
read back, and dropped before the renderer, which never had a key for it. It
held the empty object every single time, and could not have held anything else.
It is deleted rather than wired: per-style caption appearance is a taste, and
tastes belong in `config/style.yaml` with the other tastes, not on a column
every caption carries and none fills. Its neighbour `captions.emphasis`
survives the same audit unfixed and recorded -- it reaches the renderer's own
schema and no component reads it, and deciding what caption emphasis should
*look* like is a design question rather than a wiring one.

**A metric that measured the wrong thing, and what it cost to notice.** Giving
styles a say in how shots are cut moved the judge's pacing axis *down* on the
three that trim — 0.61 to 0.52 — because the axis scored `(longest − shortest)
/ mean` against an ideal of 1.2, and shortening shots lowers the mean it
divides by. The ideal was also a number no edit this system has ever made comes
near; the house style's own spread is 1.888.

Three attempts to fix it by giving each style its own ideal were reverted. The
instructive one: raising `cinematic`'s improved its pacing score *and* its judge
total, until the house-shaped counterfactual plan started winning again and
`cinematic` went back to producing the house edit exactly. The judge's per-style
taste decides which of three plans is rendered, so a change of taste is a change
of edit — and a number that improves a score while erasing the style is not an
improvement.

So the definition was replaced rather than the constant. The axis now holds no
ideal at all, and names the two ways a shot length fails to be a decision:
**arbitrary variation**, where the length changes and nothing else does, and a
**metronome**, four or more shots that never change length. Between them a
deliberately steady edit and a deliberately uneven one both score well, which is
the difference between *uneven because badly edited* and *uneven because
cinematic*. The axis sits at 0.82, above where it started, and six tests protect
the definition rather than the numbers — including one asserting the old
constant no longer exists.

Correcting it changed which plan wins on 4 of 17 projects, all of them toward
fewer arbitrary cuts. That is the golden baseline moving, deliberately, once:
[docs/BASELINE.md](docs/BASELINE.md#the-golden-baseline-changed-once-and-this-is-why)
records why, and `tests/golden/house_edit.pre-p1.json` keeps the old one so the
difference can be shown rather than asserted.

---

## The pipeline

Twenty-two stages. Each is a job row with a result, so the job history is the
history of what the machine decided and why.

```
analysis   import  probe  proxy  audio  frames  transcript  audio_events
           scenes  vision  ocr  game_events  semantic  moments

edit       story  edl  critique  render  qa  critic2

delivery   export  publish  shorts        (never automatic without being asked)
```

`semantic` is V2's spine and `critic2` is the stage that watches the finished
render. The three delivery stages are the only manual ones: §51 means gameplay
footage never leaves the machine unasked, and a delivery job that exists in the
queue was asked for — by the Export screen's button or by the owner's own
standing policy.

## Running a night on its own

The daily policy (`config/daily.yaml`) produces one long video and up to two
Reels a day, production at 02:00 Europe/Oslo, publication scheduled on YouTube
itself so nothing goes public early.

The scheduler that fires it lives **inside `scripts/serve.py`**, beside the job
worker that runs the stages. Neither exists outside that process, so an
autonomous night reduces to one requirement: that process has to be running at
02:00.

```bash
python scripts/autostart.py install    # a scheduled task: at log on, and 01:45 nightly
python scripts/autostart.py status
python scripts/autostart.py remove
```

It wakes the machine for the nightly trigger, restarts after a failure, and
passes `--keep-existing` so a healthy instance is left alone. Two limits it
states rather than hides: it runs only while the user is logged on, because
analysis needs the GPU and Ollama and both belong to an interactive session; and
registering a task is a change to the machine, so the script asks Windows and
reports what Windows said.

`python scripts/daily_cycle.py` drives one heartbeat by hand. It queues; it does
not execute — the worker does that, which is why the answer to "make it run
itself" is the application and not this script.

## Requirements

| | |
| --- | --- |
| OS | Windows 10/11 (target), Linux (development) |
| Python | 3.10+ |
| Node.js | 20+ (for the Remotion overlay pass) |
| FFmpeg | with NVENC for hardware encoding |
| GPU | NVIDIA RTX 3070 class, 8 GB VRAM. CPU fallback works, slower |
| Disk | recordings and intermediates are large; plan for tens of GB per project |

Nothing is uploaded. No cloud API is required.

## Getting started

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,ai]"   # Windows: .venv\Scripts\pip

python scripts/doctor.py               # check the environment
python scripts/db_init.py              # create the database
npm install && npm run build -w apps/web

python scripts/serve.py                # everything: interface, API, worker
```

`[ai]` is the analysis stack — Whisper, PySceneDetect, EasyOCR, torch. It is a
few gigabytes and pinned to a CUDA build of torch; the CPU wheel works and is
much slower.

### Everything lives beside the project

The machine this was built on has 5.7 GB free on its system drive and 1.3 TB on
its data drive, so nothing is left to a library's idea of where a cache goes.
`scripts/rooted.py` puts all of it under the project root and the launcher
applies it to every process it starts:

| | |
| --- | --- |
| `.venv/` | interpreter and packages (torch alone is gigabytes) |
| `tools/ffmpeg`, `tools/node` | bundled binaries, ahead of `PATH` |
| `.tmp/` | `TMP`/`TEMP` — segments, audio slices, frame dumps |
| `.cache/hf`, `.cache/torch` | model weights |
| `.cache/pip`, `.cache/npm` | package caches |
| `remotion/.browser` | Remotion's Chromium |

`OLLAMA_MODELS` is read and reported, never written. Ollama is a shared runtime
several projects on this machine use, and one 6 GB vision model loaded once
serves all of them — where its files live is the machine's decision, not a
consumer's. Setting it here once cost 36 GB in duplicate downloads.

Then open **http://127.0.0.1:8765**.

On Windows, double-clicking **`VAI.bat`** does all of the above, opens the
browser once the server answers, and finds an interpreter that has the
dependencies — with several Pythons installed, plain `python` is whichever is
first on PATH and is rarely the right one. Running it by hand, `py -3.11
scripts/serve.py` is the reliable spelling.

Launching again while a copy is running **restarts it**, which is what "start
the app" means when you have just closed the tab. It checks first that the
port belongs to this application, and leaves anything else alone. Pass
`--keep-existing` to fail instead of taking over.

`doctor.py` reports what is missing and what the pipeline will fall back to. A
missing GPU or model is a warning, not a failure.

Optional, for the natural-language editing in the chat panel (§63):

```bash
ollama pull qwen2.5:7b-instruct
```

### Publishing to YouTube

The OAuth client must be a **Desktop app** client, not "TV and Limited Input".
That is not a preference: Google's device flow requires the TV type and refuses
the YouTube Analytics scopes on it, *and* blocks the loopback redirect for it —
so with a TV client the analytics permission cannot be obtained by any flow at
all. Measured against Google's own endpoints, after this project spent an
afternoon discovering it the slow way.

```bash
python scripts/youtube_auth.py            # what the stored grant covers
python scripts/youtube_auth.py --connect  # sign in through the browser
```

Reading analytics also needs the **YouTube Analytics API enabled** on the Cloud
project, which is separate from the sign-in and fails with `accessNotConfigured`
when it is missing. [docs/ANALYTICS.md](docs/ANALYTICS.md) has the whole
sequence.

Without it the chat still works — the rule parser understands the common
phrasings, and only the unusual ones are lost (§95).
`python scripts/verify_phase13.py` shows what the model adds when it is there.

## Configuration

Everything tunable lives in `config/`. No business rule is scattered through the
source.

| File | Covers |
| --- | --- |
| `application.yaml` | paths, database, API, logging, disk limits |
| `output.yaml` | the 10–60 minute duration policy, video modes |
| `analysis.yaml` | chunking, frame sampling, scenes, audio, vision, OCR, HUD |
| `models.yaml` | AI providers, GPU budget, hardware profiles, fallbacks |
| `moments.yaml` | event correlation, scoring weights, dead time, repetition |
| `narrative.yaml` | story structure, hook, pacing, duration optimizer |
| `editorial.yaml` | the semantic lanes and the pacing bands — the mechanism |
| `style.yaml` | **the Style Bible**: taste, versioned, with declared bounds — the selection doctrines, pacing, audio, judgement and critique of each style, and the controlled-tuning switch |
| `effects.yaml` | the effects library, budgets, per-style profiles |
| `compositions.yaml` | the emphasis grammar: anchors, roles, offsets, dependencies |
| `rendering.yaml` | encoders, proxy, thumbnails, Remotion overlay pass |
| `audio.yaml` | mixing, ducking, music |
| `captions.yaml` | caption timing, layout, appearance |
| `qa.yaml` | technical and content checks |
| `critique.yaml` | the pre-render Critic |
| `interaction.yaml` | editing presets and the chat layer |
| `publishing.yaml` | delivery targets |
| `daily.yaml` | the daily production and publishing policy |

Every leaf in every one of these files has a consumer, and a test enforces it:
`python scripts/config_coverage.py`.

Any value can be overridden by environment variable:

```bash
VAI__APPLICATION__API__PORT=9000
VAI__ANALYSIS__CHUNK_SECONDS=300
VAI__MODELS__VISION__MODEL=llava:13b
```

## Tools

| | |
| --- | --- |
| `scripts/serve.py` | everything: interface, API, job worker, daily scheduler |
| `scripts/autostart.py` | register the scheduled task that starts it on its own |
| `scripts/daily_cycle.py` | drive one daily heartbeat by hand |
| `scripts/baseline.py` | measure every style's edit, diff a saved run, freeze the house edit |
| `scripts/doctor.py` | what is missing, and what the pipeline will fall back to |
| `scripts/db_init.py` | create the database |
| `scripts/config_coverage.py` | every YAML leaf, and whether any code reads it |
| `scripts/youtube_auth.py` | see what the stored YouTube sign-in covers, or widen it |
| `scripts/fetch_outcomes.py` | read what the audience did with a published video |
| `scripts/tuning.py` | look at controlled tuning, and undo it |
| `scripts/profile_report.py` | mine a recording for a game profile's regions |
| `scripts/dashboard.py` | the day's production report |
| `scripts/package.py` | build the distributable zip |

## Documentation

**Resuming work on this project? Start with [docs/PLAN.md](docs/PLAN.md).**

| | |
| --- | --- |
| [docs/PLAN.md](docs/PLAN.md) | **master plan and progress** — what is done, what is next, how to resume |
| [docs/SPEC.md](docs/SPEC.md) | the specification — every `§N` in the code points here |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | layers, contracts, dependency direction |
| [docs/DECISIONS.md](docs/DECISIONS.md) | every choice the spec left open, and why |
| [docs/PROFILES.md](docs/PROFILES.md) | writing a game profile — anatomy, the measured method, pitfalls |
| [docs/STYLE.md](docs/STYLE.md) | the Style Bible — what belongs in it, what stays in code, and why |
| [docs/ANALYTICS.md](docs/ANALYTICS.md) | outcome data — what it needs, what it stores, what it refuses to guess |
| [docs/BASELINE.md](docs/BASELINE.md) | the edit this system makes, measured — and the regression contract |
| [docs/P0_RESULTS.md](docs/P0_RESULTS.md) | before → after → delta for the editing upgrade, regression included |
| [docs/P1_RESULTS.md](docs/P1_RESULTS.md) | reading the joins between shots, and the metric that was measuring the wrong thing |
| [docs/P1_8_CLASSIFIER_AUDIT.md](docs/P1_8_CLASSIFIER_AUDIT.md) | why half of every moment is labelled `surprise`, and what that costs |
| [docs/P2_2_EVENT_BOUNDARIES.md](docs/P2_2_EVENT_BOUNDARIES.md) | the evidence that was being dropped, and locating the thing a moment is about |
| [docs/P2_4_SOUND_AUDIT.md](docs/P2_4_SOUND_AUDIT.md) | the sound hierarchy, measured off the rendered envelope rather than the constants |
| [docs/P2_1_REPLAY_ANALYSIS.md](docs/P2_1_REPLAY_ANALYSIS.md) | replay, analysed and deferred: two candidates in 254 clips |
| [docs/TUNING.md](docs/TUNING.md) | controlled tuning — the six guards, and how to turn it on |
| `docs/PHASE_N.md` | what each phase delivered, what it deferred, and the bugs it found |
| [docs/ASSESSMENT.md](docs/ASSESSMENT.md) | environment, dependencies, risks |

## Development

```bash
.venv/bin/python -m pytest              # 2671 tests (~42 min)
.venv/bin/python -m pytest tests/unit   # the unit belt alone (~5 min)
.venv/bin/python -m pytest -m "not slow"  # fast subset
.venv/bin/ruff check .
```

**Run the whole suite, not only `tests/unit`.** The integration suite was left
unrun between V2-P0 and V2-P8 and was red in four places by the time anyone
looked: a hook split into pieces failed the chronology check on every edit built
with one, a stage with no worker stranded a queued Reel behind it, and a
module-scoped fixture kept a media-vault restriction that made all twenty-one
render tests error at setup. None of them could fail the unit belt.

Test artefacts stay inside the repository: `pyproject.toml` pins
`--basetemp=.pytest-tmp`, so transcoded proxies and frame dumps never land in
the system temp directory. Both are gitignored.

Tests never touch the real `projects/` directory or database — every fixture is
rooted in a temporary directory.

## License

[MIT](LICENSE).
