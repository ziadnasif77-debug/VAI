"""Job management (SPEC sections 46, 47, 81, 82, 90).

Owns queueing, state transitions, retry accounting, cancellation and resume.
It does not execute anything: workers claim jobs and report back. That split is
what makes resume possible -- state lives in the database, not in a process.

Stages are queued per media where they consume one file (probe, proxy, audio,
frames, transcript, scenes, vision, OCR) and once per project where they reason
across all of them (game events onward).
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, JobError
from backend.core.ids import new_id
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, JobStatus, ProjectStatus
from backend.core.models.jobs import (
    MANUAL_STAGES,
    Job,
    ProjectStageStatus,
    dependencies_of,
    downstream_of,
    runnable_stages,
    stages_in_order,
)
from backend.database.connection import Database
from backend.database.repositories.jobs import JobRepository

logger = get_logger("services.job_manager", LogChannel.PIPELINE)

#: Stages that run once per source file. Everything else runs once per project.
#:
#: The line falls where §45 puts it: ``game_events`` and ``moments`` both carry
#: a ``NOT NULL media_id``, because an event is something that happened *in a
#: recording* and a moment is a span *of one*. Only from STORY onward does the
#: pipeline reason across every file at once, and those stages have no media
#: column to fill.
PER_MEDIA_STAGES: frozenset[JobStage] = frozenset(
    {
        JobStage.IMPORT,
        JobStage.PROBE,
        JobStage.PROXY,
        JobStage.AUDIO,
        JobStage.FRAMES,
        JobStage.TRANSCRIPT,
        JobStage.AUDIO_EVENTS,
        JobStage.SCENES,
        JobStage.VISION,
        JobStage.OCR,
        JobStage.GAME_EVENTS,
        # V2-P1b built the semantic spine as a stage of its own and added it
        # to ANALYSIS_STAGES and to the dependency graph -- and not here, the
        # only set that actually queues work. MOMENTS depends on it, so every
        # project created after that phase stopped dead at GAME_EVENTS with a
        # MOMENTS job that could never become runnable. It went unseen because
        # the gate projects had their semantic stage queued by hand.
        JobStage.SEMANTIC,
        JobStage.MOMENTS,
    }
)


#: The lifecycle in order, so a status only ever moves forwards. Re-running a
#: stage on a finished project must not walk it backwards.
_LIFECYCLE: tuple[ProjectStatus, ...] = (
    ProjectStatus.CREATED,
    ProjectStatus.IMPORTING,
    ProjectStatus.ANALYZING,
    ProjectStatus.MOMENTS_READY,
    ProjectStatus.EDIT_GENERATED,
    ProjectStatus.REVIEW,
    ProjectStatus.RENDERING,
    ProjectStatus.QA,
    ProjectStatus.COMPLETED,
)

#: Which stage finishing means the project has reached which state (§122).
#: Stages not named here do not move it -- the per-file analysis chain is all
#: "analyzing", and saying so once is enough.
_STATUS_AFTER: dict[JobStage, ProjectStatus] = {
    JobStage.IMPORT: ProjectStatus.IMPORTING,
    JobStage.PROBE: ProjectStatus.ANALYZING,
    JobStage.MOMENTS: ProjectStatus.MOMENTS_READY,
    JobStage.STORY: ProjectStatus.EDIT_GENERATED,
    # CRITIQUE is deliberately absent: it runs between the EDL and the
    # render and leaves the project in REVIEW either way. A status that
    # flickered through a stage nobody waits for would say less, not more.
    JobStage.EDL: ProjectStatus.REVIEW,
    JobStage.RENDER: ProjectStatus.RENDERING,
    JobStage.QA: ProjectStatus.COMPLETED,
}


class JobManager:
    """Queues and tracks pipeline stage executions."""

    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db = database
        self._config = config
        self._jobs = JobRepository(database)

    # -- queueing -------------------------------------------------------

    def queue(
        self,
        project_id: str,
        stage: JobStage,
        *,
        media_id: str | None = None,
        payload: dict | None = None,
    ) -> Job:
        """Queue a stage, or return the job that already covers it.

        Idempotent: re-queueing a completed stage returns it untouched, which
        is what makes "analyze" safe to press twice.
        """
        if stage in PER_MEDIA_STAGES and media_id is None:
            raise JobError(
                f"Stage {stage.value!r} runs per media but no media_id was given.",
                code=ErrorCode.JOB_DEPENDENCY_FAILED,
                details={"stage": stage.value, "project_id": project_id},
                recoverable=False,
            )

        existing = self._jobs.find(project_id, stage, media_id)
        if existing is not None:
            if existing.status is JobStatus.FAILED and existing.can_retry:
                return self._requeue(existing)
            return existing

        job = Job(
            id=new_id("job"),
            project_id=project_id,
            media_id=media_id,
            stage=stage,
            status=JobStatus.QUEUED,
            max_attempts=self._max_attempts,
            created_at=datetime.now(timezone.utc),
            payload=dict(payload or {}),
        )
        with self._db.transaction():
            self._jobs.create(job)
        logger.info(
            "Job queued",
            extra={"project_id": project_id, "job_id": job.id, "stage": stage.value},
        )
        return job

    def queue_import_chain(self, project_id: str, media_id: str) -> list[Job]:
        """Queue every per-media stage for a newly imported file.

        Only the per-media stages are queued here. The project-wide stages are
        queued once every file has finished its own chain, so game-event
        detection sees all the sources at once.
        """
        jobs = [
            self.queue(project_id, stage, media_id=media_id)
            for stage in stages_in_order()
            if stage in PER_MEDIA_STAGES
        ]
        logger.info(
            "Import chain queued",
            extra={"project_id": project_id, "media_id": media_id, "jobs": len(jobs)},
        )
        return jobs

    def queue_project_stages(self, project_id: str) -> list[Job]:
        """Queue the project-wide analysis and edit stages.

        Delivery stages are excluded: publishing is always an explicit action.
        """
        return [
            self.queue(project_id, stage)
            for stage in stages_in_order()
            if stage not in PER_MEDIA_STAGES and stage not in MANUAL_STAGES
        ]

    # -- execution transitions ------------------------------------------

    def start(self, job_id: str) -> Job:
        """Mark a job RUNNING after checking its prerequisites."""
        job = self._jobs.require(job_id)
        if job.status is JobStatus.CANCELLED:
            # Already settled. Asking a cancelled job to start gets a cancelled
            # job back, not an exception: cancellation can land between the
            # moment a caller picks a job and the moment it starts it, and that
            # race is ordinary rather than a programming error.
            return job
        if job.status not in (JobStatus.QUEUED, JobStatus.FAILED):
            raise JobError(
                f"Job {job_id!r} cannot start from status {job.status.value!r}.",
                code=ErrorCode.JOB_DEPENDENCY_FAILED,
                details={"job_id": job_id, "status": job.status.value},
                recoverable=False,
            )
        if job.cancel_requested:
            return self.cancel(job_id)

        completed = self._jobs.completed_stages(job.project_id)
        unmet = [stage.value for stage in dependencies_of(job.stage) if stage not in completed]
        if unmet:
            raise JobError(
                f"Stage {job.stage.value!r} cannot start: {', '.join(unmet)} incomplete.",
                code=ErrorCode.JOB_DEPENDENCY_FAILED,
                details={"job_id": job_id, "unmet_dependencies": unmet},
                recoverable=False,
            )

        started = job.model_copy(
            update={
                "status": JobStatus.RUNNING,
                "attempt": job.attempt + 1,
                "started_at": datetime.now(timezone.utc),
                "progress": 0.0,
                "error_code": None,
                "error_message": None,
            }
        )
        with self._db.transaction():
            self._jobs.update(started)
        logger.info(
            "Job started",
            extra={
                "project_id": job.project_id,
                "job_id": job_id,
                "stage": job.stage.value,
                "attempt": started.attempt,
            },
        )
        return started

    def report_progress(self, job_id: str, progress: float, *, message: str | None = None) -> Job:
        """Update progress for the analysis screen (§60)."""
        job = self._jobs.require(job_id)
        updated = job.model_copy(
            update={
                "progress": min(max(progress, 0.0), 1.0),
                "message": message if message is not None else job.message,
            }
        )
        with self._db.transaction():
            self._jobs.update(updated)
        return updated

    def complete(self, job_id: str, *, result: dict | None = None) -> Job:
        """Mark a job COMPLETED and record its duration (§81)."""
        job = self._jobs.require(job_id)
        finished_at = datetime.now(timezone.utc)
        completed = job.model_copy(
            update={
                "status": JobStatus.COMPLETED,
                "progress": 1.0,
                "completed_at": finished_at,
                "duration_seconds": _elapsed(job.started_at, finished_at),
                "result": dict(result or {}),
                "error_code": None,
                "error_message": None,
            }
        )
        with self._db.transaction():
            self._jobs.update(completed)
        logger.info(
            "Job completed",
            extra={
                "project_id": job.project_id,
                "job_id": job_id,
                "stage": job.stage.value,
                "duration": completed.duration_seconds,
            },
        )
        self._advance_project(job.project_id, job.stage)
        return completed

    def _advance_project(self, project_id: str, stage: JobStage) -> None:
        """Move the project's lifecycle status on when a stage finishes (§122).

        `ProjectStatus` has ten states and, until this existed, exactly two of
        them were ever written: CREATED at creation and IMPORTING when a file
        was added. Everything after that kept saying "importing" -- a finished,
        QA'd, exportable video included, which is what the dashboard read out
        beside it.

        Written rather than derived on read because `list(status=...)` filters
        on the stored column, and a displayed value that disagrees with the one
        being filtered on is a worse lie than the one being fixed. This is the
        single place a stage is known to have finished, so it is the only place
        that has to be right.

        Only ever forwards. Re-running QA on a finished project must not walk
        its status backwards to "rendering".
        """
        target = _STATUS_AFTER.get(stage)
        if target is None:
            return
        from backend.database.repositories.projects import ProjectRepository

        projects = ProjectRepository(self._db)
        project = projects.get(project_id)
        if project is None:
            return
        if project.status not in _LIFECYCLE:
            # FAILED is not a point on the line. A project that failed and then
            # had a stage succeed is recovering, so it re-joins at that stage.
            pass
        elif _LIFECYCLE.index(target) <= _LIFECYCLE.index(project.status):
            return
        with self._db.transaction():
            projects.update(project.model_copy(update={"status": target}))
        logger.info(
            "Project status advanced",
            extra={"project_id": project_id, "status": target.value, "after": stage.value},
        )

    def fail(
        self,
        job_id: str,
        *,
        error_code: ErrorCode | str,
        error_message: str,
    ) -> Job:
        """Mark a job FAILED. Never silent (§81)."""
        job = self._jobs.require(job_id)
        finished_at = datetime.now(timezone.utc)
        code = error_code.value if isinstance(error_code, ErrorCode) else str(error_code)
        failed = job.model_copy(
            update={
                "status": JobStatus.FAILED,
                "completed_at": finished_at,
                "duration_seconds": _elapsed(job.started_at, finished_at),
                "error_code": code,
                "error_message": error_message,
            }
        )
        with self._db.transaction():
            self._jobs.update(failed)
        logger.error(
            "Job failed",
            extra={
                "project_id": job.project_id,
                "job_id": job_id,
                "stage": job.stage.value,
                "error_code": code,
                "attempt": failed.attempt,
                "duration": failed.duration_seconds,
            },
        )
        return failed

    def requeue(
        self, job_id: str, *, reset_attempts: bool = True, payload: dict | None = None
    ) -> Job:
        """Return a finished stage to the queue so it can run again (§90).

        ``payload`` replaces the stored one when given. The publish stage is
        why: its payload *is* the user's instruction -- target, title,
        visibility -- and re-running it with yesterday's instruction is
        nobody's intent.

        Re-analysis is a first-class operation: changing a sampling interval,
        a game profile or a prompt means a stage that already completed has to
        run again over the same source. Distinct from the retry path, which
        keeps the attempt count because it is still the *same* run -- a user
        asking for a re-run is starting a new one, so the retry budget resets.

        Raises:
            JobError: if the job is currently RUNNING. Something is executing
                it, and re-queueing underneath a live worker would produce two
                writers for one artefact.
        """
        job = self._jobs.require(job_id)
        if job.status is JobStatus.RUNNING:
            raise JobError(
                f"Job {job_id!r} is running and cannot be re-queued.",
                code=ErrorCode.JOB_DEPENDENCY_FAILED,
                details={"job_id": job_id, "stage": job.stage.value},
                recoverable=False,
            )
        requeued = job.model_copy(
            update={
                "status": JobStatus.QUEUED,
                **({"payload": dict(payload)} if payload is not None else {}),
                "progress": 0.0,
                "message": None,
                "started_at": None,
                "completed_at": None,
                "duration_seconds": None,
                "error_code": None,
                "error_message": None,
                "cancel_requested": False,
                "attempt": 0 if reset_attempts else job.attempt,
            }
        )
        with self._db.transaction():
            self._jobs.update(requeued)
        logger.info(
            "Job re-queued for another run",
            extra={
                "project_id": job.project_id,
                "job_id": job_id,
                "stage": job.stage.value,
                "previous_status": job.status.value,
            },
        )
        return requeued

    def cancel_project(self, project_id: str) -> int:
        """Request cancellation of every unfinished job (§82).

        Advisory: workers stop at their next checkpoint, so a cancelled project
        is never left half-written.
        """
        with self._db.transaction():
            affected = self._jobs.request_cancel(project_id)
        logger.info("Cancellation requested", extra={"project_id": project_id, "jobs": affected})
        return affected

    def cancel(self, job_id: str) -> Job:
        """Mark one job CANCELLED."""
        job = self._jobs.require(job_id)
        cancelled = job.model_copy(
            update={
                "status": JobStatus.CANCELLED,
                "completed_at": datetime.now(timezone.utc),
                "cancel_requested": True,
            }
        )
        with self._db.transaction():
            self._jobs.update(cancelled)
        return cancelled

    # -- scheduling and recovery ----------------------------------------

    def next_runnable(self, project_id: str) -> Job | None:
        """Return the next job whose dependencies are satisfied.

        Stage order decides priority, so the pipeline advances breadth-first
        through a project's media rather than finishing one file completely.
        """
        completed = self._jobs.completed_stages(project_id)
        # include_manual: SS51 forbids QUEUEING a delivery uninvited, and that
        # rule lives at queue time. A delivery job that exists in the queue
        # was asked for -- the Export screen's button, or the project's own
        # auto-publish flag -- and a worker that refuses to run it strands an
        # explicit instruction forever. Found live: the first real upload sat
        # queued four minutes behind a healthy idle worker.
        ready = set(runnable_stages(completed, include_manual=True))
        for job in self._jobs.list_for_project(project_id):
            if job.status is not JobStatus.QUEUED:
                continue
            if job.cancel_requested:
                continue
            if job.stage in ready or not dependencies_of(job.stage):
                return job
        return None

    def recover(self, project_id: str | None = None) -> list[Job]:
        """Re-queue jobs left RUNNING by a crash (§47).

        Nothing is executing at startup, so a RUNNING row means the process
        died. Completed work is untouched -- recovery never restarts from
        IMPORT.
        """
        stale = self._jobs.stale_running_jobs(project_id)
        recovered: list[Job] = []
        for job in stale:
            requeued = job.model_copy(
                update={
                    "status": JobStatus.QUEUED,
                    "progress": 0.0,
                    "started_at": None,
                    "message": "Re-queued after interrupted run",
                }
            )
            with self._db.transaction():
                self._jobs.update(requeued)
            recovered.append(requeued)

        if recovered:
            logger.warning(
                "Recovered interrupted jobs",
                extra={
                    "project_id": project_id,
                    "jobs": len(recovered),
                    "stages": sorted({job.stage.value for job in recovered}),
                },
            )
        self._settle_abandoned(project_id)
        self._backfill_new_stages(project_id)
        return recovered

    def _backfill_new_stages(self, project_id: str | None) -> list[Job]:
        """Create rows for stages that joined the graph after a project ran.

        CRITIQUE arrived between EDL and RENDER, and a project analysed before
        it existed has no row for it -- so RENDER refuses to start with
        "critique incomplete", forever, and there is nothing on the screen to
        run. Every path that requeues by existing rows hits the same wall.
        Startup is where state is made true (§47), so the rows are created
        here, and the *status* they are created in is the whole decision:

        * a project whose later stages are **complete** gets the missing stage
          as completed with ``skipped`` in its result -- the truthful record
          that the stage did not exist when this project ran. Queueing it
          instead would have an upgrade silently re-editing a finished video,
          which §78 exists to forbid.
        * a project still on its way to a render gets it **queued**, so the
          runner simply runs it in order.

        Project-wide stages only: those are where the graph has ever grown
        mid-pipeline, and a per-media backfill would need per-media rows this
        pass has no reason to invent yet.
        """
        projects = [project_id] if project_id else self._jobs.project_ids()
        created: list[Job] = []
        for project in projects:
            rows = [job for job in self._jobs.list_for_project(project) if job.media_id is None]
            by_stage = {job.stage for job in rows}
            done = {job.stage for job in rows if job.status is JobStatus.COMPLETED}
            for stage in stages_in_order():
                if stage in PER_MEDIA_STAGES or stage in MANUAL_STAGES or stage in by_stage:
                    continue
                later = set(downstream_of(stage))
                if not later & by_stage:
                    # The project never got this far; nothing to make true.
                    continue
                finished = bool(later & done)
                job = Job(
                    id=new_id("job"),
                    project_id=project,
                    stage=stage,
                    status=JobStatus.COMPLETED if finished else JobStatus.QUEUED,
                    progress=1.0 if finished else 0.0,
                    max_attempts=self._max_attempts,
                    created_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc) if finished else None,
                    message=(
                        f"The {stage.value} stage was added to the pipeline after this project ran"
                        if finished
                        else None
                    ),
                    result=(
                        {
                            "skipped": True,
                            "reason": (
                                f"the {stage.value} stage did not exist when this "
                                "project was rendered"
                            ),
                        }
                        if finished
                        else {}
                    ),
                )
                with self._db.transaction():
                    self._jobs.create(job)
                created.append(job)
        if created:
            logger.warning(
                "Backfilled stages the graph gained after these projects ran",
                extra={
                    "jobs": len(created),
                    "stages": sorted({job.stage.value for job in created}),
                    "completed": sum(1 for job in created if job.status is JobStatus.COMPLETED),
                    "queued": sum(1 for job in created if job.status is JobStatus.QUEUED),
                },
            )
        return created

    def _settle_abandoned(self, project_id: str | None) -> list[Job]:
        """Close queued jobs that were cancelled but never settled.

        An older build flagged queued jobs for cancellation and left them
        queued. Nothing runs them -- `next_runnable` skips a cancelled job --
        so they sit at "queued" forever and the pipeline for that project is
        dead while the screen says it is waiting its turn. Startup is where
        state is made true (§47), so they are closed here.
        """
        abandoned = self._jobs.abandoned_queued_jobs(project_id)
        settled: list[Job] = []
        for job in abandoned:
            closed = job.model_copy(
                update={
                    "status": JobStatus.CANCELLED,
                    "completed_at": datetime.now(timezone.utc),
                    "message": "Cancelled before it started",
                }
            )
            with self._db.transaction():
                self._jobs.update(closed)
            settled.append(closed)
        if settled:
            logger.warning(
                "Closed jobs that were cancelled but left queued",
                extra={
                    "project_id": project_id,
                    "jobs": len(settled),
                    "stages": sorted({job.stage.value for job in settled}),
                },
            )
        return settled

    # -- reporting ------------------------------------------------------

    def project_status(self, project_id: str) -> list[ProjectStageStatus]:
        """Per-stage rollup for ``GET /projects/{id}/status`` and the §60 screen."""
        jobs = self._jobs.list_for_project(project_id)
        by_stage: dict[JobStage, list[Job]] = {}
        for job in jobs:
            by_stage.setdefault(job.stage, []).append(job)

        report: list[ProjectStageStatus] = []
        for stage in stages_in_order():
            stage_jobs = by_stage.get(stage, [])
            if not stage_jobs:
                report.append(ProjectStageStatus(stage=stage))
                continue
            report.append(
                ProjectStageStatus(
                    stage=stage,
                    status=_aggregate_status(stage_jobs),
                    progress=sum(job.progress for job in stage_jobs) / len(stage_jobs),
                    error_code=next((job.error_code for job in stage_jobs if job.error_code), None),
                    updated_at=max(
                        (
                            job.completed_at or job.started_at or job.created_at
                            for job in stage_jobs
                        ),
                        default=None,
                    ),
                )
            )
        return report

    def list_jobs(self, project_id: str, *, status: JobStatus | None = None) -> list[Job]:
        return self._jobs.list_for_project(project_id, status=status)

    def get_job(self, job_id: str) -> Job:
        return self._jobs.require(job_id)

    # -- internals ------------------------------------------------------

    @property
    def _max_attempts(self) -> int:
        return 3

    def _requeue(self, job: Job) -> Job:
        """Return a failed job to the queue for another attempt (§81)."""
        requeued = job.model_copy(
            update={
                "status": JobStatus.QUEUED,
                "progress": 0.0,
                "started_at": None,
                "completed_at": None,
                "error_code": None,
                "error_message": None,
            }
        )
        with self._db.transaction():
            self._jobs.update(requeued)
        logger.info(
            "Job re-queued after failure",
            extra={
                "project_id": job.project_id,
                "job_id": job.id,
                "stage": job.stage.value,
                "attempt": job.attempt,
            },
        )
        return requeued


def _aggregate_status(jobs: list[Job]) -> JobStatus:
    """Worst-case rollup across the jobs of one stage."""
    statuses = {job.status for job in jobs}
    for candidate in (
        JobStatus.FAILED,
        JobStatus.RUNNING,
        JobStatus.CANCELLED,
        JobStatus.QUEUED,
    ):
        if candidate in statuses:
            return candidate
    return JobStatus.COMPLETED


def _elapsed(started_at: datetime | None, finished_at: datetime) -> float | None:
    if started_at is None:
        return None
    return max((finished_at - started_at).total_seconds(), 0.0)


__all__ = ["PER_MEDIA_STAGES", "JobManager"]
