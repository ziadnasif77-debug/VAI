# Session ledger (autonomous engineering mission)

Append-only record of every change the scheduled mission makes. Newest at
the bottom. Every session reads the last three runs, every entry that is
"in progress", blocked or not pushed, and the latest closing entry before it
does anything else. When this file disagrees with git, git is the ground
truth and a correction entry is added here.

## [reconstructed] [2026-10-22 05:10, Europe/Oslo] [0]
- Backlog ID / title: BL-016 — history before the ledger existed
- Type: correction
- What changed and why: the ledger did not exist before this run. Reconstructed from `git log` of every mission branch and `ENGINEERING_BACKLOG.md`. Seven runs on 2026-10-21 each started a fresh branch from `b034df8`; the 21:07 run consolidated them onto `claude/great-feynman-vsj885` (BL-001…BL-007, BL-011, BL-012, BL-014 verified and implemented; ENG-010 ported). One fix exists only on `claude/dreamy-mccarthy-4cvvu9`: `e13c267` [P0.8] "A moment the sequence repair took out is never brought back" — not yet on the consolidated line.
- Files changed: none (record only)
- Evidence: `git log HEAD..origin/claude/<branch>` for all nine remote branches, run 2026-10-22 03:07 UTC
- Status: Verified and implemented (record)
- Commit hash: see the commit that adds this file
- Push: see the run's push log
- Known risks / limitations: earlier runs' test evidence is quoted from their reports, not re-run here.
- What this does NOT cover, next step: port `e13c267` (this run), keep one line of history from now on.

## [run-20261022-0307] [2026-10-22 05:40, Europe/Oslo] [1]
- Backlog ID / title: BL-019 — port P0.8 "A moment the sequence repair took out is never brought back" (`e13c267`, only on `claude/dreamy-mccarthy-4cvvu9`)
- Type: fix
- What changed and why: `repair_sequence` now remembers every moment a variety/flatness/arc repair removed and never offers it again from the bench. Before, variety and flatness could swap the same pair back and forth until the round limit decided the edit by parity. The fix was written in an earlier run on a branch that was never consolidated; ported unchanged.
- Files changed: `backend/narrative/optimizer.py`, `tests/unit/test_narrative.py`
- Evidence: new test `test_a_moment_taken_out_is_never_brought_back` fails on the old optimizer (1 failed) and passes with the port (1 passed). `test_narrative.py test_narrative_pipeline.py test_pacing_benchmark.py test_house_edit_frozen.py test_edl_pipeline.py`: 110 passed, 7 skipped (the benchmark/frozen-edit skips need the owner's database). `ruff check`: clean.
- Status: Verified and implemented (the house-edit benchmark could not be replayed here: no database)
- Commit hash: recorded in the push log of the run report
- Push: see the run report's push log
- Known risks / limitations: the frozen house edit on the owner's machine may change if the old ping-pong was live there; `test_house_edit_frozen.py` will show it.
- What this does NOT cover, next step: owner runs `pytest tests/integration/test_house_edit_frozen.py` on the machine with the database.

## [run-20261022-0307] [2026-10-22 06:05, Europe/Oslo] [2]
- Backlog ID / title: BL-017 — the "untriaged early failures" of the fast suite
- Type: test
- What changed and why: root cause found. On a host without the `ai` extra's `scenedetect`, the SCENES stage fails ("PySceneDetect is not installed") and every pipeline integration test then fails on `outcomes[JobStage.RENDER]` / `JobStage.STORY` with a bare `KeyError` (e.g. `test_qa.py::TestNothingToCheck::test_qa_skips_when_the_render_skipped`, 14 tests of `test_narrative_pipeline.py`). Not a code defect. `tests/conftest.py` now prints a report header naming any missing analysis package (scenedetect, numpy, cv2) and the install command, so the cause is visible at the top of the run.
- Files changed: `tests/conftest.py`, `tests/unit/test_suite_header.py` (new)
- Evidence: with scenedetect missing `test_qa.py`+`test_narrative_pipeline.py` fail with KeyError; after `pip install scenedetect==0.7.1` (pinned in `pyproject.toml [ai]`): 36 passed, 1 skipped. Unit suite file by file (scenedetect absent, all 77 files): every file passed, 397 s total — so the unit suite fits a run. New tests: 2 passed; header shown when scenedetect is uninstalled. `ruff check` clean.
- Status: Verified and implemented
- Commit hash: recorded in the push log of the run report
- Push: see the run report's push log
- Known risks / limitations: the header informs; it does not skip. Pipeline tests still fail without the package, deliberately (a green run without scene detection would be a false pass).
- What this does NOT cover, next step: the integration suite file-by-file timing is in this run's report.

## [run-20261022-0307] [2026-10-22 05:35, Europe/Oslo] [3]
- Backlog ID / title: BL-015 — `encoder.py` said `-sc_threshold 0` gives closed GOPs
- Type: docs
- What changed and why: the comment now says what the flag does (no extra keyframes at scene cuts, fixed GOP length), that it is libx264-only, and that h264_nvenc ignores it and still inserts scene-cut I-frames with rc-lookahead unless `-no-scenecut 1`. No behaviour change: adding `-no-scenecut 1` stays an owner decision until measured on NVENC.
- Files changed: `backend/rendering/encoder.py` (comment only)
- Evidence: local libx264 (FFmpeg 6.1.1), 4 s synthetic clip with a hard cut at 2 s, `-g 120 -keyint_min 120`: default sc_threshold → 2 I-frames, `-sc_threshold 0` → 1 I-frame (ffprobe `pict_type`). `ffmpeg -h encoder=h264_nvenc` lists `-no-scenecut` ("When lookahead is enabled, set this to 1 to disable adaptive I-frame insertion at scene cuts"). `test_rendering.py`: 62 passed; ruff clean.
- Status: Verified and implemented (the comment); NVENC behaviour itself remains "Verified finding awaiting implementation"
- Commit hash: recorded in the push log of the run report
- Push: see the run report's push log
- Known risks / limitations: none, comment only.
- What this does NOT cover, next step: on the RTX 3070, encode the same clip with and without `-no-scenecut 1` and count I-frames; then decide.

## [run-20261022-0307] correction [2026-10-22]
- Entries 1-2 above carry hand-written Oslo times that are out of order (05:40, 06:05 before 05:35). Their commits (`9cad4cb`, `252b523`, `07b0238`) give the true order: 1, 2, 3.

## [run-20261022-0307] CLOSING [2026-10-22, Europe/Oslo]
- Items completed and verified (IDs): BL-019 (`9cad4cb`), BL-017 (`252b523`), BL-015 comment (`07b0238`), ledger reconstruction (`7795512`); BL-018 upgraded from hypothesis to verified finding (no code)
- Items in progress (IDs, patch names): none
- Items blocked (IDs, exact reason): BL-008 (no GPU / Ollama / Whisper on the runner); BL-016 merge is the owner's
- Everything pushed? yes — every change pushed and confirmed; this closing commit is pushed last. Branch `claude/lucid-heisenberg-auaak8`, not `ai/gaming-video-auto`: the cloud session may push only its designated branch.
- Backlog changes: closed BL-017, BL-019 (new, closed); updated BL-015, BL-016, BL-018
- Top five next actions: (1) owner merges this branch, deletes stale mission branches; (2) BL-018 with the probed source matrix + QA tag check; (3) owner runs `test_house_edit_frozen.py`; (4) BL-015 measured on the RTX 3070; (5) integration timings from `test_narrative_pipeline.py` on. NEXT RUN: start from `origin/claude/lucid-heisenberg-auaak8`, not `b034df8`.

## [run-20261022-0624] correction [2026-10-22 08:35, Europe/Oslo] [0]
- Backlog ID / title: BL-016: a second mission line ran in parallel
- Type: correction
- What changed and why: start-of-session sync found that this session's branch `claude/lucid-heisenberg-repo6c` sat at `b034df8`. It was fast-forwarded to `origin/claude/lucid-heisenberg-auaak8` (`e1351a8`, the 03:07 run), the line this ledger describes. A second line, `origin/claude/lucid-heisenberg-5r6sxv` (`076bec6`, run "run-20261022-a", 04:33 UTC), also started from `b034df8` and kept its own ledger. Its M-1, M-3 and M-4 repeat BL-001, BL-006 and BL-003. Its one fix not on this line is M-2 (`2ffbf1e`), ported in this run (entry 1). That branch is not merged here because both lines rewrote the same files and ledger, so the merge conflicts.
- Files changed: none (record only)
- Evidence: `git log HEAD..origin/claude/lucid-heisenberg-5r6sxv` (7 commits), `git show 2ffbf1e`
- Status: Verified and implemented (record)
- Commit hash: see push log of the run report
- Push: see push log of the run report
- Known risks / limitations: each new cloud session gets a fresh branch from `b034df8`. Until the owner merges a mission branch into the default branch, parallel lines can keep repeating fixes.
- What this does NOT cover, next step: owner merges this branch; 5r6sxv's open M-5..M-10 items are copied into the backlog as BL-020 to BL-022 where they are not duplicates.

## [run-20261022-0624] [2026-10-22 08:40, Europe/Oslo] [1]
- Backlog ID / title: BL-020: port M-2 "A drive-letter source root allows nothing on a system without drives" (`2ffbf1e`, only on `5r6sxv`)
- Type: fix
- What changed and why: `_require_allowed_root` resolved each allowed root with `Path(root).resolve()`. On Linux, `D:/vault` is a relative path, so it resolved to a folder under the working directory, and a folder with that literal name there would pass the owner's exclusive-source rule. Roots that are not absolute on the running system are now skipped, so they allow nothing. Windows behaviour is unchanged.
- Files changed: `backend/services/media_ingestion.py`, `tests/unit/test_media_ingestion.py`
- Evidence: `pytest tests/unit/test_media_ingestion.py` baseline 29 passed, 1 skipped. With the new test but without the fix: 1 failed. With the fix: 30 passed, 1 skipped (the skip is a Windows-only test). `ruff check` clean.
- Status: Verified and implemented
- Commit hash: see push log of the run report
- Push: see push log of the run report
- Known risks / limitations: none known.
- What this does NOT cover, next step: none.

## [run-20261022-0624] [2026-10-22 08:55, Europe/Oslo] [2]
- Backlog ID / title: BL-018: a BT.601 recording is cut as BT.709
- Type: fix
- What changed and why: the render now probes each recording once. If the recording is tagged BT.601 (`bt470bg` or `smpte170m`), its segments get `in_color_matrix=bt601:out_color_matrix=bt709` in the same scale pass and are written with BT.709 tags. Untagged and BT.709 recordings are not touched (swscale reads untagged input as 601, so converting everything would shift normal captures). Converted segments get a `-709` name suffix, so a segment cut before this change is never reused as a converted one. Found while testing: FFmpeg 6.1.1's `vf_scale.c` does not put the output matrix into the frame's colour tag (master does, `out->colorspace = outlink->colorspace`), and `ffmpeg_enc.c` copies the encoder's tags from the frame. So the tags must be written explicitly. The previous run's note "the conversion also tags" was true only for master.
- Files changed: `backend/rendering/ffmpeg_renderer.py`, `tests/integration/test_colour_matrix.py` (new)
- Evidence: new tests (synthetic #FFD400, lossless x264, FFmpeg 6.1.1). Without the fix: 2 failed, 1 passed (programme still tagged `bt470bg`; read as BT.709 the green channel is off by 10). With the conversion but no explicit tags: 1 failed (tag). With the fix: 3 passed. Regression: `test_timeline.py test_rendering.py test_time_warp.py test_colour_matrix.py` 208 passed; `test_media_engine.py` 32 passed (198 s). `ruff check` clean.
- Status: Verified and implemented (programme video). The final encode with the overlay and a QA tag check are not covered.
- Commit hash: see push log of the run report
- Push: see push log of the run report
- Known risks / limitations: primaries are relabelled, not converted (SMPTE-C/EBU vs BT.709 differ very little). One extra ffprobe per recording per render. Full-range (`pc`) 601 sources are not handled specially.
- What this does NOT cover, next step: check that the final encode (overlay composite, `encoder.py`) keeps the BT.709 tag, then add a QA `colour_tags` check (new check name and config key).

## [run-20261022-0624] [2026-10-22 09:00, Europe/Oslo] [3]
- Backlog ID / title: BL-018 follow-up: the finished video keeps BT.709
- Type: test
- What changed and why: entry 2 left one question open: does the final encode (`composite`) keep the BT.709 tag? `ffmpeg_enc.c` sets the encoder's colour tags from the first frame, so a converted programme should come out BT.709. Now measured.
- Files changed: `tests/integration/test_colour_matrix.py`
- Evidence: new test `test_the_finished_video_keeps_the_bt709_tag` (no overlay, no mix, libx264): passes on the current renderer, fails with the renderer from before `8c32e51` (1 failed). File: 4 passed.
- Status: Verified and implemented (without an overlay. A composite with a Remotion overlay needs the Remotion toolchain, which this runner lacks)
- Commit hash: see push log of the run report
- Push: see push log of the run report
- Known risks / limitations: the overlay path was not run here.
- What this does NOT cover, next step: a QA `colour_tags` check that warns when the final file is not tagged BT.709 (needs a new config key in `config/qa.yaml`).

## [run-20261022-0624] [2026-10-22 09:10, Europe/Oslo] [4]
- Backlog ID / title: BL-018 (closing part): QA `colour_tags` check
- Type: feature
- What changed and why: technical QA now reads the finished video's colour matrix from the probe it already runs. A file tagged BT.601 (`bt470bg`/`smpte170m`) gets a warning. That tag means a step skipped the conversion, and the BT.709 captions would show shifted colours. It is a warning, not a failure, because the video still plays. BT.709 and untagged files pass, since players read untagged HD video as BT.709. The check is on by default (`enabled_checks` runs any check missing from the config) and is listed in `config/qa.yaml`. It needs no extra decode pass.
- Files changed: `backend/qa/technical.py`, `config/qa.yaml`, `tests/integration/test_qa.py`
- Evidence: 3 new tests (601 tag warns and does not block; 709 passes; untagged passes). Without the check: 3 failed. With it: 3 passed. `test_qa.py test_config_coverage.py test_config.py test_qa_content.py`: 69 passed. `ruff check` clean.
- Status: Verified and implemented
- Commit hash: see push log of the run report
- Push: see push log of the run report
- Known risks / limitations: a recording tagged 601 that is not actually 601 inside would now be converted at cut time (entry 2) and pass QA. That trusts the tag, the same as every player does.
- What this does NOT cover, next step: BL-018 is closed. A composite with a Remotion overlay on a 601 source should still be looked at once on the owner's machine.
