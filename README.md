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

**The style decides what gets selected, not just what gets decorated.** Ask for
`gaming_fast` and you get twelve different moments than the house edit;
`cinematic`, `funny` and `minimal` each differ by ten, and no two of them cut
alike — measured before a single effect is placed. A style reaches the
optimiser as five bounded multipliers on the objective it already has, so the
optimiser's own code is untouched and still deterministic.

**Chronology is constitutional.** No engine may reorder events; a stronger
moment never precedes what happened before it. The single exception is the
cold-open hook at the start, and the rule is checked on the built timeline
rather than assumed from the inputs.

---

## Four styles, one recording

```bash
# the same session, four editorial decisions
"high energy"   → gaming_fast   shorter shots, dead time expensive, spectacle first
"cinematic"     → cinematic     longer shots, narrative first, silence tolerated
"funny"         → funny         variety high, repetition costly, the pause kept
"minimal"       → minimal       no decoration, and no editorial opinion either
```

The difference is in **selection and structure**, which is the part that makes
it an edit rather than a filter. The same doctrine then reaches pacing, audio,
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

## What it does not claim

This project is deliberately careful about the difference between a mechanism
and a claim.

**It does not learn from your audience — yet.** The plumbing exists: the
analytics scope, the fetcher, the tables, the join to the style that cut each
video. What does not exist is data. On the machine this was built for: four
published videos, **zero measured**, and an authorisation that predates the
analytics scope. Every surface says so rather than showing zeros.

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

**A style changes decisions, not appearances — and only where it is allowed
to.** The optimiser is the most delicate code here and it is deterministic,
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
| `scripts/doctor.py` | what is missing, and what the pipeline will fall back to |
| `scripts/db_init.py` | create the database |
| `scripts/config_coverage.py` | every YAML leaf, and whether any code reads it |
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
| [docs/TUNING.md](docs/TUNING.md) | controlled tuning — the six guards, and how to turn it on |
| `docs/PHASE_N.md` | what each phase delivered, what it deferred, and the bugs it found |
| [docs/ASSESSMENT.md](docs/ASSESSMENT.md) | environment, dependencies, risks |

## Development

```bash
.venv/bin/python -m pytest              # 2536 tests (~42 min)
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
