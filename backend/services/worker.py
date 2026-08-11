"""The background job runner (SPEC §46, §57, §83).

The API queues work; something has to do it. Without this the interface shows a
render that never starts — which is exactly what a screen reading `status:
queued` cannot distinguish from one in progress, and exactly what happened the
first time the UI was pointed at a real project.

**One job at a time, deliberately.** §83 asks for resource awareness, and the
simplest honest form of that on a single machine is not running two FFmpeg
encodes and a VLM at once. A local editor is not a job farm: the user is
waiting for one video.

**It polls rather than being notified.** SQLite has no queue semantics worth
building on, and a two-second poll against a table that is nearly always empty
costs nothing. The alternative — a notification channel between the API process
and this one — would be real machinery for a problem measured in seconds.

**A failure stops that project, not the worker.** §81 makes every stage's error
part of its job row, so a failed stage is already reported. The loop moves to
the next project rather than exiting, because one broken recording should not
stop the other three.
"""

from __future__ import annotations

import threading
from typing import Any, Final

from backend.config.paths import Paths
from backend.config.schema import AppConfig
from backend.core.errors import GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, JobStatus
from backend.database.connection import Database
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers.base import StageWorker

logger = get_logger("services.worker", LogChannel.PIPELINE)

#: How long to wait when there was nothing to do. Short enough that pressing
#: "Render" feels immediate, long enough to be invisible when idle.
IDLE_SECONDS: Final[float] = 2.0


class JobWorker:
    """Runs queued pipeline jobs until asked to stop.

    Owns its own database connection: SQLite objects belong to the thread that
    created them, and sharing the API's would be a crash waiting for load.
    """

    def __init__(
        self,
        config: AppConfig,
        paths: Paths,
        *,
        workers: dict[JobStage, StageWorker] | None = None,
    ) -> None:
        """
        Args:
            workers: stage registry override. The default is every implemented
                stage with its real provider, which is right in production and
                wrong in a test -- a worker that loads Whisper to prove it
                polls the queue is three gigabytes of download for a fact about
                a loop.
        """
        self._config = config
        self._paths = paths
        self._workers = workers
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._database: Database | None = None
        #: Counters for the health endpoint and for tests.
        self.jobs_run = 0
        self.failures = 0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Run the loop on a daemon thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="vai-worker", daemon=True)
        self._thread.start()
        logger.info("Job worker started")

    def stop(self, *, timeout: float = 30.0) -> None:
        """Ask the loop to finish the current job and exit (§82).

        The running stage is not killed: a half-written render is worse than a
        slightly slower shutdown, and every stage already checks for
        cancellation at its own checkpoints.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("Job worker stopped", extra={"jobs_run": self.jobs_run})

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the loop -------------------------------------------------------

    def run(self) -> None:
        """Poll for work until stopped. Blocks; :meth:`start` is the usual way in.

        Recovery happens *here*, on this thread, before the first poll. It used
        to run in the caller before the worker started, which reads as more
        careful and is not: recovery re-queues anything RUNNING on the premise
        that nothing can be executing yet, and the moment the two live in one
        process that premise is a race. It lost it — a render was reset to
        "queued" while the worker was two clips into cutting it, so the
        interface showed work that was plainly happening as not started.

        One thread owns a job's lifecycle from recovery to completion, and the
        race cannot happen.
        """
        self._database = Database(self._paths.database_path, self._config.application.database)
        try:
            recover_stale_jobs(self._database, self._config)
        except GamingEditorError as error:
            # An unreadable database at startup is a reason to keep polling and
            # report, not to end the thread: the API is still up, and the user
            # would see a queue that never moves with nothing said about it.
            logger.warning(
                "Could not recover interrupted jobs",
                extra={"error_code": error.code},
            )
        runner = PipelineRunner(
            self._database, self._paths, self._config, workers=self._workers
        )
        try:
            while not self._stop.is_set():
                if not self._run_one(runner):
                    # Nothing to do: wait, but wake immediately on shutdown.
                    self._stop.wait(IDLE_SECONDS)
        finally:
            self._database.close()
            self._database = None

    def _run_one(self, runner: PipelineRunner) -> bool:
        """Run at most one job. Returns whether anything ran."""
        try:
            projects = self._projects_with_work()
        except GamingEditorError as error:
            # The poll itself can fail -- a locked database, or one deleted
            # underneath a still-running process. Outside the guard below, that
            # killed the thread silently and the queue stopped moving with no
            # sign of why.
            logger.warning("Could not read the job queue", extra={"error_code": error.code})
            return False

        for project_id in projects:
            if self._stop.is_set():
                return False
            try:
                outcome = runner.run_next(project_id)
            except GamingEditorError as error:
                # A typed failure is already recorded on the job row (§81); the
                # worker's job is to keep going.
                self.failures += 1
                logger.error(
                    "A stage failed",
                    extra={"project_id": project_id, "error_code": error.code},
                )
                continue
            except Exception:  # pragma: no cover - defensive
                self.failures += 1
                logger.exception(
                    "The worker hit an unexpected error",
                    extra={"project_id": project_id},
                )
                continue

            if outcome is not None:
                self.jobs_run += 1
                logger.info(
                    "Ran a stage",
                    extra={
                        "project_id": project_id,
                        "stage": outcome.job.stage.value,
                        "succeeded": outcome.succeeded,
                    },
                )
                return True
        return False

    def _projects_with_work(self) -> list[str]:
        """Projects with a queued job, oldest first.

        Ordered by when the job was queued rather than by project, so two
        people's work interleaves fairly instead of one project starving the
        other.
        """
        assert self._database is not None
        rows = self._database.fetch_all(
            "SELECT DISTINCT project_id FROM analysis_jobs WHERE status = ? "
            "ORDER BY created_at",
            (JobStatus.QUEUED.value,),
        )
        return [row["project_id"] for row in rows]

    # -- reporting ------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "jobs_run": self.jobs_run,
            "failures": self.failures,
        }


def recover_stale_jobs(database: Database, config: AppConfig) -> int:
    """Re-queue jobs left RUNNING by a process that died (§47, §90).

    A crash or a closed terminal leaves a job claiming to be running forever,
    and nothing would ever pick it up again. Called at startup, before the
    worker begins, so the first thing a restarted application does is make its
    own state true.
    """
    from backend.services.job_manager import JobManager

    recovered = JobManager(database, config).recover()
    if recovered:
        logger.warning(
            "Re-queued jobs left running by a previous process",
            extra={"count": len(recovered), "stages": [job.stage.value for job in recovered]},
        )
    return len(recovered)


def active_projects(database: Database) -> int:
    """How many projects have work outstanding. For the dashboard (§58)."""
    row = database.fetch_one(
        "SELECT COUNT(DISTINCT project_id) AS total FROM analysis_jobs "
        "WHERE status IN (?, ?)",
        (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
    )
    return int(row["total"]) if row is not None else 0


__all__ = ["IDLE_SECONDS", "JobWorker", "active_projects", "recover_stale_jobs"]
