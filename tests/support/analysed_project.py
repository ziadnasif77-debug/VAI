"""Rows that stand in for a finished analysis.

The interaction layer answers questions and applies commands from what the
pipeline *stored*, never from the video. So the tests for it need a project
with plausible analysis rows and no pipeline run — these are the inserts that
produce one.

They live here rather than in a test file because both the unit tests for the
interaction layer and the Phase 13 acceptance test need the same starting
state, and a second copy of this SQL would be a second place to update when the
schema moves.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.core.ids import new_id
from backend.core.models.enums import JobStatus
from backend.database.connection import Database, dumps
from backend.services.job_manager import JobManager


def insert_moment(
    database: Database,
    project_id: str,
    media_id: str,
    *,
    moment_type: str = "clutch",
    start: float = 100.0,
    end: float | None = None,
    score: float = 0.9,
    confidence: float = 0.9,
    explanation: list[str] | None = None,
    breakdown: dict[str, float] | None = None,
    user_state: str = "auto",
    dead_time: float = 0.0,
) -> str:
    # Derive the end from the start so a caller only has to move `start`.
    end = start + 30.0 if end is None else end
    moment_id = new_id("moment")
    database.execute(
        "INSERT INTO moments (id, project_id, media_id, moment_type, start_seconds, "
        "end_seconds, context_start, context_end, score, confidence, dead_time_score, "
        "repetition_score, score_breakdown, explanation, event_ids, needs_review, "
        "user_state, analysis_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, '[]', 0, ?, 1, ?)",
        (
            moment_id,
            project_id,
            media_id,
            moment_type,
            start,
            end,
            max(start - 8, 0),
            end + 6,
            score,
            confidence,
            dead_time,
            dumps(breakdown or {}),
            dumps(explanation or []),
            user_state,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return moment_id


def insert_event(
    database: Database,
    project_id: str,
    media_id: str,
    *,
    event_type: str = "kill",
    start: float = 100.0,
    end: float | None = None,
    confidence: float = 0.9,
) -> str:
    end = start + 5.0 if end is None else end
    event_id = new_id("game_event")
    database.execute(
        "INSERT INTO game_events (id, project_id, media_id, event_type, start_seconds, "
        "end_seconds, confidence, importance, sources, analysis_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0.8, ?, 1, ?)",
        (
            event_id,
            project_id,
            media_id,
            event_type,
            start,
            end,
            confidence,
            dumps(["vision", "audio"]),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return event_id


def insert_clip(
    database: Database,
    project_id: str,
    media_id: str,
    *,
    index: int,
    timeline_start: float,
    duration: float,
    moment_id: str | None = None,
    enabled: bool = True,
) -> str:
    clip_id = new_id("timeline_clip")
    database.execute(
        "INSERT INTO timeline_clips (id, project_id, media_id, moment_id, track, clip_index, "
        "source_in, source_out, timeline_start, timeline_end, enabled) "
        "VALUES (?, ?, ?, ?, 'video', ?, ?, ?, ?, ?, ?)",
        (
            clip_id,
            project_id,
            media_id,
            moment_id,
            index,
            timeline_start,
            timeline_start + duration,
            timeline_start,
            timeline_start + duration,
            int(enabled),
        ),
    )
    return clip_id


def complete_analysis(job_manager: JobManager, project_id: str, media_id: str) -> None:
    """Mark every analysis stage complete so questions become answerable."""
    from backend.core.models.jobs import ANALYSIS_STAGES
    from backend.services.job_manager import PER_MEDIA_STAGES

    for stage in ANALYSIS_STAGES:
        job = job_manager.queue(
            project_id, stage, media_id=media_id if stage in PER_MEDIA_STAGES else None
        )
        job_manager._jobs.update(
            job.model_copy(update={"status": JobStatus.COMPLETED, "progress": 1.0})
        )


__all__ = ["complete_analysis", "insert_clip", "insert_event", "insert_moment"]
