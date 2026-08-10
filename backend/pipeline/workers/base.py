"""What a stage worker is, and what it is given (SPEC sections 46, 47, 81, 82).

A worker does one stage for one job and returns a result dictionary. It does
not decide what runs next, does not queue anything, and does not mark itself
complete -- the runner owns all of that. Keeping the two apart is what makes
resume possible: the pipeline's state lives in the database, so a worker
process can die at any point without taking the schedule with it (§47).

The three capabilities every worker is handed are the three §81/§82 obligations
in executable form: report progress, check for cancellation, and fail with a
typed error.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from backend.config.paths import ProjectPaths
from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, MediaError
from backend.core.models.enums import JobStage
from backend.core.models.jobs import Job
from backend.core.models.media import Media
from backend.database.connection import Database
from backend.media.ffmpeg import FFmpegRunner

#: ``(fraction, message)`` -- what the analysis screen shows (§60).
ProgressReporter = Callable[[float, str], None]


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """Everything a stage needs, and nothing it does not."""

    job: Job
    media: Media | None
    paths: ProjectPaths
    #: Where downloaded model weights live (§43). Separate from the project
    #: tree: models are shared across every project and must not be copied into
    #: each one.
    models_dir: Path
    #: Where game profiles live (§22). Resolved against the repository rather
    #: than the data root, because profiles ship with the code.
    profiles_dir: Path
    config: AppConfig
    database: Database
    ffmpeg: FFmpegRunner
    report: ProgressReporter
    should_cancel: Callable[[], bool]

    @property
    def project_id(self) -> str:
        return self.job.project_id

    def require_media(self) -> Media:
        """Return the job's media, or fail with a typed error.

        Per-media stages are queued with a ``media_id``; a job without one is a
        scheduling bug, and it must surface as a failed stage rather than an
        ``AttributeError`` inside FFmpeg argument construction.
        """
        if self.media is None:
            raise MediaError(
                f"Stage {self.job.stage.value!r} needs a media file but the job has none.",
                code=ErrorCode.JOB_DEPENDENCY_FAILED,
                details={"job_id": self.job.id, "stage": self.job.stage.value},
                recoverable=False,
            )
        return self.media

    def stage_result(self, stage: JobStage) -> dict[str, Any]:
        """Return what an upstream stage recorded for this job's media.

        The job result is the contract between stages (§81), so a stage reads
        its input from there rather than reconstructing paths from conventions.
        Guessing works right up until a naming rule changes in one place and not
        the other, and then it fails silently by finding nothing.

        Returns an empty dict when the stage has not run for this media. The
        dependency graph makes that impossible for a declared prerequisite, so
        an empty result means the caller asked about a stage it does not
        actually depend on.
        """
        # Imported here: the job repository knows the §45 tables, and the
        # worker context is deliberately not a database module.
        from backend.database.repositories.jobs import JobRepository

        job = JobRepository(self.database).find(self.project_id, stage, self.job.media_id)
        return dict(job.result) if job is not None else {}

    def source_path(self) -> Path:
        """The file this job reads, checked for existence.

        A recording referenced in place can be deleted or unplugged between
        import and analysis (§42 keeps originals outside the project by
        default), so every stage confirms it is still there.
        """
        media = self.require_media()
        path = Path(media.source_path)
        if not path.is_file():
            raise MediaError(
                f"The source file is no longer at {path}.",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"media_id": media.id, "path": str(path)},
                recoverable=False,
            )
        return path


@runtime_checkable
class StageWorker(Protocol):
    """One pipeline stage.

    Implementations must be idempotent: a stage that already produced its
    artefact re-runs after a crash and should recognise the existing output
    instead of redoing the work (§47).
    """

    stage: JobStage

    def run(self, context: WorkerContext) -> dict[str, Any]:
        """Execute the stage and return what it produced.

        The returned dictionary is stored on the job row (§81) and is what the
        API and the next stage read: artefact paths, counts, durations.
        """
        ...


__all__ = ["ProgressReporter", "StageWorker", "WorkerContext"]
