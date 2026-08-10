# Specification — AI Gaming Video Editor

**The source of truth.** Every `§N` reference in the codebase points to a
section number here. Section numbering is fixed: sections are never renumbered
or removed, only amended.

| | |
| --- | --- |
| Version | 1.0 + addenda A, B, C |
| Product | Local-first AI Gaming Video Editor |
| Target | Windows 10/11, NVIDIA RTX 3070 (8 GB VRAM) |
| Output | YouTube 16:9, 1080p, 30/60 fps, **10–60 minutes** |
| Cloud | Not required. Optional and disabled by default. |

Progress against this specification is tracked in [`PLAN.md`](PLAN.md).

---

## Part I — Product (§1–§7)

**§1 Vision.** Not a video cutter. A local AI gaming editor that watches
gameplay, understands what happens, finds the moments that matter, understands
their context, chooses the best footage, builds a story, produces an editable
timeline, applies editing elements, and outputs a YouTube-ready video.

Input example: 3 hours of gameplay. User selects: type (Story / Best Moments),
duration (20 minutes), style (Fast-paced Gaming), output (YouTube 16:9, 1080p,
60 fps). The system does the rest.

**§2 Objectives.** Import long recordings · analyse locally without upload ·
analyse image, audio and speech · detect gameplay events · detect player
reactions · read HUD and game UI · find important moments · score each moment ·
detect dead time · detect repetition · preserve context · build a story · fix
the final duration · produce an EDL · apply cuts · apply transitions when
needed · zooms · speed ramps · freeze frames · captions · local music · audio
ducking · motion graphics via Remotion · render via FFmpeg · post-render QA ·
let the user review every decision · re-edit through natural language.

**§3 The core principle.** The system must **not** work like this: loudest
audio = best clip; a kill = an important clip; highest AI score = take it.
Instead:

```
Gameplay Event + Context + Visual Importance + Audio + Reaction
+ Novelty + Narrative Value + Entertainment  =  Moment Quality
```

**§4 Input scope.** Containers: MP4, MOV, MKV, WEBM, AVI — extensible. Source
configurations: gameplay only · gameplay + microphone · gameplay + webcam ·
gameplay + microphone + webcam.

**§5 Output.** Minimum: MP4, H.264, AAC, 16:9, 1080p, 30/60 fps. Future:
1440p, 4K, Shorts, TikTok, Reels — **not** in the MVP.

**§6 Duration.** `MIN = 10 minutes`, `MAX = 60 minutes`. Presets: 10, 15, 20,
25, 30, 40, 45, 50, 60. Custom values allowed inside the band.

**§7 Long sources.** Must handle 30 min, 1 h, 2 h, 4 h, 6 h, 8 h **without
loading the video into RAM**. Analysis is chunk-based with overlap where needed.

---

## Part II — Architecture and stack (§8–§13)

**§8 Overall architecture.**

```
USER → PROJECT MANAGER → MEDIA INGESTION
     → {VIDEO ANALYSIS, AUDIO ANALYSIS, SPEECH}
     → {VISION ENGINE, AUDIO ENGINE, WHISPER}
     → GAMING EVENT ENGINE → EVENT CORRELATION
     → MOMENT DETECTOR → MOMENT SCORING → NARRATIVE ENGINE
     → EDIT DECISION LIST → {CAPTIONS, AUDIO, EFFECTS}
     → REMOTION → FFMPEG → VIDEO QA → FINAL VIDEO
```

**§9 Frontend.** React + TypeScript + Vite. Electron/Tauri later. The first
version may run as a localhost web application.

**§10 Backend.** Python + FastAPI + Pydantic — the AI and video ecosystem is
significantly stronger there.

**§11 Video processing.** FFmpeg, FFprobe, PyAV, OpenCV. FFmpeg is the final
rendering backbone.

**§12 Programmatic composition.** Remotion, responsible for captions, motion
graphics, overlays, animated text, zoom effects, transitions, visual
compositions, intro/outro. FFmpeg remains the low-level media layer.

**§13 AI layer.** Must be modular. Do not hardcode one model.

```
AIProvider ├── LocalVisionModel ├── LocalLLM ├── LocalSpeechModel └── OptionalCloudProvider
```

The system must keep working when a specific model is replaced.

---

## Part III — Analysis (§14–§20)

**§14 Speech-to-text.** Local Whisper-family implementation. Requires
timestamps, word timestamps, segments, confidence where available. Whisper is
supporting evidence, not the sole intelligence layer.

**§15 Vision model.** Local multimodal VLM where hardware allows.
Responsibilities: scene understanding, game state, important visual events,
unexpected events, HUD interpretation, context, visual novelty. **The vision
model must not process every frame.** Instead: scene detection → keyframes →
candidate frames → vision analysis.

**§16 Frame sampling.** Base: 1 frame per 2–5 seconds. Increase on audio spike,
scene change, HUD change, motion spike, or a game-event candidate. For a
detected event, analyse pre-roll + event + post-roll at higher resolution.

**§17 Scene detection.** PySceneDetect or equivalent, for scene boundaries,
visual changes, screen-state changes, menu and gameplay transitions. **Scene
boundaries are supporting information, not automatic edit points.**

**§18 Audio analysis.** RMS, LUFS, peak, silence, speech, noise, transients,
spectral activity. Detect shouting, laughing, explosions, shots, major game
sounds, sudden silence, audio spikes.

**§19 Microphone detection.** When separate microphone audio exists, analyse it
independently — a game explosion and a player screaming carry very different
semantic values.

**§20 Player reactions.** Detect laugh, scream, surprise, anger, celebration,
fear, disappointment, confusion, excitement, and correlate them with gameplay
events.

---

## Part IV — Gaming intelligence (§21–§34)

**§21 Gaming events.** Kill · Multi Kill · Death · Near Death · Clutch ·
Victory · Defeat · Boss Fight · Boss Defeat · Objective · Objective Failure ·
Rare Loot · Rare Event · High Damage · Low Health · Escape · Chase · Outplay ·
Comeback · Fail · Funny Moment · Unexpected Event.

**§22 Game profiles.** `profiles/` with `generic/`, `fortnite/`,
`call_of_duty/`, `valorant/`, `cs2/`, `gta/`, `minecraft/`, `apex/`, `custom/`.
Each may define HUD, kill feed, health, score, objective, victory state, defeat
state, round state, known UI, OCR regions, event rules.

**§23 Unknown games.** Generic vision + OCR + audio + speech + temporal
analysis must still operate. **The application must not require a game profile.**

**§24 HUD detection.** health, armor, ammo, score, kill feed, timer, objective,
minimap, round, team status, boss health, victory, defeat. Confidence-based.

**§25 OCR.** For kill feed, victory, defeat, score, objectives, damage, timers,
items, player names. **Every OCR result must have a timestamp.**

**§26 Event schema.**

```json
{ "id": "evt_001", "type": "clutch", "start": 812.4, "end": 827.9,
  "confidence": 0.94, "importance": 0.91,
  "sources": ["vision", "audio", "ocr", "microphone"], "metadata": {} }
```

**§27 Event correlation.** When several detectors see the same instant, merge
them. Kill-feed change + weapon sound + "NO WAY" becomes one high-confidence
gameplay moment.

**§28 Moment formation.** Events group into moments: setup → enemy appears →
combat → kill → reaction → victory. One moment may contain several events.

**§29 Context expansion.** Every candidate moment gets pre-roll + main event +
post-roll (e.g. −20 s / +20 s). **The exact duration must be adaptive.**

**§30 Dead time.** Detect walking, waiting, loading, menus, inventory, travel,
AFK, long silence, repetitive farming, repeated failed attempts. Each segment
gets a `dead_time_score`. **Removed only when removal does not damage context.**

**§31 Repetition.** Detect repeated kills, deaths, attempts, jokes, situations,
reactions. Keep the strongest representative examples.

**§32 Moment scoring.** Dimensions: Gameplay · Visual · Audio · Reaction ·
Novelty · Skill · Emotion · Narrative · Context · Entertainment. Penalties:
Dead Time · Repetition · Low Confidence. Weights must be configurable.

**§33 Important principle.** The highest score is not necessarily the best clip.
The system must consider story, context, progression, variety and pacing.

**§34 Moment taxonomy.** EPIC · CLUTCH · FUNNY · FAIL · REACTION · SKILL ·
OUTPLAY · CHAOS · TENSION · SURPRISE · RAGE · COMEBACK · BOSS · VICTORY ·
DEFEAT · DISCOVERY · RARE.

---

## Part V — Narrative (§35–§39)

**§35 Video modes.** **Story** — hook, context, build-up, event, escalation,
climax, reaction, ending. **Best Moments** — strongest moments selected.
**Compilation** — moments grouped by type.

**§36 Story engine.** Optimises narrative coherence, not merely moment score. A
slightly weaker moment may be selected when it creates necessary context.

**§37 Hook engine.** The first seconds must give a reason to keep watching:
epic moment, clutch, funny reaction, unexpected event, or an outcome preview.
**The system must not invent narration.**

**§38 Pacing.** Depends on event frequency, clip duration, dead time, audio
intensity, visual intensity, reaction frequency, narrative position.

**§39 Duration optimizer.** Given a 3-hour source and a 20-minute target, find
the combination of moments closest to the target while maximising
entertainment + narrative + variety and minimising repetition + dead time.
**This is an optimisation problem, not simple sorting.**

---

## Part VI — Timeline and editing (§40–§45)

**§40 EDL.** Every editing decision becomes structured data.

```json
{ "type": "clip", "source": "video.mp4", "in": 812.4, "out": 827.9,
  "timeline_start": 0, "timeline_end": 15.5 }
{ "type": "zoom", "start": 4.2, "duration": 2.5, "scale": 1.15 }
```

**§41 Timeline model.** Video tracks · audio tracks · caption tracks · music
tracks · effects · overlays · transitions · markers.

**§42 Non-destructive editing.** The original video is never modified. All
edits are references: source media + EDL + project metadata.

**§43 Project structure.**

```
project/ ├── source/ ├── proxy/ ├── audio/ ├── frames/ ├── analysis/
         ├── events/ ├── moments/ ├── transcript/ ├── timeline/ ├── assets/
         ├── renders/ ├── previews/ ├── logs/ └── project.json
```

**§44 Database.** SQLite for the MVP. PostgreSQL later if multi-user deployment
is required.

**§45 Core entities.** `projects` · `media` · `media_tracks` ·
`analysis_jobs` · `scenes` · `frames` · `audio_events` · `transcript_segments` ·
`game_events` · `moments` · `timeline_clips` · `timeline_effects` · `captions` ·
`music` · `renders` · `qa_results`.

---

## Part VII — Execution (§46–§56)

**§46 Job system.** Long processing is asynchronous. Stages:
`UPLOAD → PROBE → PROXY → AUDIO → TRANSCRIPT → SCENE → VISION → GAME EVENTS →
MOMENTS → STORY → EDL → RENDER → QA`. Each stage is a job.

**§47 Resume.** A failure at VISION must not restart from UPLOAD. Resume from
the failed stage.

**§48 Caching.** Every expensive analysis is cached. `video_hash +
model_version + analysis_version` determines whether existing results can be
reused.

**§49 Model versioning.** Store `model_name`, `model_version`,
`prompt_version`, `analysis_version` for every AI-generated analysis. This
makes debugging possible.

**§50 Local-first.** 100 % local processing by default. No required cloud API.
Cloud providers may be optional.

**§51 Privacy.** Gameplay footage stays on the local machine by default. No
automatic upload. No telemetry containing video content.

**§52 GPU.** Detect the available GPU. CUDA for NVIDIA. CPU fallback must exist
where technically practical.

**§53 Hardware profiles.** LOW (CPU-focused) · MEDIUM (GPU inference, moderate
frame sampling) · HIGH (GPU + vision models + higher sampling).

**§54 RTX 3070 target.** Model quantization, batch control, frame sampling,
VRAM management, CPU/GPU scheduling. **Do not assume unlimited VRAM.**

**§55 Proxy video.** 1080p/720p proxy for UI preview and analysis. Original
media is used for the final render.

**§56 Preview frames.** Generate thumbnails for scenes, events, moments and
timeline clips — this makes review significantly faster.

---

## Part VIII — Interface (§57–§63)

**§57 UI screens.** Dashboard · Projects · Import · Analysis · Moments ·
Timeline · Preview · Export · Settings.

**§58 Dashboard.** Projects · recent videos · processing jobs · render status ·
storage · GPU status.

**§59 Import screen.** Video · game · target duration · mode · output
resolution · fps.

**§60 Analysis screen.** Live pipeline: `✓ Media ✓ Audio ✓ Speech ✓ Scenes
● Gaming Events ○ Moments ○ Story ○ Render`.

**§61 Moments screen.** Moment · timestamp · type · score · confidence ·
duration · preview. Filters: Epic, Funny, Clutch, Reaction, Fail, Victory.

**§62 Timeline screen.** Remove clip · restore clip · change duration · move
clip · split · merge · change effects · edit captions · change music.

**§63 Natural language editing.** Later feature: the user writes an instruction
and the LLM converts it into timeline operations. **It must never directly
modify files** — it modifies project state / EDL.

---

## Part IX — Rendering (§64–§75)

**§64 Remotion integration.** Remotion receives a deterministic project
description (EDL + captions + effects + assets + timeline) and renders the
composition. **Remotion must not decide which clips are good. AI decides;
Remotion executes visual composition.**

**§65 FFmpeg responsibilities.** Transcoding · proxy generation · audio
extraction · frame extraction · media probing · final encoding · muxing · audio
processing.

**§66 Remotion responsibilities.** Motion graphics · captions · animated
overlays · zoom · transitions · text · intro/outro · visual effects.

**§67 Rendering architecture.** `Source → EDL → Timeline → Remotion
Composition → FFmpeg → Final MP4`.

**§68 Effects engine.** Effects are declarative:
`{"effect": "zoom", "start": 12.5, "duration": 2, "scale": 1.12}`.
MVP effects: Zoom · Crop · Freeze · Speed · Fade · Text · Caption · Highlight.

**§69 Speed ramping.** Use selectively for epic moments, clutches, skill shots,
boss defeats. **Never apply globally.**

**§70 Freeze frames.** For an important kill, a funny frame, a reaction, a
final result — with an optional caption.

**§71 Caption system.** Standard subtitles · word highlighting · emphasis ·
animated captions. **Caption generation must use transcript timestamps.**

**§72 Audio mixing.** Final tracks: game · microphone · music · effects.
Priority: **Speech > Important Game Audio > Music**.

**§73 Music.** MVP uses local user-provided music. The system must not download
copyrighted music automatically.

**§74 Audio ducking.** Music drops when the player speaks and when important
game audio occurs; normal otherwise.

**§75 YouTube optimisation.** Configurable resolution, fps, codec, audio codec,
bitrate, aspect ratio. Expose a YouTube preset.

---

## Part X — Verification (§76–§85)

**§76 QA engine.** After rendering, inspect duration · resolution · fps ·
audio · video stream · black frames · frozen frames · missing media · caption
synchronisation · A/V synchronisation · render errors.

**§77 Content QA.** AI-based: unexpected blank screen · accidental menu
sections · broken sequence · extremely long silence · bad transition · caption
covering important HUD.

**§78 Human review.** AI-generated edit → user review → approve/edit → render.
**The system should never assume AI decisions are always correct.**

**§79 Confidence.** Every AI decision carries a confidence. Low-confidence
events are marked "Needs Review".

**§80 Explainability.** For every selected moment, show why: multi-kill
detected, player reaction detected, victory followed, high visual intensity, no
repetition. Important for debugging and user trust.

**§81 Error handling.** Every pipeline stage produces status · error ·
retry_count · duration · logs. Statuses: QUEUED · RUNNING · COMPLETED ·
FAILED · CANCELLED.

**§82 Cancellation.** The user can cancel analysis, render or export **without
corrupting the project**.

**§83 Resource management.** Monitor CPU, RAM, VRAM, GPU utilisation, disk,
temperature where available, and adapt the workload.

**§84 Disk management.** Long videos generate large intermediates. Provide
cache size, temporary files, proxy files, analysis cache and render cache with
cleanup controls.

**§85 Security.** No arbitrary shell commands from the LLM. The LLM operates
through a controlled tool layer: `LLM → validated command → application tool →
execution`.

---

## Part XI — Development process (§86–§95)

**§86 Claude Code's role.** Development agent: repository analysis,
implementation, refactoring, testing, debugging, documentation, architecture
enforcement. Not the runtime AI of the finished application.

**§87 Runtime AI vs development AI.** Claude Code builds the application; local
AI models operate it. **The finished application must not require Claude Code
to run.**

**§88 Repository structure.**

```
apps/{web,renderer}
backend/{api,pipeline,analysis,gaming,moments,narrative,timeline,rendering,qa}
ai/{speech,vision,llm,providers}
profiles/{generic,fortnite,valorant,...}
remotion/  tests/  docs/  scripts/
```

**§89 API.** `POST /projects` · `POST /projects/{id}/media` ·
`POST /projects/{id}/analyze` · `GET /projects/{id}/status` ·
`GET /projects/{id}/events` · `GET /projects/{id}/moments` ·
`POST /projects/{id}/generate-edit` · `GET|PUT /projects/{id}/timeline` ·
`POST /projects/{id}/render` · `GET /projects/{id}/render-status` ·
`GET /projects/{id}/qa`.

**§90 Analysis API.** Must support full analysis · partial analysis ·
re-analysis · model replacement · cancel · resume.

**§91 Configuration.** Central, e.g. `analysis.chunk_seconds`,
`analysis.frame_sampling_seconds`, `analysis.event_overlap_seconds`,
`video.default_resolution`, `video.default_fps`, `output.min_minutes`,
`output.max_minutes`. **No critical business rule scattered through source.**

**§92 Prompt architecture.** Prompts are versioned, under `prompts/{vision,
gaming,moments,narrative,qa}/`, each with version, purpose, input schema, output
schema.

**§93 Structured AI output.** AI returns JSON conforming to schemas. **Never
rely on uncontrolled prose for pipeline decisions.**

**§94 Validation.** Every AI output passes Pydantic validation. Invalid result:
reject → retry → fallback.

**§95 Fallback strategy.** Vision fails → OCR + audio + scene detection + game
profile. Speech fails → video + audio + vision. LLM fails → rule-based scoring.
**The system degrades gracefully.**

---

## Part XII — Scope and phases (§96–§112)

**§96 MVP.** `Import → probe → audio extraction → Whisper → scene detection →
frame sampling → vision analysis → generic gaming events → moment scoring →
best moments → target duration → EDL → FFmpeg render → QA`.

**§97 MVP explicitly excludes.** 20+ game profiles · advanced AI effects ·
automatic music generation · complex webcam editing · multi-camera editing ·
cloud collaboration · mobile app · automatic YouTube upload · training custom
models.

**§98 Phase 1 — Foundation.** Repository, configuration, logging, database,
project model, media model, API, job system.
**Acceptance: create project · import video · persist metadata.**

**§99 Phase 2 — Media Engine.** FFmpeg, FFprobe, proxy generation, audio
extraction, frame extraction, media validation.
**Acceptance: a 2-hour video analysed without loading the entire file into RAM.**

**§100 Phase 3 — Speech/Audio.** Whisper, timestamps, audio events, silence,
volume, reaction candidates. **Acceptance: transcript synchronised with source.**

**§101 Phase 4 — Vision.** Scene detection, keyframes, vision model, visual
embeddings if needed. **Acceptance: the system describes major visual changes.**

**§102 Phase 5 — Gaming Intelligence.** Generic gaming events, HUD candidates,
OCR, kill/death/victory candidates, reaction correlation.
**Acceptance: gameplay moments detected with timestamps.**

**§103 Phase 6 — Moments.** Formation, context expansion, scoring, repetition,
dead time. **Acceptance: ranked moments.**

**§104 Phase 7 — Narrative.** Story mode, best-moments mode, hook, progression,
duration optimisation. **Acceptance: 2-hour source → 20-minute coherent edit.**

**§105 Phase 8 — EDL.** Timeline, clips, tracks, effects, captions.
**Acceptance: the generated EDL reproduces the planned video.**

**§106 Phase 9 — Remotion.** Composition, captions, zoom, text, transitions,
overlays. **Acceptance: EDL → Remotion → rendered output.**

**§107 Phase 10 — Final Render.** FFmpeg encoding, audio mixing, muxing,
YouTube preset. **Acceptance: the final MP4 opens correctly in standard players.**

**§108 Phase 11 — QA.** Technical, audio, timing, caption, visual QA.
**Acceptance: bad renders are detected automatically.**

**§109 Phase 12 — UI.** Dashboard, import, analysis, moments, timeline,
preview, export. **Only after the core pipeline works.**

**§110 Phase 13 — Natural language editing.** LLM command interpreter; commands
become structured project operations.

**§111 Phase 14 — Game profiles.** Start with one game, then a Game Profile API,
then progressively more. **Do not create 10 profiles before validating the
architecture.**

**§112 Phase 15 — Quality improvement.** Collect false positives and negatives,
analyse user edits, improve scoring, prompts and profiles.

---

## Part XIII — Testing (§113–§119)

**§113 Levels.** Unit · Integration · End-to-End.

**§114 Unit tests.** Timestamp conversion · event merging · moment scoring ·
duration optimizer · EDL generation · timeline operations · configuration ·
validation.

**§115 Integration tests.** FFmpeg · Whisper · Vision · OCR · database ·
Remotion.

**§116 End-to-end.** A fixed test video with known gameplay: known events,
known moments, known approximate duration, valid EDL, valid MP4.

**§117 Golden dataset.** Real gameplay, manually annotated with important
events, boring segments, best moments, reactions and game state. This is the
benchmark.

**§118 Quality metrics.** Event precision/recall · moment precision/recall ·
false positive rate · false negative rate · target duration error · render
failure rate.

**§119 User quality metrics.** AI-selected moments accepted · AI-selected
moments deleted · AI-rejected moments restored · average manual edits.

---

## Part XIV — Success (§120–§126)

**§120 Success criteria.** 2-hour gameplay → 20-minute YouTube video generated
automatically with reasonable moment selection, context, pacing, valid audio,
valid captions and valid rendering, requiring only limited human correction.

**§121 Product rule.** The objective is not 100 % automatic editing. It is
**90 % automatic + 10 % human control** — a professional editor must always be
able to override AI decisions.

**§122 User workflow.** Open app → create project → import gameplay → select
game → select Story/Best Moments → select 10–60 minutes → Analyze → AI analyses
→ detects events → creates moments → ranks them → builds story → generates EDL
→ user reviews → user optionally edits → AI applies captions, zoom, speed,
transitions, music → Remotion composition → FFmpeg render → QA validates →
user exports MP4.

**§123 Five independent layers.**

```
1 UNDERSTAND  video · audio · speech · vision · OCR · game events
2 DECIDE      moments · scoring · context · narrative · duration
3 DESCRIBE    EDL · timeline · captions · effects
4 RENDER      Remotion · FFmpeg
5 VERIFY      technical QA · content QA · user review
```

**Never combine these responsibilities into one AI prompt.**

**§124 What makes this different.** A traditional editor: the user watches,
finds moments, cuts, edits, renders. This system: AI watches → understands →
detects gameplay events → evaluates moments → understands context → constructs
story → generates EDL → renders → verifies → user approves.

**§125 Final product definition.** A local-first AI gaming video editor that
ingests long gameplay recordings, understands gameplay events and player
reactions through multimodal analysis, identifies and ranks high-value moments,
removes repetitive and low-value footage while preserving narrative context,
constructs a coherent 10–60 minute YouTube gaming video, generates a
non-destructive editable timeline, applies captions and controlled visual
effects through Remotion, renders through FFmpeg, and automatically verifies the
result. The architecture must stay modular so the AI model, game, video length,
editing style, effects and rendering engine can each change without rewriting
the application.

**§126 Development order.**

```
01 Repository + Architecture   02 Configuration          03 Database
04 Media Ingestion             05 FFmpeg/FFprobe         06 Proxy/Frames
07 Audio Analysis              08 Whisper                09 Scene Detection
10 Vision Analysis             11 OCR                    12 Gaming Event Engine
13 Event Correlation           14 Moment Detection       15 Moment Scoring
16 Dead-Time Detection         17 Repetition Detection   18 Context Expansion
19 Duration Optimizer          20 Story Engine           21 Best-Moments Engine
22 EDL                         23 Timeline Model         24 Captions
25 Audio Mixing                26 Remotion               27 Effects
28 FFmpeg Final Render         29 QA                     30 Review UI
31 Timeline UI                 32 Natural Language Edit  33 Game Profiles
34 Performance Optimization    35 Golden Dataset         36 Quality Benchmarking
37 Packaging                   38 Production Hardening
```

**Do not start with a large UI.** Start with the pipeline: gameplay → analysis
→ events → moments → EDL → render. If that line does not work, no professional
interface will save the project.

**§127 Definition of done.**

```
INPUT    2–3 hour gaming recording
USER     game selected or auto-detected · Mode: Story · Target: 20 minutes
SYSTEM   analyse video · analyse audio · transcribe speech · detect scenes
         · analyse keyframes · detect gaming events · detect reactions
         · correlate events · build moments · score moments · remove repetition
         · remove unnecessary dead time · preserve context · build narrative
         · generate hook · optimise duration · generate EDL · generate captions
         · apply selected effects · generate Remotion composition
         · render with FFmpeg · run QA
OUTPUT   ~20-minute YouTube gaming video + editable project + timeline
         + moment list + analysis metadata + QA report
```

**The system must be able to regenerate the video after any edit without
re-analysing the original video from scratch. This is a fundamental design
point, not an optional optimisation.**

---

# Addendum A — YouTube Auto-Publish (planned)

Requested during Phase 1: YouTube auto-publish will be added later, and the
architecture must accommodate it **without rewriting the pipeline**.

**A-1** The pipeline ends at a *final video artifact*. Publishing is a separate
layer behind an interface.

**A-2** A destination is added by implementing one `Publisher` and registering
it. No change to analysis, moments, narrative, timeline or rendering.

**A-3** Publication is always an explicit user action. Nothing uploads
automatically (§51).

**A-4** Credentials never live in configuration or in models; a publisher
resolves them from the OS credential store at call time.

**A-5** Video metadata — title, description, tags, category, visibility,
chapters, thumbnail — is produced during narrative construction, not retrofitted
at publish time.

**Status:** seam complete. `EXPORT`/`PUBLISH` stages, `publications` and
`video_metadata` tables, `Publisher` protocol, `LocalFilePublisher`, and
`config/publishing.yaml` all exist. YouTube itself is not implemented.

---

# Addendum B — Interactive AI Editing Instructions & Q&A

Requested during Phase 1. An **additional layer above** the existing pipeline —
not a redesign of it.

```
Video + User Instructions/Questions → AI Editing Intent → (existing pipeline)
```

**B-1 Instructions are optional.** With no instructions the system uses a
default editing profile and produces a finished video.

**B-2 Natural-language editing brief.** "Make it fast and fun", "focus on
clutches and multi-kills", "don't use many effects", "keep the context before
each important event", "make it cinematic", "about 25 minutes", "remove
repetition and boring moments". **Do not turn these into a single model prompt.**
Instead: `User Instruction → Intent Parser → Structured Editing Intent`.

**B-3 Structured editing intent.** Typed and dynamic — e.g. target duration,
pacing, priority events, dead-time policy, context preservation, effects,
captions, style. The example is not fixed; the structure must be extensible.

**B-4 Editing presets.** Best Moments · Fast Gaming · Cinematic · Story Driven ·
Funny Gaming · Competitive · Minimal Effects. A preset plus additional
instructions merge into the final intent.

**B-5 Video Q&A.** "What are the best 10 moments?" · "Why did you choose this
clip?" · "What was the strongest clutch?" · "How many times did I die?" ·
"When was the best kill?" · "What was the funniest moment?" · "Show me the
comebacks" · "What did you exclude, and why?"

**B-6 Q&A must not re-analyse the video.** After analysis, events, moments and
scores are stored. A question is answered from that data:
`Question → query understanding → event/moment database → answer`.

**B-7 Video knowledge layer.** Scenes · events · moments · transcript · OCR ·
audio events · game events · scores · timeline · EDL become the source of truth
for Q&A.

**B-8 Timestamp questions.** "What happened at 12:34?" searches a context window
around that time across events, transcript, OCR and vision data.

**B-9 Commands that modify the edit.** "Delete the clip at 12:34" · "no clips
like this one" · "add this clip" · "make this event more important" · "make it
shorter" · "more funny moments".
`Command → parser → intent update → EDL update → re-render`.

**B-10 Editing must not re-analyse.** Deleting a clip does not re-run Whisper,
vision, OCR or scene detection. Only the EDL changes, then a re-render.

**B-11 Conversation context.** Each new instruction **modifies** the current
editing intent rather than discarding previous instructions.

**B-12 Explainability.** "Why this clip?" must be answered from the data —
score, clutch event, low health, multi-kill, audio spike, player reaction, high
visual activity. **Do not invent reasons that are not in the data.**

**B-13 Confidence.** When an answer is uncertain, say so. No hallucination.

**B-14 Chat architecture.** `interaction/{intent_parser, question_answering,
command_parser, conversation, editing_intent}`. Chat logic must not live inside
rendering, ffmpeg, remotion, vision or whisper.

**B-15 Three interaction types.** QUESTION · COMMAND · EDITING_INSTRUCTION.

**B-16 UI.** Project → Video · Editing Settings · Generate · AI Chat, with an
`Ask about your video…` box and example prompts.

**B-17 Chat must know the project state.** Before analysis completes, "what was
the best moment?" must answer *"Video analysis is not complete yet"* rather than
guessing.

**B-18 Chat must know the current EDL.** "How long is the video?" is computed
from the current EDL, not from the original recording.

**B-19 Versioning.** Every significant edit is versioned; "go back to the
previous version" restores the previous EDL without re-analysis.

**B-20 No unnecessary AI calls.** "How long is the video?" needs no AI. "What
was the best clutch?" uses stored data. "What happened at this moment?" uses
stored data first, and a VLM only if that is insufficient. Goal: **maximum
intelligence, minimum expensive inference.**

**B-21 Architecture.** `USER → {video, instructions/questions/commands} →
INTERACTION LAYER → {intent, Q&A, commands} → EDITING INTENT → CORE PIPELINE →
event database → moment engine → EDL engine → FFmpeg/Remotion → final video`.

**B-22 Most important condition.** This feature must not turn the project into a
chatbot. The product stays an AI gaming video editor; chat is a control and
query interface.

**B-23 Future capability.** "Make this more cinematic" · "use the style of my
previous project" · "shorten the intro" · "add this moment at the beginning" ·
"create a 10-minute version" · "create three different edits" · "compare these
two edits". Design for these; do not implement them prematurely.

**B-24 Independent of YouTube.** Chat, editing, EDL and rendering are not
coupled to any publishing destination.

**B-25 Implementation strategy.** Inspect the current repository, identify the
minimum change set, implement incrementally.

**B-26 Tests.** A question answered from event/moment data · an instruction that
changes the editing intent · a command that changes the EDL · a follow-up that
refines previous instructions · no invented answer before analysis completes ·
an EDL change that does not trigger re-analysis.

**B-27 Definition of done.** The user can enter gameplay, run analysis, write
natural-language editing instructions, ask questions about the video, learn why
the AI chose each moment, issue commands that modify the edit, change the EDL
without re-analysis, keep conversation context, re-render, and produce a final
video — **and the system must still work fully without chat.**

**Status:** implemented, except the LLM fallback (B-2 for unrecognised text and
B-20's VLM escalation), which lands with Phase 13.

---

# Addendum C — Effects Engine

Requested during Phase 1: a complete, detailed effects library for professional
gaming edits, as an independent engine.

**C-1 Library.** Zoom / Punch In · Camera Shake · Slow Motion · Speed Ramp ·
Freeze Frame · Flash · Blur · Glow · Motion Blur · Text Pop · Kill Counter ·
Highlight Box · Crosshair Effects · Impact Effects · Transitions · Cinematic
Bars · Dynamic Captions · Sound Effects · Meme/Funny Effects.

**C-2** The engine is independent: it decides *which effect goes where* from the
selected moments and the editing intent. Renderers decide how to realise it.

**C-3** Effects remain declarative data on the timeline (§68) and are never
applied globally (§69).

**Status:** implemented. 22 entries in `config/effects.yaml`, split between the
FFmpeg pass (effects that alter the footage) and the Remotion overlay pass
(effects drawn on top), with a deterministic planner and three levels of budget.
The renderers that execute them arrive in Phases 9–10.

---

# Addendum D — Engineering review (applied)

An engineering review of the plan raised four issues that changed the design.
All four are applied.

**D-1 VRAM bottleneck.** A VLM + Whisper large-v3 + hardware encoding on one
8 GB card causes CUDA OOM. Required: a strictly sequential worker system —
load model → process → save → free VRAM (`empty_cache`) → next model.

**D-2 Local VLM speed.** A 2-hour video sampled every 3 seconds is 2400 frames;
analysing them all with a local VLM takes hours. Required: cascading analysis —
lightweight heuristics first (audio spikes, frame difference via OpenCV,
transcript), and only the keyframes of a candidate region reach the VLM.

**D-3 Remotion vs FFmpeg.** Remotion renders every frame through Chromium —
excellent for captions and overlays, far too slow for stitching raw gameplay.
Required: FFmpeg cuts, concatenates, applies transitions and mixes audio;
Remotion renders overlays to a transparent intermediate that FFmpeg composites.

**D-4 Component choices.** `faster-whisper` (CTranslate2) for up to 4× speed at
lower VRAM · PaddleOCR/EasyOCR restricted to specific bounding boxes such as the
kill-feed region · a quantized local LLM via Ollama/llama.cpp · SQLite plus
local JSON artefacts for fast caching.
