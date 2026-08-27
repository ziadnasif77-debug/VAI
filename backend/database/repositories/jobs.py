"""Job persistence (SPEC sections 46, 47, 81, 82)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from backend.core.errors import ErrorCode, NotFoundError
from backend.core.models.enums import JobStage, JobStatus
from backend.core.models.jobs import Job
from backend.database.connection import Database, dumps, loads

_COLUMNS = (
    "id, project_id, media_id, stage, status, progress, message, attempt, max_attempts, "
    "error_code, error_message, cancel_requested, created_at, started_at, completed_at, "
    "duration_seconds, payload, result"
)


class JobRepository:
    """CRUD for the ``analysis_jobs`` table.

    One row per (project, media, stage). Retries update the row rather than
    inserting a new one, so resume can find the stage's state without scanning
    a history table (§47).
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def create(self, job: Job) -> Job:
        self._db.execute(
            f"INSERT INTO analysis_jobs ({_COLUMNS}) VALUES ("
            ":id, :project_id, :media_id, :stage, :status, :progress, :message, :attempt, "
            ":max_attempts, :error_code, :error_message, :cancel_requested, :created_at, "
            ":started_at, :completed_at, :duration_seconds, :payload, :result)",
            _to_row(job),
        )
        return job

    def get(self, job_id: str) -> Job | None:
        row = self._db.fetch_one(f"SELECT {_COLUMNS} FROM analysis_jobs WHERE id = ?", (job_id,))
        return _from_row(row) if row is not None else None

    def require(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise NotFoundError(
                f"Job {job_id!r} does not exist.",
                code=ErrorCode.JOB_NOT_FOUND,
                details={"job_id": job_id},
                recoverable=False,
            )
        return job

    def find(self, project_id: str, stage: JobStage, media_id: str | None = None) -> Job | None:
        """Return the job for one stage of a project (optionally per media)."""
        if media_id is None:
            row = self._db.fetch_one(
                f"SELECT {_COLUMNS} FROM analysis_jobs "
                "WHERE project_id = ? AND stage = ? AND media_id IS NULL",
                (project_id, stage.value),
            )
        else:
            row = self._db.fetch_one(
                f"SELECT {_COLUMNS} FROM analysis_jobs "
                "WHERE project_id = ? AND stage = ? AND media_id = ?",
                (project_id, stage.value, media_id),
            )
        return _from_row(row) if row is not None else None

    def list_for_project(self, project_id: str, *, status: JobStatus | None = None) -> list[Job]:
        sql = f"SELECT {_COLUMNS} FROM analysis_jobs WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if status is not None:
            sql += " AND status = ?"
            parameters.append(status.value)
        sql += " ORDER BY created_at ASC"
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def completed_stages(self, project_id: str) -> set[JobStage]:
        """Stages fully completed for a project.

        A stage counts as complete only when no job for it is unfinished, so a
        project with three media files is not "probed" until all three are.
        """
        rows = self._db.fetch_all(
            "SELECT stage, "
            "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS done, "
            "COUNT(*) AS total "
            "FROM analysis_jobs WHERE project_id = ? GROUP BY stage",
            (project_id,),
        )
        return {
            JobStage(row["stage"])
            for row in rows
            if row["total"] > 0 and row["done"] == row["total"]
        }

    def update(self, job: Job) -> Job:
        cursor = self._db.execute(
            "UPDATE analysis_jobs SET status = :status, progress = :progress, "
            "message = :message, attempt = :attempt, max_attempts = :max_attempts, "
            "error_code = :error_code, error_message = :error_message, "
            "cancel_requested = :cancel_requested, started_at = :started_at, "
            "completed_at = :completed_at, duration_seconds = :duration_seconds, "
            "payload = :payload, result = :result WHERE id = :id",
            _to_row(job),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(
                f"Job {job.id!r} does not exist.",
                code=ErrorCode.JOB_NOT_FOUND,
                details={"job_id": job.id},
                recoverable=False,
            )
        return job

    def request_cancel(self, project_id: str) -> int:
        """Cancel every unfinished job of a project (§82).

        For a **running** job the flag is advisory: a worker stops at its next
        checkpoint, so the project is never left half-written.

        A **queued** job has no worker to reach a checkpoint, so the flag alone
        is not a cancellation -- it is a job that will never run and never say
        so. `next_runnable` skips it, correctly, and it sits at "queued"
        forever while the screen reads as "about to start". One project spent
        eight hours that way with the worker healthy and idle beside it.
        Nothing is executing, so nothing needs interrupting: it is cancelled
        here and now.
        """
        finished_at = datetime.now(timezone.utc).isoformat()
        cursor = self._db.execute(
            "UPDATE analysis_jobs SET cancel_requested = 1, status = 'cancelled', "
            "completed_at = ? WHERE project_id = ? AND status = 'queued'",
            (finished_at, project_id),
        )
        settled = cursor.rowcount
        cursor = self._db.execute(
            "UPDATE analysis_jobs SET cancel_requested = 1 "
            "WHERE project_id = ? AND status = 'running'",
            (project_id,),
        )
        return settled + cursor.rowcount

    def abandoned_queued_jobs(self, project_id: str | None = None) -> list[Job]:
        """Queued jobs flagged for cancellation, which nothing will ever run.

        Rows written by an older build, before cancelling a queued job settled
        it immediately. Startup is where state is made true (§47), so this is
        read there.
        """
        sql = (
            f"SELECT {_COLUMNS} FROM analysis_jobs WHERE status = 'queued' AND cancel_requested = 1"
        )
        params: tuple[object, ...] = ()
        if project_id is not None:
            sql += " AND project_id = ?"
            params = (project_id,)
        return [_from_row(row) for row in self._db.fetch_all(sql, params)]

    def project_ids(self) -> list[str]:
        """Every project that has ever queued a job, for startup-wide passes."""
        rows = self._db.fetch_all("SELECT DISTINCT project_id FROM analysis_jobs ORDER BY project_id")
        return [str(row["project_id"]) for row in rows]

    def stale_running_jobs(self, project_id: str | None = None) -> list[Job]:
        """Jobs left RUNNING by a crash (§47).

        Nothing runs at startup, so any RUNNING row is a leftover and is
        eligible for resume.
        """
        if project_id is None:
            rows = self._db.fetch_all(
                f"SELECT {_COLUMNS} FROM analysis_jobs WHERE status = 'running'"
            )
        else:
            rows = self._db.fetch_all(
                f"SELECT {_COLUMNS} FROM analysis_jobs WHERE status = 'running' AND project_id = ?",
                (project_id,),
            )
        return [_from_row(row) for row in rows]

    def delete_for_stages(self, project_id: str, stages: set[JobStage]) -> int:
        """Remove jobs for the given stages.

        Used when a re-edit invalidates downstream stages: §127 requires the
        edit stages to re-run while the analysis stages are preserved.
        """
        if not stages:
            return 0
        placeholders = ", ".join("?" for _ in stages)
        cursor = self._db.execute(
            f"DELETE FROM analysis_jobs WHERE project_id = ? AND stage IN ({placeholders})",
            [project_id, *(stage.value for stage in stages)],
        )
        return cursor.rowcount


def _to_row(job: Job) -> dict[str, object]:
    return {
        "id": job.id,
        "project_id": job.project_id,
        "media_id": job.media_id,
        "stage": job.stage.value,
        "status": job.status.value,
        "progress": job.progress,
        "message": job.message,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "cancel_requested": int(job.cancel_requested),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "duration_seconds": job.duration_seconds,
        "payload": dumps(job.payload),
        "result": dumps(job.result),
    }


def _from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        project_id=row["project_id"],
        media_id=row["media_id"],
        stage=JobStage(row["stage"]),
        status=JobStatus(row["status"]),
        progress=row["progress"],
        message=row["message"],
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        cancel_requested=bool(row["cancel_requested"]),
        created_at=_parse(row["created_at"]),
        started_at=_parse(row["started_at"]),
        completed_at=_parse(row["completed_at"]),
        duration_seconds=row["duration_seconds"],
        payload=loads(row["payload"], {}),
        result=loads(row["result"], {}),
    )


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


__all__ = ["JobRepository"]
