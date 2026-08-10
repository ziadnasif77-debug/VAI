# Phase 8 — EDL and Timeline

SPEC §40–§45, §71; §126 step 14. **Acceptance: the generated EDL reproduces
the planned video exactly.**

Status: **complete and verified.** `ruff check` is clean, the EDL stage runs end
to end on a decoded recording, and the timeline survives a round trip through
the database and through every editing operation.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Timeline model (§40, §41) | `backend/timeline/models.py` | `TestClipModel`, `TestStructure` |
| Plan → timeline (§40) | `backend/timeline/builder.py` | `TestReproducesThePlan` — 7 tests |
| Operations (§42, §78) | `backend/timeline/operations.py` | `TestOperations` — 11 tests |
| Validation | `backend/timeline/validation.py` | `TestValidation` — 8 tests |
| Captions (§71) | `backend/timeline/captions.py` | `TestCaptions` — 10 tests |
| Persistence (§45) | `backend/database/repositories/timeline.py` | `TestPersistence` — 9 tests |
| EDL stage | `backend/pipeline/workers/edl_worker.py` | `test_edl_pipeline.py` — 21 tests |

`IMPORT → … → MOMENTS → STORY → EDL` now runs end to end. RENDER is the
frontier.

---

## Two coordinate systems, both stored

The defect this phase is arranged to prevent is confusing them:

`source_in` / `source_out`
: seconds **inside the original recording**, never shifted by editing.

`timeline_start` / `timeline_end`
: seconds **inside the finished video**, re-flowed whenever clips are added,
  removed or reordered.

Both are stored rather than one derived from the other, because both get asked
about — "which part of the recording is this" and "when does the viewer see
it". The invariant tying them together is checked by the model rather than
assumed: a clip whose source span cannot fill its timeline span is rejected at
construction, because the alternative is a renderer that runs out of source or
repeats frames, three minutes into an encode.

---

## The stage reads the plan; it does not re-derive it

The first version of the EDL worker rebuilt the narrative plan by re-running
the optimiser. That is wrong under §81, which makes the job result the contract
between stages. Re-running would usually agree — and the one time it did not, a
changed weight or a re-scored moment, the EDL would describe a **different
video from the plan the user was shown**, with nothing to indicate it.

So `build_timeline` takes a list of `PlannedClip`, and there are two adapters:
`clips_from_plan` for a plan held in memory, `clips_from_story_result` for the
stored job row. The pipeline uses the second.
`test_it_reads_the_stored_plan_rather_than_recomputing_one` edits the stored
plan and checks the EDL follows it.

---

## Other decisions worth knowing

### Deleting does not delete

`delete` disables a clip; the row stays, carrying its source references, so
"put that back" is a flag flip rather than a re-derivation nobody can guarantee
is identical (§78). `remove` exists for the caller that genuinely wants the row
gone, and is named differently on purpose.

That also decides what `Timeline.duration` means: enabled clips only. A
duration that counted footage the viewer never sees is a number the QA phase
would later check the render against.

### A rebuild preserves the user's decisions; a save overrides them

`replace(preserve_user_state=True)` is a **rebuild** — the pipeline re-running
must not re-enable a clip the user removed. `preserve_user_state=False` is a
**save** — the edit they just made is the decision, and preserving the old
state would revert it. Getting this backwards makes one of the two impossible,
which is why it is a parameter rather than a policy.

Both only work because clip ids are **derived**, not generated: the same plan
produces the same ids across runs, so stored references survive a re-run. That
is `derived_id` in `backend/core/ids.py`, added this phase.

### Effects are stored relative to their clip

The schema said so; the planner produced absolute timeline positions. Storing
what the planner produced would have been silently wrong the moment a clip
moved — every effect on every later clip pointing at the wrong second, with
nothing to notice it until someone watched the video. The repository converts
on write. An effect now travels with its clip through every reorder.

Captions stay absolute, because SRT and VTT want absolute times, so `save_edit`
shifts each caption by exactly its clip's delta instead.

### One key, one type, on every branch

§81 makes the job result the contract between stages, and a contract whose
field type depends on which branch wrote it is not one. The STORY stage
reported `clips` as a list of clips normally and as the count `0` when it had
nothing to select, so the first project with no moments took the EDL stage down
with a `TypeError` three frames deep. The producer now always writes a list;
the reader defends anyway, because §95 says degrade rather than crash and a
stage should stop its successor, not break it.

### The vocabulary imports without the application layer

The interaction package's docstring says the pipeline consumes an
`EditingIntent` and knows nothing else. Its `__init__` imported the service
eagerly, so touching that model loaded the whole application layer — and with
it the repositories, closing a cycle back through the effects library. It
surfaced only from `scripts/doctor.py`, which imports from a cold start; the
test suite never did.

`InteractionService` is now resolved on first attribute access (PEP 562). That
is the package matching what it already claimed about itself, not a workaround
for the cycle.

`tests/unit/test_imports.py` imports each package in a **fresh subprocess**,
because a warmed-up interpreter cannot answer "does this module import". It
also asserts that importing `backend.interaction.models` leaves
`backend.interaction.service` out of `sys.modules` — the boundary, checked
rather than commented. Both of this project's import cycles would have failed
it.

### Validation returns findings; it does not raise

A caller assembling an EDL wants every problem at once, not one per round trip.
`require_valid` raises for the callers that just need the timeline to be sound.
Gaps and overlaps are checked over **enabled** clips only, because a disabled
clip draws nothing and can neither leave a hole nor cover one.

### The last-resort duration clamp

§39 optimises towards the target and reports when it cannot reach it; §6's hard
band is enforced here, at the boundary, and only downward. Trimming happens at
the end of the programme and from the end of a clip — dropping the weakest clip
wherever it sits would undo the optimiser's variety work and leave a hole in
the middle of the arc. Reaching this path is logged as a warning, because it
means something upstream fell short.

Nothing pads a short edit. No amount of editing makes a short recording into a
long video, and padding would mean inventing footage.

---

## Acceptance

`test_timeline.py::TestReproducesThePlan` checks the criterion directly: every
planned clip becomes exactly one timeline clip, in the plan's order, with its
source span unchanged, summing to the planned duration, contiguous from zero,
and validating.

`test_edl_pipeline.py` runs the stage through the real pipeline on a decoded
recording — the stored clips reference the original file, the captions come
from real transcript timestamps, a re-run is repeatable, and a clip the user
disabled stays disabled across a rebuild.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| The EDL stage re-derived the narrative plan instead of reading it | The rendered video could differ from the plan the user approved, silently |
| Effects stored at absolute timeline positions against a schema documenting them as clip-relative | Every effect after an edited clip would point at the wrong second |
| `save_edit` updated `clip_index` row by row | `UNIQUE (project_id, track, clip_index)` fires the moment two clips swap places — every reorder raised |
| The interaction layer disabled clips with a raw `UPDATE` and no re-flow | "Delete clip 5" in chat left a hole in the video exactly the length of clip 5 |
| Captions did not move when their clip did | Captions drifting by the length of whatever was removed — §71's guarantee true at build time and false after the first edit |
| `validate` treated an unprobed media duration (`None`) as a number | `TypeError` on the one path where the caller passes probe metadata straight through |
| The STORY stage reported `clips` as a list normally and as the count `0` when it skipped | A project with no moments took the EDL stage down with a `TypeError`. One key, two types, depending on which branch produced it — that is not a contract (§81) |
| A second import cycle: `repositories.timeline` → `effects` → `interaction.models` → the interaction package's eager `service` import → back to `repositories.timeline` | `scripts/doctor.py` would not start. The suite stayed green, because it warms the modules in an order that hides it |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| Rendering anything | Phase 10. The timeline is engine-neutral (§67) and never names a renderer |
| Music and audio tracks | Phase 10's audio mix. §73 is local-music-only and there is no track to place yet |
| Burning captions in | Phase 9. Sidecar generation (`to_srt`, `to_vtt`) is here; drawing them is Remotion's |
| A bare `undo` command | The parser wants an explicit version. Flagged separately — it is an interaction-layer question, not a timeline one |
| `ClipRecord` / `TimelineClip` consolidation | The Q&A layer reads clips through its own record type. Two readers of one table is a wart, but merging them touches §59's answers and belongs with the UI work |

---

## Gate to Phase 9

Met: `ruff` clean, the acceptance criterion checked directly, and the stage
running end to end with its output surviving a database round trip.

Phase 9 begins at §126 step 15: the Remotion overlay (§66, decision D-008). Its
constraint is that Remotion draws **overlays only** — rendering gameplay through
Chromium is the mistake the architecture exists to avoid — and the pass is
skipped entirely when the effects plan has nothing for that engine, which
`EffectPlan.for_engine()` already reports and the EDL stage already stores.
