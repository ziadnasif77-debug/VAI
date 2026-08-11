# Phase 12 — The interface

SPEC §57–§63; §126 step 18. **Acceptance: a video can be made without touching
the terminal.**

Status: **complete and verified**, against the real 21-minute recording rather
than a fixture: imported, analysed, moments reviewed with their reasoning,
timeline edited, rendered, QA'd, and played back in the browser.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Editing endpoints (§57–§62) | `backend/api/routers/editing.py` | `test_editing_api.py` — 26 tests |
| Serving media (§50, §57) | `backend/api/routers/files.py` | `TestServingFiles` |
| The nine screens (§57) | `apps/web/src/` | The browser, on the real project |
| Chat panel (§63) | `apps/web/src/components/ChatPanel.tsx` | The interaction API |
| The job worker (§46, §47) | `backend/services/worker.py` | `test_worker.py` — 11 tests |
| One command (§9) | `scripts/serve.py` | Running it |

---

## The gap the browser found

Pointing the finished UI at the real project revealed something no test had:
**the API queues jobs and nothing ran them.** Pressing Render wrote a row and
the screen said "Rendering…" forever. Every test in this project had called
`PipelineRunner.run_project` directly, so the missing half was invisible.

`backend/services/worker.py` is that half — a loop that polls for queued work
and runs one job at a time. One at a time deliberately: §83 asks for resource
awareness, and on a single machine the honest form of that is not running two
FFmpeg encodes and a VLM at once. The user is waiting for one video.

With it, `scripts/serve.py` is the whole application: API plus worker, one
command.

### The race inside it

Recovery of interrupted jobs (§47) started life in `serve.py`, called before
the worker was started — which reads as careful and is not. Recovery re-queues
anything `RUNNING` on the premise that nothing can be executing yet, and the
moment recovery and the worker share a process that premise is a race.

It lost. A render was reset to "queued" while the worker was two clips into
cutting it, and the interface showed work that was plainly happening as not
started. Recovery now runs **on the worker thread, before its first poll**: one
thread owns a job's lifecycle, and the race cannot happen rather than being
unlikely. `test_no_job_is_left_claiming_to_be_queued_while_it_runs` watches for
exactly that contradiction.

---

## What the screens are for

**Dashboard (§58)** carries the machine's capabilities, not a settings page
nobody opens. Whether NVENC works and whether Remotion is installed change what
the finished video *is* — a missing overlay renderer means no captions (§95) —
and learning that after a twenty-minute render is too late. On this machine it
shows the NVENC warning with its remedy, which is how the driver problem became
visible in the first place.

**Analysis (§60)** draws the pipeline exactly as §60 writes it, and the reason
that shape works is three states rather than two. A stage that has never run is
not the same as one running: with only "done / not done", a twenty-minute
analysis looks identical to a stalled one.

**Moments (§61, §80)** is the screen where a ranking can be questioned. Every
row opens to the sentences that justify it and the ten §32 dimensions behind
its score. On the real recording the top moment reads: *"3 independent
detectors agreed on this (audio, microphone, scene)"*, *"Penalised: similar
moments appear elsewhere"*. A list sorted by a number nobody can interrogate is
a black box, and the moment someone disagrees they have no way in.

**Timeline (§62, §78)** redraws from each operation's response rather than
patching the row it changed, because an edit re-flows everything after it.
"Remove" is a toggle: the clip stays, greyed, and putting it back is a click.
That is also the honest picture of what the backend did — nothing was deleted.

**Preview (§57)** is where §78's human review actually happens. It is the only
judgement no test in this project can make.

**Export (§76–§79)** shows the difference between a technical failure that
stops publishing and a content warning that is a question for a person, with
the remedy §79 requires each to carry.

---

## Serving files is where local-first becomes a security property

The Preview screen needs a browser to play a file from disk. Two rules follow,
and both are tested:

**Nothing outside the project directory is servable.** Paths are resolved and
checked to be inside the project root. A request is a string from the network
even when the network is loopback, and `../../../Windows/win.ini` is a string.

**Range requests are answered properly.** Without `206 Partial Content` a
player downloads the whole file before it will scrub — on the 852 MB render
this produced, the difference between an interface that works and one that
appears frozen.

---

## Choosing a file by path, not by upload

A browser's file picker gives a sandboxed handle. This pipeline reads a
three-gigabyte recording from disk many times — proxy, audio, frames — so an
upload would copy gigabytes to reach a file already on the machine, and §42
keeps originals in place by default.

That is a real cost of running in a browser. The honest fix is a desktop shell
(the plan's open question), not an upload button that quietly duplicates every
recording.

---

## Acceptance, on the real recording

Through the interface only:

| Step | Result |
| --- | --- |
| Open the project | 21:12, 1920×1080 @ 60, 1.01 GB |
| Moments | 18, ranked, each with its reasoning |
| Timeline | 16 clips, 10:24 |
| Remove a clip | 16 → 15 clips, 10:24 → 10:11 |
| Restore it | back to 16 clips, 10:24 exactly |
| Render | 10.4 min, 852 MB, libx264 |
| QA | **blocked**: 3.2 s of frozen picture; 14 checks passed |
| Preview | plays at 1920×1080, seeking to a clip works |

QA blocking that render is the system working. It also warned about four menu
or loading screens in the edit — found through the real vision labels — which
is precisely the §77 judgement a person should make.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| Nothing consumed the job queue | Every action in the interface appeared to start work that never ran |
| Recovery raced the worker for a job it had just claimed | A render two clips in, reported as "queued", indefinitely |
| A database error while *polling* killed the worker thread | The queue stops moving and nothing says why |
| `save_edit` only updated positions | A split silently lost its second clip and wrote a row whose source span no longer matched its timeline span |
| Pinned dependency versions that do not exist (`vite@7.1.14`) | The install fails outright — which is better than the near miss it nearly was |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| Natural-language editing beyond the rule parser (§63) | Phase 13. The panel is here and speaks to the existing interaction layer; the LLM fallback is the next phase's subject |
| A desktop shell | The open question in the plan. A browser cannot pick a path, and this is the cost |
| Settings screen (§57) | Configuration is thirteen YAML files, and an editor for them is a bigger UI than the pipeline needed. The Dashboard shows what a user actually acts on |
| Effects and music editing (§62's last two items) | The timeline carries both; changing them wants a design conversation, not a form |
| Thumbnails on the moments screen | The frames exist on disk. Serving them is easy; laying out a grid that stays readable at 87 moments is not, and the reasoning matters more |

---

## Gate to Phase 13

Met: a video made end to end without a terminal, on real footage.

Phase 13 begins at §126 step 19: the LLM fallback for interaction (§63, §93,
§94). Its constraint is the one §63 states plainly — natural language **must
never directly modify files**; it modifies project state, through the same
validated command layer §85 already requires.
