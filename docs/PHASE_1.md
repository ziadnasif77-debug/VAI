# Phase 1 — Foundation

SPEC §98. **Acceptance: create project → import video → persist metadata.**

Status: **complete and verified.** 414 tests pass, `ruff check` is clean, and
both `scripts/doctor.py` and `scripts/db_init.py` run on a machine with no GPU
and no AI models installed.

---

## Delivered

| §98 requirement | Where | Verified by |
| --- | --- | --- |
| Repository | §88 tree at the repo root | `docs/ARCHITECTURE.md` |
| Configuration | 13 YAML files + typed loader with env overrides | `tests/unit/test_config.py` (33 tests) |
| Logging | 5 channels, JSON, ambient project/job context | `tests/unit/test_core.py::TestLogging` |
| Database | SQLite, forward-only migrator, all §45 tables | `tests/unit/test_database.py` (24 tests) |
| Project model | §43 directory tree, `project.json`, versioning | `tests/unit/test_project_manager.py` (24 tests) |
| Media model | ingestion, checksum, dedup, containers | `tests/unit/test_media_ingestion.py` (22 tests) |
| API | `/api` — projects, media, jobs, health, interaction | `tests/integration/` (54 tests) |
| Job system | §46 stages, §81 states, §47 resume, §82 cancel | `tests/unit/test_jobs.py` (37 tests) |

Built beyond the §98 minimum, because each one fixes an architectural contract
that later phases would otherwise have to retrofit:

| Addition | Why now |
| --- | --- |
| **Interaction layer** (`backend/interaction`) | Editing intent, Q&A, commands, conversation, edit versions. Requested as an additive layer; building it after the pipeline would mean threading intent through every stage retroactively. 77 tests. |
| **Effects engine** (`backend/effects`) | 22-effect library, style profiles, three-level budget enforcement. Fixes the FFmpeg/Remotion split and §69's "never global" rule before either renderer exists. 30 tests. |
| **Publishing seam** (`backend/publishing`) | Local-file target now, YouTube later. Stages, tables, metadata model and config exist so a destination is one class. 22 tests. |
| **AI provider interfaces** (`ai/providers`) | §13 requires a swappable model layer. Defining it now stops Phase 3 from hardcoding a runtime. |

---

## Acceptance test

`tests/integration/test_api.py::TestPhase1Acceptance::test_full_phase_1_flow`
walks the §98 criterion end to end:

1. `POST /api/projects` → project created, §43 tree scaffolded, manifest written
2. `POST /api/projects/{id}/media` → file validated, hashed, registered
3. metadata survives a fresh read from the database
4. version provenance recorded (`application_version`, `analysis_version`, `schema_version`)
5. the stage chain is queued and starts at `IMPORT`

## Contracts locked in this phase

| Contract | Enforced by |
| --- | --- |
| 10–60 minute output band | One definition in code; config may narrow, never widen; SQL `CHECK` kept in sync by a test |
| Re-edit never re-analyses (§127) | `ANALYSIS_STAGES` / `EDIT_STAGES` split, tested on duration, mode and EDL changes |
| Crash resume (§47) | `RUNNING` jobs re-queued at startup; completed work preserved |
| Delivery is never automatic (§51) | `MANUAL_STAGES` excludes `EXPORT`/`PUBLISH` from every automatic path |
| No answer without data (§17) | `Answer.requires_analysis`; a confident answer with no evidence fails validation |
| No effect applied globally (§69) | Every effect attached to a moment, three budgets, rejections reported |
| The model executes nothing (§85) | Commands are validated structures; every subprocess call is an explicit argv with `shell=False` |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| ffprobe, proxy, audio, frame extraction | 2 — §126 orders ingestion (04) before FFprobe (05) |
| Whisper transcription | 3 |
| Scene detection, vision analysis | 4 |
| Game events, HUD, OCR, profiles | 5 |
| Moment formation and scoring | 6 |
| Story, hook, pacing, duration optimizer | 7 |
| EDL and timeline construction | 8 |
| Remotion overlay composition | 9 |
| FFmpeg render and audio mix | 10 |
| QA engine | 11 |
| Web UI | 12 |
| LLM fallback for unparsed instructions | with Phase 6's LLM provider |

The interaction layer's `INSTRUCTION → LLM` path is deliberately absent: the
rule-based parser reports zero confidence on text it cannot read, which is the
signal to escalate once a model provider exists.

---

## Environment notes

Built on Ubuntu with FFmpeg 6.1.1, no GPU, no models. Consequences:

* GPU, NVENC, Whisper, Ollama and OCR checks report **warning**, not failure —
  and `doctor.py` exits 0, because §52/§95 require the pipeline to run on CPU.
* NVENC is reported *unusable* rather than *available*: the encoder is compiled
  into this FFmpeg build but has no GPU behind it.
* Every acceptance test that needs a real model must be re-run on the target
  Windows / RTX 3070 machine before the MVP is signed off.

## Gate to Phase 2

Met: `pytest` green (414), `ruff check` clean, `doctor.py` honest on a bare
machine, `db_init.py` idempotent.

Phase 2 begins at §126 step 05: FFmpeg/FFprobe integration, then proxy and frame
extraction, with the acceptance criterion from §99 — *a 2-hour video analysed
without loading the whole file into RAM*.
