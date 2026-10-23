# VAI QA gap: progress state
RUN_LOCK: finished (run 6 started 2026-10-23T00:08Z)
BRANCH: claude/vai-qa-gap   BASE: b034df8   LAST_HEAD: c7feca9 (+ this state commit)
PHASE 0: DONE
BASELINE (stated in the routine): 2724 passed / 173 failed / 24 skipped / 9 errors
BASELINE (measured here, run 1, at fa601b4 = b034df8 + state files):
  2737 passed / 160 failed / 24 skipped / 9 errors (same 2930 tests).
  Per-test list: docs/progress/baseline_failures.txt (160 FAILED + 9 ERROR).
  13 fewer failures than stated; the environment differs slightly (the `ai`
  extra installed CPU-only from pyproject). Later runs diff against the file.
ITEM STATUS (TODO | IN_PROGRESS | DONE <hash> | BLOCKED <reason>)
- 3: DONE 07dc30a
- 1: DONE b50c17d
- 2: DONE d492e9a
- 4: DONE c7feca9
- 5: BLOCKED (TIME SOURCE undecided in DECISIONS: pick ffprobe creation_time,
     media.created_at + tiebreak, filename timestamp, file mtime, or explicit
     user order)

ALL ITEMS DONE OR BLOCKED ON A PERSON DECISION. Next run: nothing to do until
TIME SOURCE is set in the routine's DECISIONS (then do item 5).

TEST ENVIRONMENT (how run 1 measured; reproduce exactly)
- venv outside the repo: python3.11 -m venv /tmp/vai-env/venv;
  pip install "<repo>[dev,ai]"; then `pip uninstall vai-backend` (the
  editable finder would shadow worktrees; pytest's pythonpath="." suffices).
- env overrides (no config edits):
    VAI__APPLICATION__MEDIA_SOURCE_ROOTS='["/tmp/vai-env/media"]'
    VAI__DAILY__OUTPUT_DIRECTORY=/tmp/vai-env/out
    VAI__DAILY__ARCHIVE_DIRECTORY=/tmp/vai-env/archive
- pytest -q -p no:cacheprovider --basetemp=<outside repo> -rfEs  (~10 min)
- TypeScript: apps/web copied outside the repo, declared devDeps installed
  there (npm ci at the root fails: lockfile has a ppc64-only optional dep),
  `tsc --noEmit -p .` clean for items 3 and 2.

PER-ITEM REPORTS
Item 3 (07dc30a) NOT_MEASURED + gate adapter.
  Diff: QAStatus.NOT_MEASURED; QAGate PASS/WARN/NEEDS_REVIEW/BLOCK via
  report.gate_for() over existing blocks_export/needs_review (BLOCK exactly
  when blocks_export; not-measured only => NEEDS_REVIEW, never BLOCK);
  QaReport.not_measured/gate, summary() gains "gate" and "not_measured";
  qa_worker progress message no longer says "all checks passed" when a check
  was not measured; GET /qa parses not_measured rows and returns `gate`
  (its blocking rule unchanged); api.ts types; ExportScreen lists
  not-measured checks and never shows "All N checks passed" with one present.
  6 production files (allowance 6). No CSS needed. 315 lines (justified).
  Tests: tests/unit/test_qa_gate.py (87, incl. every pass/warn/fail combo
  under both policies: verdicts unchanged, gate follows them).
  Suite: 2824 passed / 160 failed / 24 skipped / 9 errors; 0 new failures.
Item 1 (b50c17d) caption-over-HUD profile lookup.
  Diff: QaWorker._profile reads every GAME_EVENTS job of the project (they
  carry media_id); one agreed profile is loaded and the overlap check runs;
  none / a recording without one / disagreement / load error =>
  caption_covers_hud NOT_MEASURED with reason + remedy. Generic profile still
  passes. ContentInputs.profile_unresolved (default None keeps old meaning).
  2 production files. Tests: tests/unit/test_qa_hud_profile.py (9; all 9
  failed before the fix).
  Suite: 2833 passed / 160 failed / 24 skipped / 9 errors; 0 new failures.
Item 2 (d492e9a) generate-edit re-judges the edit.
  Diff: generate-edit requeues EDIT_STAGES (adds QA, CRITIC2); clears QA's
  and CRITIC2's stored results ({}; Job.result is a dict) and deletes the
  project's critic2_snapshots in one transaction; GET /qa returns
  pending=true (no findings, gate needs_review) while QA is queued/running;
  api.ts `pending`, ExportScreen "pending" badge. PUBLISH/EXPORT/CRITIC2
  depend on QA completing, so nothing acts on the cleared result.
  3 production files. Tests: tests/integration/test_generate_edit_requeue.py
  (6; all failed before). Suite: 2839 passed / 160 failed / 24 skipped /
  9 errors; 0 new failures.
Item 4 (c7feca9) observe-only render checks.
  Diff: silencedetect in the decode pass at FFmpeg defaults (-60 dB, 2 s) =>
  "silence" observation; "timeline_duration" observation compares the file
  with timeline.duration (old "duration" check left as is); true peak parsed
  onto the loudness finding; unmeasurable loudness => NOT_MEASURED;
  "cut_inside_word" join audit from stored word timestamps (NOT_MEASURED with
  none). Category "observation"; measured => passed with "observed only;
  threshold TBD-from-data", never warn/fail/block. 3 production files.
  486 lines (justified: 162 production). Tests:
  tests/integration/test_qa_observations.py (13, real FFmpeg fixtures; check
  that observations never change status/blocks_export/needs_review).
  Suite: 2852 passed / 160 failed / 24 skipped / 9 errors; 0 new failures.

NOT RUN / NOT_MEASURED (every item)
- tests/golden/house_edit.json byte-identity: NOT_MEASURED here (no owner DB;
  test_house_edit_frozen skips). The person must run it locally. No item
  touches the edit pipeline (story/EDL/optimizer); changes are QA, the
  generate-edit endpoint and UI only.
- 12 owner-DB / real-model tests: not run (skipped), never counted as passed.

NOTES / SURPRISES
- Branch: the harness proposed claude/fervent-lovelace-8iomfi; the routine
  fixes claude/vai-qa-gap, which is used. Default branch
  claude/local-ai-youtube-editor-ixsrt8 is still at b034df8 (no rebase).
- Measured baseline differs from the stated one (see BASELINE).
- Item 3: my first API test depended on qa_results row order (random ids)
  and failed once in the full run; fixed before push (compares by check).
- Item 4: a 0.5-amplitude tone measured -3.1 dBFS true peak after AAC
  (ffmpeg's own sample/true peak agree), not the theoretical -6.
- Follow-ups NOT done (outside scope / file limits):
  * scripts/dashboard.py _show_qa prints "all checks OK" when nothing failed
    or warned, even if checks were not measured (explanation lines do list
    them). Needs a one-line change; item 3 was at its 6-file limit.
  * POST /render requeues QA too and keeps QA's old result (same stale
    verdict as item 2, different endpoint); GET /qa now shows pending there
    as well, but render-status/report read the old result until QA reruns.
  * generate-edit now raises (JobError) if QA or CRITIC2 is RUNNING when it
    is pressed, as it already did for STORY..RENDER.
  * qa_worker.py and content.py had pre-existing ruff-format drift; left as
    is (no new drift introduced).

RUN LOG
- run 1 (2026-10-22 18:30Z-19:16Z): init state, measured baseline, items
  3, 1, 2, 4 DONE; 5 BLOCKED (TIME SOURCE). Draft PR opened.
- run 2 (2026-10-22 20:08Z): nothing to do; 3/1/2/4 DONE, 5 still BLOCKED
  (TIME SOURCE still UNDECIDED). No code changes, suite not re-run.
- run 3 (2026-10-22 21:07Z): nothing to do; TIME SOURCE still UNDECIDED,
  item 5 still BLOCKED. No code changes, suite not re-run.
- run 4 (2026-10-22 22:05Z): nothing to do; TIME SOURCE still UNDECIDED,
  item 5 still BLOCKED. No code changes, suite not re-run.
- run 5 (2026-10-22 23:07Z): nothing to do; TIME SOURCE still UNDECIDED,
  item 5 still BLOCKED. No code changes, suite not re-run.
- run 6 (2026-10-23 00:08Z): nothing to do; TIME SOURCE still UNDECIDED,
  item 5 still BLOCKED. No code changes, suite not re-run.
