# Decisions

Choices the specification left open, and why each one was made. SPEC §25 asks
for the smallest change set that satisfies a requirement; that is the standard
applied here.

---

**D-001 — The §88 tree lives at the repository root, not inside `gaming-editor/`.**
The repository is already the project. A nested directory would add a path level
with no benefit.

**D-002 — `backend/core`, `backend/config`, `backend/database`, `backend/services`,
`backend/media` and `backend/publishing` are added to the §88 layout.**
§88 is described as recommended and lists the domain packages. Configuration
(§91), SQLite (§44) and the job system (§46) still need somewhere to live.
Nothing listed in §88 was removed or renamed.

**D-003 — No ORM.** The §45 schema is fixed, and hand-written SQL keeps the
database free of pipeline logic. A ~150-line forward-only migrator covers what
Alembic would, and is itself tested. Revisit if the schema starts changing every
release.

**D-004 — No FFmpeg wrapper library.** §85 requires command lines to be built by
trusted application code. An in-repo builder with an explicit argument list is
more auditable than a general-purpose wrapper, and `shell=False` everywhere.

**D-005 — Duration limits live in two layers.** `ABSOLUTE_MIN/MAX_OUTPUT_SECONDS`
(600/3600) are product rules in code and are not configurable. `output.min_minutes`
/ `max_minutes` may *narrow* that band and are rejected at load time if they try
to widen it. This satisfies both §6 (hard limits) and §91 (nothing scattered).
The SQL `CHECK` in migration 0001 restates the numbers because SQL cannot import
Python; a unit test fails if the two ever disagree.

**D-006 — `EXPORT` and `PUBLISH` are pipeline stages but never auto-queued.**
YouTube auto-publish is planned. Adding the stages, the `publications` and
`video_metadata` tables, the `Publisher` interface and `config/publishing.yaml`
now means a future destination is one class plus configuration — no pipeline
change. `MANUAL_STAGES` keeps them out of automatic execution, which is also
what §51 requires.

**D-007 — Publishing metadata carries YouTube's limits from day one.** Title 100
chars, description 5000, 500 tag characters, chapters starting at 0. Applying
them to the local-file target too means nothing becomes invalid the day a real
destination is enabled.

**D-008 — Remotion renders an overlay, not the whole video.** Chromium-per-frame
is the right cost for captions and graphics and the wrong cost for stitching
gameplay. `remotion.mode: overlay` plus a two-pass FFmpeg render; the Remotion
pass is skipped when there are no overlays.

**D-009 — Vision runs on candidate regions only.** Sampling a 2-hour recording
every 3 seconds is 2400 frames; a local VLM would take hours. Cheap detectors
(audio spikes, frame difference, transcript, scene and HUD changes) nominate
regions, and `max_frames_per_source_hour` caps the work so analysis time stays
predictable.

**D-010 — OCR is region-restricted by default.** Reading the kill-feed and score
boxes a profile declares is cheaper and more accurate on stylised HUD fonts than
scanning a full frame. Full-frame scanning is the fallback for an unknown game,
at reduced resolution, on candidate frames only.

**D-011 — Speech defaults to `faster-whisper`.** CTranslate2 is markedly faster
than reference Whisper at lower VRAM, which matters on an 8 GB card. The
provider interface means the choice is configuration.

**D-012 — A stage counts as complete only when every job for it is complete.**
A project with three recordings is not "probed" until all three are. Otherwise a
downstream stage could start against partial input.

**D-013 — Import registers the file; PROBE reads it.** §98's acceptance is
"create project → import video → persist metadata", and §126 orders media
ingestion (04) before FFprobe (05). Import therefore persists file-level
metadata — path, size, checksum, container — and queues PROBE for the stream
metadata. No stage opens a video before Phase 2.

**D-014 — Media is referenced, not copied, by default.** Gameplay recordings are
tens of gigabytes. `copy_into_project` is opt-in; either way the original is
never modified, and `remove_media(delete_file=True)` only ever deletes a copy
the application itself made.

**D-015 — The interaction layer is additive and optional.** SPEC §22 is explicit
that this must not become a chatbot. `backend/interaction` imports the pipeline;
the pipeline never imports it and reads only a resolved `EditingIntent`. With no
instructions at all, the default preset takes a recording to a finished video.

**D-016 — Instructions accumulate as an ordered delta log.** §11 requires a
follow-up to refine rather than replace. Storing deltas rather than a rewritten
intent makes accumulation the default, keeps the user's words for auditability,
and lets the resolved intent be rebuilt at any time.

**D-017 — Parsing is deterministic first, LLM second.** §20 asks for maximum
intelligence at minimum expensive inference. "Delete clip 5", "make it 25
minutes" and "focus on clutches" are formulaic; rules read them instantly,
offline, and identically every time. An unrecognised message reports zero
confidence, which is the signal to escalate to an LLM when that lands.

**D-018 — Relative requests are resolved by the service, not the parser.**
"Shorter" and "faster" mean nothing without the current intent. The parser
reports a shift and stays stateless; the service applies it and clamps the
result into the duration band.

**D-019 — A vague delete is an instruction, not a command.** "Remove the boring
parts" changes the dead-time policy; "delete clip 5" removes a clip. Only exact
forms with a clip index, a timestamp or a version are treated as destructive.

**D-020 — Answers cite their records.** `Answer` rejects a confident
database-backed answer with no evidence, so §12's "do not invent reasons" is
enforced by the type rather than by discipline. Explanations are assembled from
the stored score breakdown, and before the analysis exists the answer says so.

**D-021 — Edit versions snapshot intent plus clip state.** §19's "go back to the
previous version" then costs a table read, never re-analysis.

**D-022 — The API returns one error shape.** FastAPI's default validation body
is `{"detail": [...]}`; every failure from this API instead carries
`error_code`, `message`, `details` and `recoverable`, and a violated duration
bound reports `INVALID_TARGET_DURATION` rather than a generic code.

**D-023 — `SafeExtraLogger` renames colliding log fields.** `extra={"name": ...}`
raises `KeyError` in the stdlib because `name` already exists on a `LogRecord`.
"Name", "module" and "filename" are ordinary words in this domain, and a logging
call must never take down the request it was describing.

**D-024 — Arabic is a first-class language.** Instructions, questions and
captions are written in it, so ruff's ambiguous-unicode rules (RUF001–003) are
disabled; they flag every Arabic letter as a Latin lookalike.

**D-025 — NVENC is reported unusable without a GPU.** The encoder being compiled
into FFmpeg is not enough — it is listed and then fails at runtime. Reporting
"available" would be a lie the first render exposes.
