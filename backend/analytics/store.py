"""Where outcomes are kept, and what they must name to be kept (V2-P9).

One rule enforced here rather than left to callers: an outcome belongs to a
project. A video this system did not publish has no edit to attribute, and
storing it anyway would produce a number nobody can trace back to a decision.
A number that cannot be attributed is not evidence, and evidence is the whole
point of this phase.

The attribution is read from the PUBLISH job's own result, not from the
``publications`` table. That table exists in the first schema and nothing has
ever written a row to it -- the publish worker says so in its own docstring,
having decided that the job history *is* the publication history. The first
draft of this module queried it anyway, which would have made every fetch
report "no video to measure" for ever while looking perfectly correct.

Nothing here interprets. :mod:`backend.analytics.projection` is where a curve
meets the edit that produced it, and P10 is the only phase permitted to change
anything because of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.analytics.youtube import RetentionPoint, Totals
from backend.core.errors import ErrorCode, ValidationError
from backend.core.ids import new_id
from backend.core.logging import LogChannel, get_logger
from backend.database.connection import dumps, loads

logger = get_logger("analytics.store", LogChannel.APPLICATION)

#: The API's names, mapped to the columns this schema chose to name.
_COLUMNS: dict[str, str] = {
    "views": "views",
    "estimatedMinutesWatched": "estimated_minutes_watched",
    "averageViewDuration": "average_view_duration_seconds",
    "averageViewPercentage": "average_view_percentage",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "subscribersGained": "subscribers_gained",
}


@dataclass(frozen=True, slots=True)
class Outcome:
    """One stored measurement of one video over one window."""

    id: str
    project_id: str
    video_id: str
    start_date: str
    end_date: str
    fetched_at: str
    metrics: dict[str, float]
    points: tuple[RetentionPoint, ...] = ()

    @property
    def has_curve(self) -> bool:
        return bool(self.points)


class OutcomeStore:
    """Reads and writes ``video_outcomes`` and ``retention_points``."""

    def __init__(self, database: Any) -> None:
        self._db = database

    # -- attribution --------------------------------------------------------

    def project_of(self, video_id: str) -> tuple[str, str] | None:
        """``(project_id, publish_job_id)`` for a video this system published."""
        for row in self._publishes():
            if row["video_id"] == video_id:
                return (row["project_id"], row["publish_job_id"])
        return None

    def published(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Videos this system put on YouTube, newest first.

        The candidate list for a fetch. A channel may hold videos this system
        never made, and those are deliberately absent: an outcome it cannot
        attribute to an edit is not something this phase can use.
        """
        return self._publishes()[:limit]

    def _publishes(self) -> list[dict[str, Any]]:
        """Completed PUBLISH jobs that came back with a video id.

        The publish worker stores its outcome on the job row like every other
        stage, and that result carries ``external_id`` -- the video id YouTube
        assigned. The style the edit was cut with joins on the project.
        """
        found: list[dict[str, Any]] = []
        for row in self._db.fetch_all(
            "SELECT j.id, j.project_id, j.result, j.completed_at, "
            "e.style, e.version AS style_version "
            "FROM analysis_jobs j "
            "LEFT JOIN edit_styles e ON e.project_id = j.project_id "
            "WHERE j.stage = 'publish' AND j.status = 'completed' "
            "ORDER BY j.completed_at DESC",
            (),
        ):
            result = loads(row["result"] or "{}") or {}
            video_id = result.get("external_id")
            if not video_id or result.get("target") != "youtube":
                continue
            found.append(
                {
                    "publish_job_id": row["id"],
                    "project_id": row["project_id"],
                    "video_id": str(video_id),
                    "completed_at": row["completed_at"],
                    "style": row["style"],
                    "style_version": row["style_version"],
                }
            )
        return found

    # -- writing ------------------------------------------------------------

    def record(
        self, totals: Totals, points: Sequence[RetentionPoint] = ()
    ) -> Outcome:
        """Store one window's measurement, replacing an earlier read of it.

        Raises when the video cannot be attributed: see the module docstring.
        Re-fetching the same window updates the row rather than adding a second
        opinion, because two reads of one window are one fact measured twice.
        """
        found = self.project_of(totals.video_id)
        if found is None:
            raise ValidationError(
                f"No completed publish job names video {totals.video_id!r}, so "
                f"there is no edit to attribute this outcome to.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"video_id": totals.video_id},
                recoverable=False,
            )
        project_id, publish_job_id = found
        existing = self._db.fetch_one(
            "SELECT id FROM video_outcomes "
            "WHERE video_id = ? AND start_date = ? AND end_date = ?",
            (totals.video_id, totals.start_date, totals.end_date),
        )
        outcome_id = existing["id"] if existing else new_id("job").replace("job-", "out-")
        stored = {
            column: totals.get(name) for name, column in _COLUMNS.items()
        }
        fetched_at = datetime.now(timezone.utc).isoformat()
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO video_outcomes "
                "(id, project_id, publish_job_id, video_id, start_date, end_date, "
                "fetched_at, views, estimated_minutes_watched, "
                "average_view_duration_seconds, average_view_percentage, likes, "
                "comments, shares, subscribers_gained, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(video_id, start_date, end_date) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, views = excluded.views, "
                "estimated_minutes_watched = excluded.estimated_minutes_watched, "
                "average_view_duration_seconds = excluded.average_view_duration_seconds, "
                "average_view_percentage = excluded.average_view_percentage, "
                "likes = excluded.likes, comments = excluded.comments, "
                "shares = excluded.shares, "
                "subscribers_gained = excluded.subscribers_gained, "
                "raw = excluded.raw",
                (
                    outcome_id,
                    project_id,
                    publish_job_id,
                    totals.video_id,
                    totals.start_date,
                    totals.end_date,
                    fetched_at,
                    stored["views"],
                    stored["estimated_minutes_watched"],
                    stored["average_view_duration_seconds"],
                    stored["average_view_percentage"],
                    stored["likes"],
                    stored["comments"],
                    stored["shares"],
                    stored["subscribers_gained"],
                    dumps(totals.raw),
                ),
            )
            self._db.execute(
                "DELETE FROM retention_points WHERE outcome_id = ?", (outcome_id,)
            )
            for point in points:
                self._db.execute(
                    "INSERT INTO retention_points "
                    "(outcome_id, elapsed_ratio, audience_watch_ratio, "
                    "relative_performance) VALUES (?, ?, ?, ?)",
                    (
                        outcome_id,
                        round(float(point.elapsed_ratio), 6),
                        float(point.audience_watch_ratio),
                        point.relative_performance,
                    ),
                )
        logger.info(
            "An outcome was recorded against the edit that produced it",
            extra={
                "project_id": project_id,
                "video_id": totals.video_id,
                "points": len(points),
            },
        )
        return Outcome(
            id=outcome_id,
            project_id=project_id,
            video_id=totals.video_id,
            start_date=totals.start_date,
            end_date=totals.end_date,
            fetched_at=fetched_at,
            metrics=dict(totals.metrics),
            points=tuple(points),
        )

    # -- reading ------------------------------------------------------------

    def latest(self, project_id: str) -> Outcome | None:
        """The newest window measured for this project's video."""
        row = self._db.fetch_one(
            "SELECT * FROM video_outcomes WHERE project_id = ? "
            "ORDER BY end_date DESC, fetched_at DESC LIMIT 1",
            (project_id,),
        )
        return self._outcome(row) if row is not None else None

    def all_outcomes(self, *, limit: int = 100) -> list[Outcome]:
        return [
            self._outcome(row)
            for row in self._db.fetch_all(
                "SELECT * FROM video_outcomes ORDER BY end_date DESC LIMIT ?",
                (limit,),
            )
        ]

    def count(self) -> int:
        row = self._db.fetch_one("SELECT COUNT(*) AS total FROM video_outcomes")
        return int(row["total"]) if row else 0

    def _outcome(self, row: Any) -> Outcome:
        points = tuple(
            RetentionPoint(
                elapsed_ratio=float(item["elapsed_ratio"]),
                audience_watch_ratio=float(item["audience_watch_ratio"]),
                relative_performance=(
                    None
                    if item["relative_performance"] is None
                    else float(item["relative_performance"])
                ),
            )
            for item in self._db.fetch_all(
                "SELECT elapsed_ratio, audience_watch_ratio, relative_performance "
                "FROM retention_points WHERE outcome_id = ? ORDER BY elapsed_ratio",
                (row["id"],),
            )
        )
        metrics = {
            name: float(row[column])
            for name, column in _COLUMNS.items()
            if row[column] is not None
        }
        return Outcome(
            id=row["id"],
            project_id=row["project_id"],
            video_id=row["video_id"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            fetched_at=row["fetched_at"],
            metrics=metrics,
            points=points,
        )

    def raw_of(self, outcome_id: str) -> dict[str, Any]:
        row = self._db.fetch_one(
            "SELECT raw FROM video_outcomes WHERE id = ?", (outcome_id,)
        )
        return (loads(row["raw"]) if row else {}) or {}


__all__ = ["Outcome", "OutcomeStore"]
