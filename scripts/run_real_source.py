"""Run one real recording through the whole pipeline, and report what happened.

The test fixtures are seconds long and synthetically generated, which is right
for a suite that has to run in minutes — but they cannot answer the questions
the specification actually asks. A six-second colour-bar clip does not tell you
whether §7's memory ceiling holds over an hour of 1080p60, whether §15's VLM
budget is respected on real footage, or whether a 67-minute session becomes a
watchable 20-minute edit.

This does. It takes a recording, runs `IMPORT → … → EDL`, and prints what each
stage cost and produced. Nothing is asserted: this is a measurement, and the
numbers are for reading.

    python scripts/run_real_source.py "D:/Gaming 2026/2026-05-08 22-24-23.mkv"
    python scripts/run_real_source.py <path> --minutes 20 --mode story

Everything it writes lands under the repository's data root. It is safe to
interrupt: the job system records each stage as it completes, so re-running the
same project resumes rather than restarting (§47).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A Windows console defaults to the system codepage -- cp1256 on this machine --
# which cannot encode the arrows and section signs this script prints, and
# raises rather than substituting. Ask for UTF-8, and fall back to replacing
# what the terminal cannot draw: losing a glyph is better than losing the run.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):  # not a real terminal
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.config.loader import load_config
from backend.config.paths import build_paths
from backend.core.models.enums import JobStage, VideoMode
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.pipeline.runner import PipelineRunner
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="the recording to edit")
    parser.add_argument(
        "--minutes", type=int, default=20, help="target output duration (§6 band)"
    )
    parser.add_argument(
        "--mode",
        default="story",
        choices=[mode.value for mode in VideoMode],
        help="§35 video mode",
    )
    parser.add_argument("--name", default=None, help="project name")
    arguments = parser.parse_args()

    source = arguments.source.expanduser()
    if not source.is_file():
        print(f"No file at {source}", file=sys.stderr)
        return 2

    config = load_config()
    paths = build_paths(config).create()
    database = Database(paths.database_path, config.application.database)
    migrate(database)

    projects = ProjectManager(database, paths, config)
    media_service = MediaIngestionService(database, paths, config)

    project = projects.create(
        ProjectCreate(
            name=arguments.name or source.stem,
            target_duration_seconds=arguments.minutes * 60,
            mode=VideoMode(arguments.mode),
        )
    )
    print(f"project      {project.id}  ({project.name})")
    print(f"source       {source}")
    print(f"target       {arguments.minutes} minutes, {arguments.mode}")
    print(f"data root    {paths.data_root}")
    print()

    media = media_service.import_media(project.id, MediaImport(path=str(source)))
    runner = PipelineRunner(database, paths, config)

    started = time.monotonic()
    print(f"{'stage':<14} {'status':<9} {'seconds':>8}   detail")
    print("-" * 78)
    for outcome in runner.run_project(project.id):
        job = outcome.job
        elapsed = job.duration_seconds or 0.0
        status = "ok" if outcome.succeeded else "FAILED"
        detail = _detail(job)
        print(f"{job.stage.value:<14} {status:<9} {elapsed:>8.1f}   {detail}")
        if not outcome.succeeded:
            print(f"\n{job.error_code}: {job.error_message}", file=sys.stderr)
            return 1

    total = time.monotonic() - started
    print("-" * 78)
    _report(database, project.id, media.id, source, total)
    return 0


def _detail(job) -> str:
    """One line per stage, chosen from whatever that stage reports."""
    result = job.result or {}
    if job.stage is JobStage.PROBE:
        mic = " + microphone" if result.get("has_separate_microphone_track") else ""
        return (
            f"{result.get('duration_seconds', 0):.0f}s, {result.get('width')}x"
            f"{result.get('height')}@{result.get('fps')}, "
            f"{result.get('audio_tracks', 0)} audio{mic}"
        )
    if job.stage is JobStage.FRAMES:
        return f"{result.get('frames', result.get('count', 0))} frames"
    if job.stage is JobStage.AUDIO:
        return f"{result.get('track_count', 0)} analysis track(s)"
    if job.stage is JobStage.TRANSCRIPT:
        return (
            f"{result.get('segments', 0)} segments from "
            f"{result.get('track_reason', 'the audio')}"
        )
    if job.stage is JobStage.VISION:
        return f"{result.get('observations', 0)} observations"
    if job.stage is JobStage.MOMENTS:
        return f"{result.get('moments', 0)} moments"
    if job.stage is JobStage.STORY:
        clips = result.get("clips")
        count = len(clips) if isinstance(clips, list) else 0
        return f"{count} clips, {result.get('total_seconds', 0):.0f}s"
    if job.stage is JobStage.EDL:
        return (
            f"{result.get('clips', 0)} clips, {result.get('duration_seconds', 0):.0f}s, "
            f"{result.get('captions', 0)} captions, {result.get('effects', 0)} effects"
        )
    return ", ".join(f"{key}={value}" for key, value in list(result.items())[:2])


def _report(database, project_id: str, media_id: str, source: Path, total: float) -> None:
    moments = MomentRepository(database)
    timeline = TimelineRepository(database)
    clips = timeline.list_clips(project_id)
    duration = timeline.duration_seconds(project_id)

    print()
    print(f"analysed in   {total / 60:.1f} minutes")
    print(f"moments       {moments.count_for_media(media_id)} found")
    print(f"edit          {len(clips)} clips, {duration / 60:.1f} minutes")
    print(f"captions      {timeline.caption_count(project_id)}")
    print(f"effects       {timeline.effect_count(project_id)}")
    if clips:
        print()
        print("first five clips (source → timeline):")
        for clip in clips[:5]:
            kind = clip.moment_type.value if clip.moment_type else "?"
            print(
                f"  {clip.clip_index:>3}  {_stamp(clip.source_in)}-{_stamp(clip.source_out)}"
                f"  →  {_stamp(clip.timeline_start)}  {kind:<10} {clip.role}"
            )
    print()
    print("Nothing was rendered: RENDER is Phase 10. What exists is the EDL.")


def _stamp(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes:02d}:{remainder:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
