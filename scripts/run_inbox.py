"""Edit every recording sitting in ``input/`` and put the results in ``output/``.

The two-folder workflow: drop a recording in, collect a finished video. Nothing
else to know, and no project id to keep track of.

    python scripts/run_inbox.py
    python scripts/run_inbox.py --minutes 12 --mode best_moments
    python scripts/run_inbox.py "D:/Gaming 2026/2026-05-08 22-24-23.mkv"

The work still happens in a project under ``projects/`` (§43) -- that is where
the analysis, the timeline and the render live, and it is what makes a re-edit
cheap (§127). ``output/`` holds a copy of the finished file, named after the
recording rather than after a project id.

**A render that fails QA is not copied.** That is the point of §76's blocking
policy: a file with a technical fault should not reach the folder a person
publishes from. The findings are printed and the render is left in the project,
so nothing is lost -- and ``--force`` overrides it when the call is yours.

Safe to re-run: a recording whose output already exists is skipped, and the job
system resumes a project rather than restarting it (§47).
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A Windows console defaults to the system codepage -- cp1256 on this machine --
# which cannot encode the arrows this script prints, and raises rather than
# substituting.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):  # not a real terminal
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.config.loader import load_config
from backend.config.paths import build_paths
from backend.core.models.enums import JobStage, VideoMode
from backend.core.models.media import SUPPORTED_CONTAINERS, MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.pipeline.runner import PipelineRunner
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sources", type=Path, nargs="*",
        help="recordings to edit; defaults to everything in input/",
    )
    parser.add_argument(
        "--minutes", type=int, default=20, help="target output duration (§6 band)"
    )
    parser.add_argument(
        "--mode", default="story", choices=[mode.value for mode in VideoMode],
        help="§35 video mode",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="copy to output/ even when QA blocks the export",
    )
    parser.add_argument(
        "--redo", action="store_true",
        help="edit a recording again even if its output already exists",
    )
    arguments = parser.parse_args()

    config = load_config()
    paths = build_paths(config).create()

    sources = _sources(arguments.sources, paths.input_dir)
    if not sources:
        print(f"Nothing to edit. Put a recording in {paths.input_dir}")
        print(f"Recognised: {', '.join(sorted(SUPPORTED_CONTAINERS))}")
        return 0

    print(f"input        {paths.input_dir}")
    print(f"output       {paths.output_dir}")
    print(f"target       {arguments.minutes} minutes, {arguments.mode}")
    print(f"to edit      {len(sources)} recording(s)\n")

    database = Database(paths.database_path, config.application.database)
    migrate(database)
    projects = ProjectManager(database, paths, config)
    media_service = MediaIngestionService(database, paths, config)

    failures = 0
    for index, source in enumerate(sources, start=1):
        target = paths.output_dir / f"{source.stem}.mp4"
        if target.exists() and not arguments.redo:
            print(f"[{index}/{len(sources)}] {source.name} -- already in output/, skipping")
            continue

        print(f"[{index}/{len(sources)}] {source.name}")
        ok = _edit_one(
            source, target, arguments, config, paths, database, projects, media_service
        )
        failures += 0 if ok else 1
        print()

    done = len(sources) - failures
    print(f"{done} of {len(sources)} finished; results in {paths.output_dir}")
    return 1 if failures else 0


def _sources(given: list[Path], input_dir: Path) -> list[Path]:
    """What to edit: the arguments if any, otherwise the inbox.

    Directories are not walked. A recording is a file someone put there, and
    recursing would sweep up whatever else happens to be nested underneath.
    """
    if given:
        return [path.expanduser() for path in given if path.expanduser().is_file()]
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_CONTAINERS
    )


def _edit_one(
    source: Path,
    target: Path,
    arguments: argparse.Namespace,
    config,
    paths,
    database,
    projects: ProjectManager,
    media_service: MediaIngestionService,
) -> bool:
    project = projects.create(
        ProjectCreate(
            name=source.stem,
            target_duration_seconds=arguments.minutes * 60,
            mode=VideoMode(arguments.mode),
        )
    )
    media_service.import_media(project.id, MediaImport(path=str(source)))
    runner = PipelineRunner(database, paths, config)

    started = time.monotonic()
    rendered: str | None = None
    qa: dict | None = None
    for outcome in runner.run_project(project.id):
        job = outcome.job
        if not outcome.succeeded:
            print(f"    {job.stage.value} FAILED -- {job.error_code}: {job.error_message}")
            return False
        result = job.result or {}
        if job.stage is JobStage.RENDER:
            if result.get("skipped"):
                # Not a failure: there was nothing in the recording worth
                # editing, and saying so beats inventing a video (§95).
                print(f"    nothing to edit -- {result.get('reason', 'no moments found')}")
                return True
            rendered = result.get("output_path")
        elif job.stage is JobStage.QA:
            qa = result

    elapsed = time.monotonic() - started
    if not rendered:
        print("    the pipeline finished but recorded no rendered file")
        return False

    blocked = bool(qa and qa.get("blocks_export"))
    for line in (qa or {}).get("explanation") or []:
        print(f"    {line}")

    if blocked and not arguments.force:
        print(f"    QA blocks export, so nothing was copied. Render: {rendered}")
        print("    Re-run with --force to copy it anyway.")
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rendered, target)
    note = "  (QA blocked; copied because --force)" if blocked else ""
    print(f"    -> {target.name}  in {elapsed / 60:.1f} min{note}")
    return True


if __name__ == "__main__":
    sys.exit(main())
