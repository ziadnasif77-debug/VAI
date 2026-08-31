"""The pipeline runner (SPEC sections 46, 47, 81, 82).

The runner is the loop the job system implies: take the next job whose
dependencies are satisfied, run its worker, record what happened, repeat. It
holds no pipeline state of its own -- everything it needs is a query, which is
what lets a killed process be replaced by a new one that simply continues (§47).

Three obligations are met here rather than in each worker, so no stage can
forget one:

* **Progress** (§60, §81) is throttled before it reaches the database. A worker
  reports every half-second for hours; writing each one would turn a progress
  bar into thousands of transactions.
* **Cancellation** (§82) is advisory and checked on a cached read. A worker that
  stops mid-encode leaves a partial file the media layer discards, so the
  project stays consistent.
* **Failure** (§81) is typed. A worker's :class:`~backend.core.errors.ErrorCode`
  reaches the job row unchanged; anything unexpected becomes ``INTERNAL_ERROR``
  with a traceback in the log rather than a silent stall.

Stages with no worker yet are not failures. The runner stops when it reaches
one and says so, which is what makes a half-built pipeline usable during
development instead of red.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from backend.config.paths import Paths
from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, GamingEditorError
from backend.core.gpu import release_everything_we_loaded
from backend.core.logging import LogChannel, get_logger, log_context
from backend.core.models.enums import JobStage, JobStatus, MediaState
from backend.core.models.jobs import Job
from backend.database.connection import Database
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.media.ffmpeg import CancelledError, FFmpegRunner
from backend.pipeline.workers import default_workers
from backend.pipeline.workers.base import ProgressReporter, StageWorker, WorkerContext
from backend.services.job_manager import PER_MEDIA_STAGES, JobManager

logger = get_logger("pipeline.runner", LogChannel.PIPELINE)

#: Minimum progress movement, and minimum interval, before a progress update is
#: written. A two-hour proxy emits ~14 000 reports; at these thresholds that
#: becomes a couple of hundred writes.
PROGRESS_MIN_DELTA: Final[float] = 0.01
PROGRESS_MIN_INTERVAL_SECONDS: Final[float] = 2.0

#: How long a cancellation flag may be stale. Cancellation is a user action, so
#: a second of latency is invisible; re-reading the row on every FFmpeg tick is
#: not free.
CANCEL_CHECK_INTERVAL_SECONDS: Final[float] = 1.0


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What running one job produced."""

    job: Job
    #: ``None`` when the stage completed. Otherwise the code it failed with.
    error_code: ErrorCode | str | None = None

    @property
    def succeeded(self) -> bool:
        return self.job.status is JobStatus.COMPLETED

    @property
    def cancelled(self) -> bool:
        return self.job.status is JobStatus.CANCELLED


class PipelineRunner:
    """Executes queued stages for a project."""

    def __init__(
        self,
        database: Database,
        paths: Paths,
        config: AppConfig,
        *,
        job_manager: JobManager | None = None,
        ffmpeg: FFmpegRunner | None = None,
        workers: dict[JobStage, StageWorker] | None = None,
    ) -> None:
        self._db = database
        self._paths = paths
        self._config = config
        self._jobs = job_manager or JobManager(database, config)
        self._ffmpeg = ffmpeg or FFmpegRunner(config.ffmpeg)
        self._workers = dict(workers if workers is not None else default_workers())
        self._media = MediaRepository(database)

    @property
    def supported_stages(self) -> frozenset[JobStage]:
        """Stages this runner can execute. The rest are still to be built."""
        return frozenset(self._workers)

    @property
    def jobs(self) -> JobManager:
        """The job manager this runner reports to.

        Exposed so a caller that already has a runner can inspect or re-queue
        stages without constructing a second manager over the same database.
        """
        return self._jobs

    # -- execution ------------------------------------------------------

    def run_next(self, project_id: str) -> RunOutcome | None:
        """Run the next runnable job, or return ``None`` if there is nothing to do.

        ``None`` covers three different situations, which are distinguished in
        the log rather than in the return type: no queued jobs, no job whose
        dependencies are met, and a next job whose stage has no worker yet.
        """
        job = self._jobs.next_runnable(
            project_id, runnable=lambda stage: stage in self._workers
        )
        if job is None:
            # Every per-media chain is done, so the stages that reason across
            # all of them can now be queued. This is the moment §46 means by
            # "once every file has finished its own chain": doing it earlier
            # would let STORY start while a second recording was still being
            # analysed, and it has to see them all.
            if not self._queue_project_stages(project_id):
                return self._paused(project_id)
            job = self._jobs.next_runnable(
                project_id, runnable=lambda stage: stage in self._workers
            )
            if job is None:
                return self._paused(project_id)

        return self.run_job(job.id)

    def _paused(self, project_id: str) -> None:
        """Say which stage the queue is waiting on, if it is waiting on one."""
        waiting = self._jobs.next_runnable(project_id)
        if waiting is not None:
            logger.info(
                "Pipeline paused: no worker for this stage yet",
                extra={"project_id": project_id, "stage": waiting.stage.value,
                       "job_id": waiting.id},
            )
        return None

    def _queue_project_stages(self, project_id: str) -> bool:
        """Queue the project-wide stages once every media file is finished.

        Returns whether anything new was queued, so the caller knows whether
        retrying is worthwhile rather than looping.
        """
        jobs = self._jobs.list_jobs(project_id)
        if not jobs:
            return False
        per_media = [job for job in jobs if job.stage in PER_MEDIA_STAGES]
        if not per_media or any(
            job.status is not JobStatus.COMPLETED for job in per_media
        ):
            return False
        if any(job.stage not in PER_MEDIA_STAGES for job in jobs):
            return False  # already queued

        queued = self._jobs.queue_project_stages(project_id)
        if queued:
            logger.info(
                "Every media file is analysed; queueing the project-wide stages",
                extra={
                    "project_id": project_id,
                    "stages": [job.stage.value for job in queued],
                },
            )
        return bool(queued)

    def run_project(self, project_id: str, *, max_jobs: int | None = None) -> list[RunOutcome]:
        """Run stages until nothing more can run.

        Stops on the first failure. A stage whose dependency failed cannot
        produce anything useful, and continuing would bury the real error under
        a cascade of dependency failures (§81).
        """
        outcomes: list[RunOutcome] = []
        while max_jobs is None or len(outcomes) < max_jobs:
            outcome = self.run_next(project_id)
            if outcome is None:
                break
            outcomes.append(outcome)
            if not outcome.succeeded:
                break
        if outcomes:
            self.release_the_card()
        return outcomes

    def release_the_card(self) -> dict[str, Any]:
        """Hand the graphics card back now that there is nothing left to run.

        A montage should leave the machine as it found it. Ollama's default is
        to hold a model for minutes after the last call, and a model whose
        keep-alive was set by something else can hold it indefinitely — one on
        this machine was resident with an expiry in the year 2318. Waiting for
        a timeout that may never come is not releasing anything.

        Failure here is logged and swallowed on purpose: the video is already
        made, and refusing to return a finished project because a tidy-up call
        did not answer would be the tail wagging the dog.
        """
        if not self._config.gpu.release_when_idle:
            return {}
        try:
            freed = release_everything_we_loaded(self._config)
        except Exception as error:  # pragma: no cover - defensive tidy-up
            logger.warning("Could not release the card", extra={"error": str(error)[:160]})
            return {}
        if freed.get("released") or freed.get("freed_mb"):
            logger.info("Released the card; the machine is free", extra=freed)
        return freed

    def run_job(self, job_id: str) -> RunOutcome:
        """Run one job by id, whatever its position in the graph.

        Used by the runner loop, by a manual re-run from the analysis screen,
        and by tests that exercise a single stage.
        """
        job = self._jobs.get_job(job_id)
        worker = self._workers.get(job.stage)
        if worker is None:
            failed = self._jobs.fail(
                job_id,
                error_code=ErrorCode.WORKER_FAILED,
                error_message=f"No worker is registered for stage {job.stage.value!r}.",
            )
            return RunOutcome(job=failed, error_code=ErrorCode.WORKER_FAILED)

        started = self._jobs.start(job_id)
        if started.status is JobStatus.CANCELLED:
            return RunOutcome(job=started)

        context = self._build_context(started)
        with log_context(project_id=started.project_id, job_id=started.id):
            try:
                result = worker.run(context)
            except CancelledError:
                logger.info(
                    "Stage cancelled at a checkpoint",
                    extra={"stage": started.stage.value},
                )
                return RunOutcome(job=self._jobs.cancel(job_id))
            except GamingEditorError as exc:
                self._record_media_failure(started, exc.code, exc.message)
                failed = self._jobs.fail(
                    job_id, error_code=exc.code, error_message=exc.message
                )
                return RunOutcome(job=failed, error_code=exc.code)
            except Exception as exc:  # the boundary that keeps the runner loop alive
                # An unexpected exception is a bug, not a pipeline outcome. It
                # is logged with its traceback and reported as a failed stage,
                # because a worker crash must not take down the runner (§81).
                logger.exception(
                    "Stage raised an unexpected error",
                    extra={"stage": started.stage.value},
                )
                message = f"{type(exc).__name__}: {exc}"
                self._record_media_failure(started, ErrorCode.INTERNAL_ERROR, message)
                failed = self._jobs.fail(
                    job_id, error_code=ErrorCode.INTERNAL_ERROR, error_message=message
                )
                return RunOutcome(job=failed, error_code=ErrorCode.INTERNAL_ERROR)

        completed = self._jobs.complete(job_id, result=_serialisable(result))
        if completed.stage is JobStage.QA:
            if self._second_look(completed):
                # The critic corrected this edit and has not yet judged the
                # result. Nothing is delivered until it has: publishing a
                # video the critic is about to revert would be the worst
                # possible ordering of these two stages.
                return RunOutcome(job=completed)
            self._maybe_auto_publish(completed)
            self._maybe_auto_shorts(completed)
        return RunOutcome(job=completed)

    def _second_look(self, job: Job) -> bool:
        """Put CRITIC2 back for its second pass, if it is owed one.

        A stage cannot re-queue itself while it is running, so the critic asks
        for the render and QA it needs and the loop is closed here, when that
        QA lands.
        """
        from backend.pipeline.workers.critic2_worker import owes_a_second_look

        if not owes_a_second_look(self._db, job.project_id):
            return False
        try:
            for pending in JobRepository(self._db).list_for_project(job.project_id):
                if pending.stage is JobStage.CRITIC2:
                    self._jobs.requeue(pending.id)
                    logger.info(
                        "The critic was returned for its second pass",
                        extra={"project_id": job.project_id, "job_id": pending.id},
                    )
                    return True
        except Exception:
            logger.exception("The critic could not be returned for its second pass")
        return False

    def _maybe_auto_shorts(self, job: Job) -> None:
        """Queue the vertical cuts when the owner's standing config asks.

        Same §51 arithmetic as auto-publish: nothing is delivered *unasked*,
        and `shorts.auto_after_qa` is the asking -- made once, in
        configuration, by the owner who wants a Reel or two ready after every
        render. Failure to queue never poisons QA's own success.
        """
        try:
            shorts = self._config.shorts
            if not (shorts.enabled and shorts.auto_after_qa):
                return
            queued = self._jobs.queue(job.project_id, JobStage.SHORTS)
            logger.info(
                "Auto-shorts queued by the standing configuration",
                extra={"shorts_job": queued.id, "count": shorts.count},
            )
        except Exception:
            logger.exception("Auto-shorts could not be queued")

    def _maybe_auto_publish(self, job: Job) -> None:
        """Queue the YouTube publish when the project asked for it up front.

        §51 stands -- nothing is delivered *unasked* -- because the
        auto-publish flag ticked at the import screen is the asking, made
        once, explicitly, and recorded on the project row. Idempotent by the
        same rule every queue call follows; failure to queue is logged, never
        raised, because QA's own success must not be poisoned by a delivery
        convenience.
        """
        try:
            from backend.database.repositories.projects import ProjectRepository

            project = ProjectRepository(self._db).get(job.project_id)
            if project is None or not project.auto_publish:
                return
            score = (job.result or {}).get("quality_score")
            floor = self._config.publishing.auto_publish_minimum_score
            if isinstance(score, (int, float)) and score < floor:
                # The asking stands; the floor is about *this* video. The
                # render and the Reels are done, the button still works --
                # only the hands-free upload declines to spend the channel's
                # reputation on a video its own QA scored this low.
                logger.warning(
                    "Auto-publish refused by the quality floor",
                    extra={
                        "project_id": job.project_id,
                        "quality_score": score,
                        "floor": floor,
                    },
                )
                return
            queued = self._jobs.queue(
                job.project_id,
                JobStage.PUBLISH,
                payload={
                    "target": "youtube",
                    "auto": True,
                    # The project's own auto-publish flag, set by hand.
                    "authorised_by": "project_auto_publish",
                },
            )
            logger.info(
                "Auto-publish queued by the project's own flag",
                extra={"publish_job": queued.id},
            )
        except Exception:
            logger.exception("Auto-publish could not be queued")

    # -- internals ------------------------------------------------------

    def _build_context(self, job: Job) -> WorkerContext:
        media = self._media.get(job.media_id) if job.media_id else None
        return WorkerContext(
            job=job,
            media=media,
            paths=self._paths.project(job.project_id),
            models_dir=self._paths.models_dir,
            data_root=self._paths.data_root,
            profiles_dir=self._paths.profiles_dir,
            config=self._config,
            database=self._db,
            ffmpeg=self._ffmpeg,
            report=self._progress_reporter(job.id),
            should_cancel=self._cancel_check(job.id),
        )

    def _progress_reporter(self, job_id: str) -> ProgressReporter:
        """Return a throttled progress callback bound to ``job_id``."""
        state = {"fraction": -1.0, "at": 0.0, "message": ""}

        def report(fraction: float, message: str = "") -> None:
            clamped = min(max(float(fraction), 0.0), 1.0)
            now = time.monotonic()
            moved = clamped - state["fraction"] >= PROGRESS_MIN_DELTA
            waited = now - state["at"] >= PROGRESS_MIN_INTERVAL_SECONDS
            changed = message != state["message"]
            # The terminal update always lands, so a finished stage never shows
            # 98 % because its last report was throttled away.
            if not (moved or waited or changed or clamped >= 1.0):
                return
            state.update(fraction=clamped, at=now, message=message)
            self._jobs.report_progress(job_id, clamped, message=message or None)

        return report

    def _cancel_check(self, job_id: str) -> Callable[[], bool]:
        """Return a cached cancellation check bound to ``job_id`` (§82)."""
        state = {"at": 0.0, "cancelled": False}

        def should_cancel() -> bool:
            if state["cancelled"]:
                return True
            now = time.monotonic()
            if now - state["at"] < CANCEL_CHECK_INTERVAL_SECONDS:
                return False
            state["at"] = now
            state["cancelled"] = self._jobs.get_job(job_id).cancel_requested
            return bool(state["cancelled"])

        return should_cancel

    def _record_media_failure(self, job: Job, code: ErrorCode, message: str) -> None:
        """Mark the media file as failed so the UI can say which file broke.

        Best effort: a database problem while recording a failure must not
        replace the original error with a less useful one.
        """
        if job.media_id is None:
            return
        try:
            media = self._media.get(job.media_id)
            if media is None:
                return
            with self._db.transaction():
                self._media.update(
                    media.model_copy(
                        update={
                            "state": MediaState.FAILED,
                            "error_code": code.value if isinstance(code, ErrorCode) else str(code),
                            "error_message": message,
                        }
                    )
                )
        except Exception:  # never mask the real failure with a bookkeeping one
            logger.warning(
                "Could not record the media failure state",
                extra={"media_id": job.media_id, "stage": job.stage.value},
            )


def _serialisable(result: dict[str, Any] | None) -> dict[str, Any]:
    """Make a worker result safe for the job row's JSON column."""
    if not result:
        return {}
    return {str(key): _value(value) for key, value in result.items()}


def _value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return str(value)


__all__ = [
    "CANCEL_CHECK_INTERVAL_SECONDS",
    "PROGRESS_MIN_DELTA",
    "PROGRESS_MIN_INTERVAL_SECONDS",
    "PipelineRunner",
    "RunOutcome",
]
