# Master Plan and Progress

**The file to read first when resuming work on this project, from any machine.**

| | |
| --- | --- |
| Product | AI Gaming Video Editor — local-first |
| Specification | [`docs/SPEC.md`](SPEC.md) — every `§N` reference in the code points there |
| Branch | `claude/local-ai-youtube-editor-ixsrt8` |
| Last updated | 2026-08-10, end of Phase 3 |
| Current phase | **Phase 3 complete and verified.** Next: Phase 4 (Vision) |
| Tests | 580 passing (2 opt-in model tests skipped by default) |
| Backend code | ~18,000 lines across `backend/` and `ai/` |

---

## 1. Resume here

```bash
git clone https://github.com/ziadnasif77-debug/VAI.git
cd VAI
git checkout claude/local-ai-youtube-editor-ixsrt8

python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip

.venv/bin/python -m pytest               # expect 580 passing (~5 min)
.venv/bin/python -m pytest -m "not slow" # the fast development loop
.venv/bin/ruff check .                   # expect clean
.venv/bin/python scripts/doctor.py       # what this machine is missing
.venv/bin/python scripts/db_init.py      # create/migrate the database
```

FFmpeg is required from Phase 2 onward. On Windows, in an **elevated** shell:

```
choco install ffmpeg-full -y
```

If `pytest` is green and `doctor.py` reports only warnings, the checkout is
healthy and **Phase 4 is the next work**. Start at §5 of this document.

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
| 4 | **Vision** | scene detection, keyframes, VLM analysis | ⬜ next |
| 5 | **Gaming Intelligence** | OCR, HUD, game events, correlation | ⬜ |
| 6 | **Moments** | formation, context expansion, scoring, dead time, repetition | ⬜ |
| 7 | **Narrative** | story / best-moments / compilation, hook, pacing, duration optimizer | ⬜ |
| 8 | **EDL & Timeline** | clips, tracks, effects placement, captions | ⬜ |
| 9 | **Remotion** | overlay composition | ⬜ |
| 10 | **Final Render** | FFmpeg encode, audio mix, YouTube preset | ⬜ |
| 11 | **QA** | technical + content verification | ⬜ |
| 12 | **UI** | dashboard, import, analysis, moments, timeline, export, chat | ⬜ |
| 13 | **NL editing (LLM)** | LLM fallback for unparsed instructions and questions | ⬜ |
| 14 | **Game Profiles** | one real game, then a profile API | ⬜ |
| 15 | **Quality** | golden dataset, precision/recall benchmarking, packaging | ⬜ |

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
| `backend/services` | `project_manager`, `media_ingestion`, `job_manager`, `health` | complete |
| `backend/interaction` | `models`, `intent`, `parser`, `knowledge`, `qa`, `store`, `service` | complete (LLM fallback pending Phase 13) |
| `backend/effects` | `models`, `library`, `planner` | complete (renderers pending Phases 9–10) |
| `backend/publishing` | `base` (Publisher protocol + registry), `local_file` | local target complete |
| `backend/api` | `app`, `dependencies`, `routers/` × 5 | complete for current scope |
| `ai/providers` | `base.py` — Speech / Vision / LLM protocols, registry | interfaces only |
| `backend/media` | `ffmpeg` (process layer), `probe`, `proxy`, `audio`, `frames`, `chunking` | complete |
| `backend/pipeline` | `runner`, `workers/` for IMPORT · PROBE · PROXY · AUDIO · FRAMES | complete for the media stages |
| `backend/analysis` | `signal` (streaming features), `audio_events` (§18), `reactions` (§19, §20) | audio complete; vision pending |
| `ai/speech` | `faster_whisper_provider`, `fake_provider`, factory | complete |
| `backend/{gaming,moments,narrative,timeline,rendering,qa}` | package docstrings only | **empty — this is the remaining work** |

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

Still to add: `POST /projects/{id}/generate-edit`, `GET|PUT /projects/{id}/timeline`,
`POST /projects/{id}/render`, `GET /projects/{id}/render-status`,
`GET /projects/{id}/qa`, `GET /projects/{id}/events`,
`GET /projects/{id}/moments`, `POST /projects/{id}/publish` (§89).

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

### Phase 4 — Vision (next)

**Goal (§15, §16, §17):** scene boundaries and frame understanding, cheaply.

**Create**
- `backend/analysis/scenes.py` — PySceneDetect wrapper.
- `backend/analysis/candidates.py` — the cascade: audio spikes + frame
  difference + transcript + scene/HUD changes → candidate regions.
- `ai/vision/ollama_provider.py` implementing `VisionProvider`.
- `backend/pipeline/workers/scenes.py`, `vision.py`.

**Acceptance:** the system describes major visual changes, and **the VLM sees
only candidate keyframes** — verify the frame count against
`analysis.vision.max_frames_per_source_hour`.

**Traps**
- This is where a naive implementation turns a 2-hour video into a 6-hour
  analysis. The cascade is not an optimisation; it is the design.
- Scene boundaries are supporting information, never automatic edit points (§17).

---

### Phase 5 — Gaming Intelligence

**Goal (§21–§27):** the product differentiator.

**Create**
- `backend/gaming/ocr.py` — region-restricted, PaddleOCR by default.
- `backend/gaming/hud.py` — confidence-based field detection.
- `backend/gaming/events.py` — event detection from all sources.
- `backend/gaming/correlation.py` — merge agreeing detectors (§27).
- `backend/gaming/profiles.py` — profile loader; `profiles/generic/` first.
- `backend/pipeline/workers/ocr.py`, `game_events.py`.

**Acceptance:** gameplay moments detected with timestamps, **without a game
profile** (§23). A profile improves accuracy; it is never required.

**Traps**
- Do not write ten game profiles before one is validated (§111).
- Every OCR result carries a timestamp (§25).
- Correlation must raise confidence, not multiply events: kill-feed change +
  weapon sound + "NO WAY" is *one* high-confidence moment.

---

### Phase 6 — Moments

**Goal (§28–§34):** ranked moments with defensible scores.

**Create**
- `backend/moments/formation.py` — group correlated events.
- `backend/moments/context.py` — adaptive pre/post roll, snap to boundaries.
- `backend/moments/scoring.py` — the ten dimensions of §32.
- `backend/moments/dead_time.py`, `repetition.py`, `variety.py`.
- `ai/llm/ollama_provider.py` implementing `LLMProvider`.
- `backend/pipeline/workers/moments.py`.

**Acceptance:** ranked moments, each with a stored `score_breakdown` and
`explanation` — the Q&A layer already reads both and will start answering
"why did you pick this?" for real.

**Traps**
- §33: the highest score is not the best clip. Variety and narrative value are
  inputs to selection, not afterthoughts.
- §30: dead time is removed *only when removal does not damage context*.
- Rule-based scoring must work when the LLM is unavailable (§95).

---

### Phase 7 — Narrative

**Goal (§35–§39):** a coherent video of the requested length.

**Create**
- `backend/narrative/story.py`, `best_moments.py`, `compilation.py`
- `backend/narrative/hook.py` — selects an existing moment, invents nothing (§37)
- `backend/narrative/pacing.py`
- `backend/narrative/optimizer.py` — **constrained optimisation, not a sort** (§39)
- `backend/pipeline/workers/story.py`

**Acceptance:** a 2-hour source becomes a coherent 20-minute edit within the
configured tolerance.

**Traps**
- The optimizer maximises entertainment + narrative + variety while minimising
  repetition + dead time, subject to a duration constraint. A greedy top-N sort
  will miss the target and produce a monotonous video.
- Clamp to the hard band at the EDL boundary as a last resort, and log a warning
  when that path is reached.

---

### Phase 8 — EDL and Timeline

**Goal (§40–§42):** the non-destructive description of the finished video.

**Create**
- `backend/timeline/models.py` — clips, tracks, transitions (engine-neutral)
- `backend/timeline/builder.py` — narrative plan → timeline
- `backend/timeline/operations.py` — split, trim, move, delete, restore
- `backend/timeline/validation.py` — timestamps, bounds, no gaps or overlaps
- `backend/timeline/captions.py` — from transcript timestamps
- `backend/pipeline/workers/edl.py`

**Acceptance:** the generated EDL reproduces the planned video exactly.

**Note:** the interaction layer's commands already write to `timeline_clips`;
this phase populates it in the first place. Re-check
`InteractionService._apply_timeline_command` against the real builder.

---

### Phase 9 — Remotion overlay

**Goal (§66, and decision D-008):** captions and motion graphics only.

**Create**
- `remotion/` — Remotion project, `OverlayLayer` composition, transparent canvas
- `backend/rendering/remotion.py` — write `composition.json`, invoke the render
- overlay renderers for the Remotion half of the effects library

**Acceptance:** EDL → Remotion → an alpha overlay that composites correctly.

**Traps**
- Overlay only. Rendering the gameplay through Chromium is the mistake this
  design exists to avoid.
- Skip the pass entirely when the plan has no Remotion-engine effects — the
  planner already reports that via `EffectPlan.for_engine()`.

---

### Phase 10 — Final Render

**Goal (§65, §72–§75):** the MP4.

**Create**
- `backend/rendering/ffmpeg_renderer.py` — cut, concat, transitions, speed
- `backend/rendering/audio_mix.py` — mix, ducking, normalisation (§72–§74)
- `backend/rendering/composite.py` — overlay + final encode
- `backend/rendering/encoder.py` — NVENC with libx264 fallback
- `backend/pipeline/workers/render.py`

**Acceptance:** the final MP4 opens in standard players; duration inside the
10–60 minute band.

---

### Phase 11 — QA

**Goal (§76, §77):** catch bad renders automatically.

**Create**
- `backend/qa/technical.py` — decode, duration, resolution, fps, streams,
  black/frozen frames, A/V sync, caption sync
- `backend/qa/content.py` — blank screens, accidental menus, broken sequence,
  extreme silence, captions covering HUD
- `backend/qa/report.py`
- `backend/pipeline/workers/qa.py`

**Acceptance:** a deliberately broken render is detected. Technical failures
block export; content warnings go to human review.

---

### Phase 12 — UI

**Goal (§57–§62):** the smallest interface that makes the pipeline usable.

`apps/web` — React + TypeScript + Vite against the local API. Screens:
Dashboard, Import, Analysis, Moments, Timeline, Preview, Export, and the Chat
panel (§16 of the interaction addendum: `Ask about your video…` with example
prompts).

**Only after the pipeline produces a convincing video** (§126).

---

### Phase 13 — LLM fallback for interaction

The rule-based parser already reports `confidence == 0.0` on text it cannot
read. This phase wires that signal to an LLM that returns a validated
`IntentDelta` or `EditCommand` — never free prose, never a file operation.

Also: LLM-assisted Q&A for questions the deterministic resolvers do not cover,
still grounded in retrieved records.

---

### Phase 14 — Game profiles

One real game end to end, then a profile API, then more (§111).

### Phase 15 — Quality

Golden dataset (§117), precision/recall metrics (§118), user-edit metrics
(§119), performance tuning, packaging.

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

### Target machine setup (do this before Phase 4)

Installed already: FFmpeg 9.0 (gyan.dev full build, NVENC present),
faster-whisper 1.2.1, PaddleOCR, numpy/scipy, Ollama. Remaining:

```powershell
ollama pull qwen2.5vl:7b        # Phase 4 vision model
ollama pull qwen2.5:7b-instruct # Phase 13 LLM
pip install scenedetect opencv-python
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
