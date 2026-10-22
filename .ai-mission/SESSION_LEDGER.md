# Session Ledger — Gaming Video AI engineering mission

Append-only. Newest at the bottom. Never edit or delete an entry; add a
`correction` entry instead. Every new session reads the last three runs, every
entry that is in progress / blocked / not pushed, and the latest CLOSING entry
before doing anything else. Git history is ground truth when they disagree.

Times are Europe/Oslo.

---

## [run-20261022-a] [2026-10-22 06:07, Europe/Oslo] [0 — baseline, reconstructed]
- Backlog ID / title: BASE-0 / ledger created; history reconstructed from git
- Type: docs
- What changed and why: First run of the mission. No `.ai-mission/` existed.
  Prior history (50 commits visible in this clone, last `b034df8` 2026-10-21)
  is the owner's own phased work recorded in `docs/PLAN.md`; current phase
  P0.6 awaits the owner's viewing and the DATASET GATE (owner labels).
  Branch: the harness pins this session to `claude/lucid-heisenberg-5r6sxv`;
  the mission prompt's `ai/gaming-video-auto` was NOT used because the
  session's branch rule forbids pushing elsewhere without explicit permission.
- Files changed: `.ai-mission/SESSION_LEDGER.md`, `.gitignore`
- Evidence: baseline `pytest -m "not slow"` at HEAD `b034df8` on Linux
  (Python 3.11.17, FFmpeg 6.1.1, 4 vCPU, no GPU): collection stops with
  `ConfigurationError` (shipped `D:/...` paths rejected on non-Windows); with
  `--continue-on-collection-errors`, partial run to 58%: 801 passed, 831
  errors, 19 failed, 5 skipped (stopped to free CPU).
- Status: Verified and implemented
- Commit hash: see CLOSING entry
- Push: see CLOSING entry
- Known risks / limitations: no GPU here, so NVENC/VRAM work cannot be measured.
- Next step: fix the config-load failure so the suite can run at all.

## [run-20261022-a] [2026-10-22 06:20, Europe/Oslo] [1]
- Backlog ID / title: M-1 / The shipped configuration loads on non-Windows systems
- Type: fix
- What changed and why: `daily.output_directory`/`archive_directory` and
  `application.media_source_roots` were validated with `Path.is_absolute()`;
  on Linux `D:/Gaming 2026` is relative, so the shipped config raised
  `ConfigurationError` and the whole test suite stopped at collection. Both
  validators now also accept a `PureWindowsPath` absolute path; the
  system-drive refusal reads the Windows drive too.
- Files changed: `backend/config/schema.py`, `tests/unit/test_config.py`
- Evidence: `pytest tests/unit/test_config.py` — without the fix 10 failed /
  10 errors (new `TestDailyDirectories` incl. the C: refusal on Linux); with
  it all pass. `ruff check .` clean.
- Status: Verified and implemented (full suite result in CLOSING)
- Commit hash: see CLOSING entry
- Push: see CLOSING entry
- Known risks: a Windows root on Linux now passes validation — paired with M-2
  so it cannot widen the ingestion allowlist.
- Next step: none for this item.

## [run-20261022-a] [2026-10-22 06:22, Europe/Oslo] [2]
- Backlog ID / title: M-2 / A drive-letter source root names nowhere on another system
- Type: fix (security hardening of the owner's exclusive-source rule)
- What changed and why: `_require_allowed_root` resolved each root with
  `Path(root).resolve()`; on Linux `D:/vault` resolves under the working
  directory, so a folder literally named `D:/vault` there would be importable.
  Roots that are not absolute on this system are now skipped (allow nothing).
- Files changed: `backend/services/media_ingestion.py`,
  `tests/unit/test_media_ingestion.py`
- Evidence: new test `test_a_drive_letter_root_on_another_system_allows_nothing`
  fails without the fix (import succeeded), passes with it; the whole file
  passes (31, 1 Windows-only skip).
- Status: Verified and implemented
- Commit hash: see CLOSING entry
- Push: see CLOSING entry
- Known risks: none known; Windows behaviour unchanged (test skipped on nt).
- Next step: none.

## [run-20261022-a] [2026-10-22 06:24, Europe/Oslo] [3]
- Backlog ID / title: M-3 / The mix limiter's ceiling is a ceiling (alimiter auto level off)
- Type: fix (audio quality, §72/§74)
- What changed and why: FFmpeg's `alimiter` has `level` (auto level) on by
  default, which multiplies the output by 1/limit. The mix wrote
  `alimiter=limit=X` without it, so the configured ceiling was not respected:
  measured on FFmpeg 6.1.1 with synthetic pink noise, a -6 dB limit peaked at
  0.00 dBFS, and the shipped loudnorm(TP -1) + limiter(-0.3) chain peaked at
  -0.70 dB instead of -1.00. Now `alimiter=limit=X:level=0`. The option exists
  since FFmpeg 4.2 (doxygen source), so the target's FFmpeg 9.0 has it.
- Files changed: `backend/rendering/audio_mix.py`,
  `tests/integration/test_audio_mix_levels.py` (new, runs real FFmpeg),
  `tests/unit/test_rendering.py`
- Evidence: both new integration tests fail before (peaks 0.00 / -0.70 dB),
  pass after (≤ -6.0 / ≤ -1.0 dB, tolerance 0.1). `test_rendering.py` 60 pass.
- Status: Verified and implemented
- Commit hash: see CLOSING entry
- Push: see CLOSING entry
- Known risks: programme ~0.3 dB quieter than before (it was 0.3 dB over the
  configured target). `backend/rendering/sfx.py` recipes still use auto level
  (generated stingers normalise to 0 dBFS) — left alone, backlog M-6.
- Next step: M-5 (loudnorm single-pass misses the target, measured below).

## [run-20261022-a] [2026-10-22 06:26, Europe/Oslo] [4]
- Backlog ID / title: M-4 / The proxy's libx264 fallback no longer inherits NVENC's preset
- Type: fix (§95 graceful degradation)
- What changed and why: shipped `config/rendering.yaml` proxy is
  `h264_nvenc` / `p4`. When `resolve_codec` finds NVENC unusable it returns
  `libx264`, but `codec_arguments` passed `-preset p4` through and x264 exits
  "invalid preset 'p4'": every import on a machine without working NVENC
  failed at the proxy (found when the integration suite ran here, no GPU).
  An NVENC-style `pN` preset now maps to `veryfast` (the schema's software
  default) on software encoders.
- Files changed: `backend/media/proxy.py`, `tests/unit/test_media.py`
- Evidence: captured FFmpeg stderr `x264 [error]: invalid preset 'p4'`;
  `tests/integration/test_critique_pipeline.py` then got past the proxy
  (remaining failure was a missing PySceneDetect, installed from the
  project's own `ai` extras → 8 passed). New unit test fails before, passes
  after.
- Status: Verified and implemented
- Commit hash: see CLOSING entry
- Push: see CLOSING entry
- Known risks: none; NVENC path unchanged.
- Next step: none.

## [run-20261022-a] [2026-10-22 06:35, Europe/Oslo] [5]
- Backlog ID / title: M-5..M-8 / research, gap analysis and mission docs
- Type: research + docs
- What changed and why: Read FFmpeg's alimiter and loudnorm sources and
  filter docs; measured the shipped loudness chain on synthetic audio. Found
  that single-pass loudnorm lands ≈3 LU under −14 LUFS on bursty material and
  that two-pass linear mode is refused by loudnorm for exactly that material
  (LRA and TP conditions in `af_loudnorm.c`), so the fix is an owner decision
  on dynamics, not a two-pass switch. Created `ENGINEERING_BACKLOG.md`,
  `RESEARCH_LOG.md`, `.ai-mission/PROJECT_MAP.md`.
- Files changed: the three files above, this ledger
- Evidence: ffmpeg/ebur128 measurements in the run report (synthetic pink
  noise, FFmpeg 6.1.1); source excerpts quoted in the research log.
- Status: Research hypothesis requiring an experiment (M-5); Verified finding
  awaiting implementation (M-6, M-7, M-8)
- Commit hash: see CLOSING entry
- Push: see CLOSING entry
- Known risks: synthetic signal is harsher than real gameplay audio.
- Next step: M-5 needs QA loudness figures from real renders on the target machine.
