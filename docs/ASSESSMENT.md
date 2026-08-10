# Assessment — AI Gaming Video Editor

Repository and environment assessment for the **Gaming-First Local AI Video Editor**
specification (v1.0, 126 sections). Supersedes the earlier long-form-editor assessment;
that spec was replaced by the gaming spec before any code was committed.

| Field | Value |
| --- | --- |
| Date | 2026-08-10 |
| Repository | `ziadnasif77-debug/vai` |
| Branch | `claude/local-ai-youtube-editor-ixsrt8` |
| Spec | AI GAMING VIDEO EDITOR v1.0 |

---

## 1. Repository

Empty at the time of assessment: `.git` only, zero commits. Nothing to preserve, no
migration path to design. The §88 structure is created at the repository root rather than
inside a nested `gaming-editor/` directory (decision D-001).

## 2. Environment

Build host is **Ubuntu 24.04, x86-64, 4 vCPU, 15 GiB RAM, no GPU**. The product targets a
Windows machine with an **RTX 3070** (§54).

| Present | Version |
| --- | --- |
| Python | 3.11.15 |
| Node.js | 22.22.2 (npm 10.9.7, pnpm 10.33.0) |
| FFmpeg / FFprobe | 6.1.1 (installed this session) |
| SQLite (Python binding) | 3.45.1 |
| Rust / Cargo | 1.94.1 |
| Network | PyPI + npm reachable through the agent proxy |
| Disk | ~30 GiB writable |

| Absent | Consequence | Handling |
| --- | --- | --- |
| NVIDIA GPU, CUDA, `nvidia-smi` | §52/§54 GPU paths cannot execute here | Hardware profiles (§53) resolve to `LOW` when no GPU is detected; every GPU path has a CPU fallback and is unit-testable with the hardware absent |
| Whisper / Vision model / LLM runtime | Phases 3–5 cannot run against real models here | §13 requires a modular `AIProvider` layer anyway; deterministic fake providers back the tests, real providers are validated on the target machine |
| Tesseract / OCR engine | §25 OCR unverifiable here | Same provider-interface treatment |
| Real gameplay footage | §117 golden dataset cannot be built here | Synthetic FFmpeg-generated fixtures for pipeline mechanics; the golden dataset is a target-machine deliverable |

**Verdict:** Phase 1 (§98 Foundation) is fully implementable *and* fully verifiable in this
environment. Phases 2–4 are implementable and unit-testable here with real FFmpeg. Phases
5+ need the target machine for acceptance, not for construction.

## 3. Dependencies

Installed now (Phase 1 only): `pydantic`, `PyYAML`, `fastapi`, `uvicorn`, plus dev
`pytest`, `pytest-cov`, `httpx`, `ruff`. TypeScript + Vitest for the JS workspace.

Deferred with the phase that needs them: `av`/`opencv-python` (Phase 2, §11),
Whisper implementation (Phase 3, §14), `scenedetect` (Phase 4, §17), OCR engine
(Phase 5, §25), Remotion (Phase 9, §12), React/Vite (Phase 12, §57).

Deliberately **not** used: an ORM (hand-written SQL keeps §45's schema explicit and the
database free of pipeline logic); an FFmpeg wrapper library (§85 requires command lines to
be built by trusted application code from an explicit argument list).

## 4. Architecture mapped to the spec

The five layers of §123 are the top-level seam and are never merged into one prompt:

| Layer | §123 | Packages | Phase |
| --- | --- | --- | --- |
| 1 UNDERSTAND | video, audio, speech, vision, OCR, game events | `backend/media`, `backend/analysis`, `backend/gaming`, `ai/*` | 2–5 |
| 2 DECIDE | moments, scoring, context, narrative, duration | `backend/moments`, `backend/narrative` | 6–7 |
| 3 DESCRIBE | EDL, timeline, captions, effects | `backend/timeline` | 8 |
| 4 RENDER | Remotion, FFmpeg | `backend/rendering`, `remotion/` | 9–10 |
| 5 VERIFY | technical QA, content QA, review | `backend/qa`, `apps/web` | 11–12 |

Supporting infrastructure (`backend/core`, `backend/config`, `backend/database`,
`backend/pipeline`, `backend/services`) sits below all five and depends on none of them.

Contracts fixed in Phase 1 so later phases cannot violate them:

1. **AI decides, renderers execute** (§64). The timeline/EDL never names a rendering engine.
2. **The LLM never touches files or shells** (§85). It emits validated structured data that
   an application tool applies to project state.
3. **Non-destructive** (§42). Source media is written once at import and never again.
4. **Chunked analysis** (§7). No stage may load a whole recording into RAM; an 8-hour source
   is processed as bounded chunks with overlap.
5. **Re-edit without re-analysis** (§127). Analysis artefacts are keyed by
   `video_hash + model_version + analysis_version` (§48) and survive every re-plan.

## 5. Risks

| # | Risk | Mitigation |
| --- | --- | --- |
| R-1 | 8 GB VRAM cannot hold vision + speech + LLM together (§54) | One resident model at a time; hardware profiles (§53) select sampling density and quantization; benchmark-driven, not assumed |
| R-2 | 8-hour sources (§7) exceed RAM and any single process lifetime | Chunked analysis + per-stage persisted artefacts + resume from the failed stage (§47) |
| R-3 | Game event detection is the differentiator *and* the hardest part (§21) | Generic path first (§23): vision + OCR + audio + speech + temporal, no game profile required. Profiles are an accelerator added after the architecture is proven (§111) |
| R-4 | Duration optimizer is combinatorial, not a sort (§39) | Explicit optimizer stage with hard clamping to the 10–60 min band and QA re-verification of the rendered file |
| R-5 | "Highest score ≠ best clip" (§33) is easy to regress into | Narrative coherence and variety are first-class inputs to selection, and every selected moment carries an explainability record (§80) |
| R-6 | Detection quality is unmeasurable without annotated footage | §117 golden dataset + §118 precision/recall metrics are treated as deliverables, not optional extras |
| R-7 | No GPU / no models on the build host | Provider interfaces + fakes; acceptance tests re-run on the RTX 3070 machine |
| R-8 | Built on Linux, shipped on Windows | `pathlib` only, no shell strings, explicit UTF-8, Windows-safe filename sanitisation, tested |

## 6. Phase 1 plan (§98 Foundation)

Deliverables: repository, configuration, logging, database, project model, media model,
API, job system. Acceptance: **create project → import video → persist metadata**.

| Step | Deliverable |
| --- | --- |
| 1.1 | §88 tree, `pyproject.toml`, `package.json`, `.gitignore`, `README.md` |
| 1.2 | `config/*.yaml` + typed loader with environment overrides (§91) |
| 1.3 | `backend/core`: versions (§49), duration policy (§6), typed errors (§81), structured logging, ids, filesystem safety, cache keys (§48) |
| 1.4 | SQLite connection + forward-only migrator + all §45 tables |
| 1.5 | Project model (§43 directory layout, §59 import options) and media model |
| 1.6 | Job system (§46 stages, §81 statuses, §47 resume, §82 cancellation) |
| 1.7 | Media ingestion: validate, checksum, register, enqueue the stage chain |
| 1.8 | FastAPI: `POST /projects`, `POST /projects/{id}/media`, `GET /projects/{id}/status`, health |
| 1.9 | Tests at unit + integration level; `scripts/` for db init and environment doctor |

Out of scope for Phase 1 (each belongs to its own phase): ffprobe/proxy/frames (Phase 2),
Whisper (Phase 3), scene detection and vision (Phase 4), gaming events (Phase 5), moments,
narrative, EDL, Remotion, render, QA, UI.
