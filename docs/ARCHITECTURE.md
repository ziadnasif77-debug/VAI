# Architecture

The five layers of SPEC §123 are the top-level seam. They are never merged into
one AI prompt, and each is replaceable without touching the others.

```
                        USER
                          │
             ┌────────────┴────────────┐
             │                         │
          VIDEO              INSTRUCTIONS / QUESTIONS / COMMANDS
             │                         │
             │                 ┌───────▼────────┐
             │                 │  INTERACTION   │   backend/interaction
             │                 │  intent · Q&A  │   (optional layer)
             │                 │  · commands    │
             │                 └───────┬────────┘
             │                         │  EditingIntent
             ▼                         ▼
   ┌──────────────────────────────────────────────────┐
   │ LAYER 1  UNDERSTAND                              │
   │ media · audio · speech · scenes · vision · OCR   │  backend/media
   │ · game events                                     │  backend/analysis
   └───────────────────────┬──────────────────────────┘  backend/gaming, ai/
                           ▼
   ┌──────────────────────────────────────────────────┐
   │ LAYER 2  DECIDE                                  │  backend/moments
   │ moments · scoring · context · narrative · duration│  backend/narrative
   └───────────────────────┬──────────────────────────┘
                           ▼
   ┌──────────────────────────────────────────────────┐
   │ LAYER 3  DESCRIBE                                │  backend/timeline
   │ EDL · timeline · captions · effects              │
   └───────────────────────┬──────────────────────────┘
                           ▼
   ┌──────────────────────────────────────────────────┐
   │ LAYER 4  RENDER                                  │  backend/rendering
   │ FFmpeg (cut/concat/encode) · Remotion (overlay)  │  remotion/
   └───────────────────────┬──────────────────────────┘
                           ▼
   ┌──────────────────────────────────────────────────┐
   │ LAYER 5  VERIFY                                  │  backend/qa
   │ technical QA · content QA · human review         │
   └───────────────────────┬──────────────────────────┘
                           ▼
                  FINAL VIDEO ARTIFACT
                           │
                  ┌────────▼────────┐
                  │   PUBLISHING    │  backend/publishing
                  │ local file now, │  (seam only; nothing
                  │ YouTube later   │   uploads automatically)
                  └─────────────────┘
```

Infrastructure sits below all of it and depends on none of it:
`backend/core` (errors, logging, ids, duration policy, cache keys, models),
`backend/config`, `backend/database`, `backend/pipeline`, `backend/services`.

---

## Contracts

These hold for every phase. A change that breaks one is an architecture change,
not a refactor.

| # | Contract | Why |
| --- | --- | --- |
| C-1 | **AI decides, renderers execute.** The timeline never names an engine; FFmpeg and Remotion consume it. | §64, §67 |
| C-2 | **The model never executes anything.** LLM output is a validated structure that ordinary code applies. No shell, no file writes, no FFmpeg strings. | §85, §93 |
| C-3 | **Sources are immutable.** Written once at import, read forever after. Every edit is a reference into them. | §42 |
| C-4 | **Analysis is chunked.** No stage loads a whole recording into RAM; an 8-hour source is bounded chunks with overlap. | §7 |
| C-5 | **Re-editing never re-analyses.** Changing duration, mode, the timeline or an instruction re-runs STORY→QA only. | §10, §127 |
| C-6 | **Cache identity is exact.** `video_hash + model_version + prompt_version + analysis_version`, built in one function. | §48, §49 |
| C-7 | **Answers are grounded.** Q&A reads stored records and cites them; before the data exists it refuses. | §12, §13, §17 |
| C-8 | **One model in VRAM at a time.** Load → process → unload → empty cache, then the next stage. | §52, §54 |
| C-9 | **Nothing leaves the machine on its own.** Publishing is always an explicit user action. | §50, §51 |
| C-10 | **The chat is optional.** With no instructions the default preset produces a finished video. | §1, §22 |

### Dependency direction

```
api → services → interaction → {moments, narrative, timeline} → core models
                              ↘ analysis/gaming → ai/providers
rendering → timeline            (never → ai)
publishing → rendering output   (never → pipeline)
core, config, database          (import nothing above them)
```

`backend/interaction` imports the pipeline. The pipeline never imports
`backend/interaction`: it reads a resolved `EditingIntent` and knows nothing
about chat.

---

## Sequential model execution

An RTX 3070 has 8 GB. Whisper large-v3 (~5 GB), a 7B VLM (~7 GB) and an NVENC
session do not coexist. The scheduler therefore keeps one model resident:

```
load speech  → transcribe → unload → empty cache
load vision  → analyse    → unload → empty cache
load llm     → decide     → unload → empty cache
encode with NVENC (or libx264 when no GPU)
```

`gpu.exclusive_model_slot`, `gpu.empty_cache_between_stages` and
`gpu.preflight_vram_check` in `config/models.yaml` express this, and
`jobs.max_concurrent` keeps stages serialised.

## Cascading analysis

A local VLM costs seconds per frame. A 2-hour recording sampled every 3 seconds
is 2400 frames — hours of inference. So cheap detectors nominate candidates and
only their keyframes reach the model:

```
audio spikes + frame difference + transcript + scene changes + HUD changes
                          ↓
                  candidate regions
                          ↓
              VLM confirms and reads (bounded per source-hour)
```

`analysis.vision.only_candidate_regions` and
`analysis.vision.max_frames_per_source_hour` enforce it.

## Two-pass rendering

Remotion rasterises every frame through Chromium: right for captions and motion
graphics, wrong for stitching twenty minutes of gameplay.

```
1. FFmpeg   cut + concat + transitions + speed + audio mix   → base.mp4
2. Remotion captions + graphics on a transparent canvas      → overlay.webm
3. FFmpeg   composite + final encode                         → output.mp4
```

Step 2 is skipped entirely when the timeline carries no overlay elements.

---

## Repository layout

```
backend/
├── api/           local HTTP surface (thin routers)
├── services/      use cases: projects, media ingestion, jobs, health
├── interaction/   editing intent, Q&A, commands, conversation   ← optional layer
├── pipeline/      stage workers                                  (Phase 2+)
├── media/         ffprobe, proxy, audio, frames                  (Phase 2)
├── analysis/      audio events, scenes, frame sampling           (Phase 3-4)
├── gaming/        game events, HUD, OCR, profiles                (Phase 5)
├── moments/       formation, scoring, dead time, repetition      (Phase 6)
├── narrative/     story, hook, pacing, duration optimizer        (Phase 7)
├── timeline/      EDL and timeline model                         (Phase 8)
├── rendering/     FFmpeg + Remotion adapters                     (Phase 9-10)
├── qa/            technical and content QA                       (Phase 11)
├── publishing/    delivery targets (local file now)
├── core/          errors, logging, ids, duration, cache keys, models
├── config/        typed configuration loader
└── database/      SQLite schema, migrations, repositories

ai/                provider interfaces: speech, vision, llm
profiles/          game profiles (generic + per-game)
remotion/          overlay composition project
apps/              web UI, renderer host
config/            the YAML files
```
