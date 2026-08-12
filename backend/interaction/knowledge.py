"""Video knowledge base (§6, §7, §8).

Everything the pipeline learned about a recording -- scenes, frames, audio
events, transcript, game events, moments, scores, the current timeline --
already lives in the database. This is the read-only view over it, and it is
the single source of truth for questions.

The rule this module exists to enforce: **answering a question never re-runs
analysis**. No method here loads a model, opens a video file, or spawns a job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.core.models.enums import JobStage, MomentType
from backend.database.connection import Database, loads
from backend.database.repositories.jobs import JobRepository

#: Stages whose completion makes the corresponding questions answerable.
#: Asking "what was the best moment?" before MOMENTS has run is refused, not
#: guessed at (§17).
STAGE_FOR_MOMENTS = JobStage.MOMENTS
STAGE_FOR_EVENTS = JobStage.GAME_EVENTS
STAGE_FOR_TRANSCRIPT = JobStage.TRANSCRIPT


@dataclass(frozen=True, slots=True)
class MomentRecord:
    """A scored moment, with the data that justifies its score."""

    id: str
    moment_type: str
    start_seconds: float
    end_seconds: float
    context_start: float
    context_end: float
    score: float
    confidence: float
    dead_time_score: float
    repetition_score: float
    score_breakdown: dict[str, float]
    explanation: list[str]
    #: The *types* of the events this moment was formed around (§28). The
    #: column is called ``event_ids`` and holds types -- deliberately, so a
    #: moment describes itself without a join, and the full records stay in
    #: ``game_events`` rather than being stored twice. Named for what it holds.
    event_types: list[str]
    needs_review: bool
    user_state: str

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True, slots=True)
class GameEventRecord:
    id: str
    event_type: str
    start_seconds: float
    end_seconds: float
    confidence: float
    importance: float
    sources: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    id: str
    start_seconds: float
    end_seconds: float
    text: str
    speaker: str | None


@dataclass(frozen=True, slots=True)
class ClipRecord:
    """One clip of the current edit."""

    id: str
    clip_index: int
    media_id: str
    moment_id: str | None
    source_in: float
    source_out: float
    timeline_start: float
    timeline_end: float
    enabled: bool

    @property
    def duration_seconds(self) -> float:
        return self.timeline_end - self.timeline_start


@dataclass(frozen=True, slots=True)
class AnalysisReadiness:
    """Which questions the project can currently answer."""

    completed_stages: frozenset[JobStage]

    @property
    def has_moments(self) -> bool:
        return STAGE_FOR_MOMENTS in self.completed_stages

    @property
    def has_events(self) -> bool:
        return STAGE_FOR_EVENTS in self.completed_stages

    @property
    def has_transcript(self) -> bool:
        return STAGE_FOR_TRANSCRIPT in self.completed_stages

    @property
    def is_complete(self) -> bool:
        return self.has_moments


class VideoKnowledgeBase:
    """Read-only queries over a project's analysis results."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._jobs = JobRepository(database)

    # -- readiness ------------------------------------------------------

    def readiness(self, project_id: str) -> AnalysisReadiness:
        """Which analysis stages have finished for this project."""
        return AnalysisReadiness(frozenset(self._jobs.completed_stages(project_id)))

    # -- moments --------------------------------------------------------

    def top_moments(
        self,
        project_id: str,
        *,
        limit: int = 10,
        moment_type: MomentType | str | None = None,
        include_rejected: bool = False,
    ) -> list[MomentRecord]:
        """Highest-scoring moments, best first."""
        sql = "SELECT * FROM moments WHERE project_id = ?"
        parameters: list[Any] = [project_id]
        if moment_type is not None:
            value = moment_type.value if isinstance(moment_type, MomentType) else moment_type
            sql += " AND moment_type = ?"
            parameters.append(value)
        if not include_rejected:
            sql += " AND user_state != 'rejected'"
        sql += " ORDER BY score DESC LIMIT ?"
        parameters.append(limit)
        return [_moment(row) for row in self._db.fetch_all(sql, parameters)]

    def moment(self, moment_id: str) -> MomentRecord | None:
        row = self._db.fetch_one("SELECT * FROM moments WHERE id = ?", (moment_id,))
        return _moment(row) if row is not None else None

    def moments_at(
        self, project_id: str, timestamp: float, *, window_seconds: float = 15.0
    ) -> list[MomentRecord]:
        """Moments overlapping ``timestamp`` plus or minus a window (§8)."""
        rows = self._db.fetch_all(
            "SELECT * FROM moments WHERE project_id = ? "
            "AND context_end >= ? AND context_start <= ? ORDER BY start_seconds",
            (project_id, timestamp - window_seconds, timestamp + window_seconds),
        )
        return [_moment(row) for row in rows]

    def rejected_moments(self, project_id: str, *, limit: int = 10) -> list[MomentRecord]:
        """Moments the pipeline or the user excluded -- "what did you leave out?"."""
        rows = self._db.fetch_all(
            "SELECT * FROM moments WHERE project_id = ? AND user_state = 'rejected' "
            "ORDER BY score DESC LIMIT ?",
            (project_id, limit),
        )
        return [_moment(row) for row in rows]

    def moment_type_counts(self, project_id: str) -> dict[str, int]:
        rows = self._db.fetch_all(
            "SELECT moment_type, COUNT(*) AS total FROM moments WHERE project_id = ? "
            "GROUP BY moment_type ORDER BY total DESC",
            (project_id,),
        )
        return {row["moment_type"]: int(row["total"]) for row in rows}

    # -- events ---------------------------------------------------------

    def events(
        self,
        project_id: str,
        *,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[GameEventRecord]:
        sql = "SELECT * FROM game_events WHERE project_id = ?"
        parameters: list[Any] = [project_id]
        if event_type is not None:
            sql += " AND event_type = ?"
            parameters.append(event_type)
        sql += " ORDER BY start_seconds LIMIT ?"
        parameters.append(limit)
        return [_event(row) for row in self._db.fetch_all(sql, parameters)]

    def event_counts(self, project_id: str) -> dict[str, int]:
        rows = self._db.fetch_all(
            "SELECT event_type, COUNT(*) AS total FROM game_events WHERE project_id = ? "
            "GROUP BY event_type ORDER BY total DESC",
            (project_id,),
        )
        return {row["event_type"]: int(row["total"]) for row in rows}

    def events_at(
        self, project_id: str, timestamp: float, *, window_seconds: float = 15.0
    ) -> list[GameEventRecord]:
        rows = self._db.fetch_all(
            "SELECT * FROM game_events WHERE project_id = ? "
            "AND end_seconds >= ? AND start_seconds <= ? ORDER BY start_seconds",
            (project_id, timestamp - window_seconds, timestamp + window_seconds),
        )
        return [_event(row) for row in rows]

    # -- transcript -----------------------------------------------------

    def transcript_at(
        self, project_id: str, timestamp: float, *, window_seconds: float = 15.0
    ) -> list[TranscriptRecord]:
        rows = self._db.fetch_all(
            "SELECT * FROM transcript_segments WHERE project_id = ? "
            "AND end_seconds >= ? AND start_seconds <= ? ORDER BY start_seconds",
            (project_id, timestamp - window_seconds, timestamp + window_seconds),
        )
        return [
            TranscriptRecord(
                id=row["id"],
                start_seconds=row["start_seconds"],
                end_seconds=row["end_seconds"],
                text=row["text"],
                speaker=row["speaker"],
            )
            for row in rows
        ]

    def search_transcript(
        self, project_id: str, phrase: str, *, limit: int = 10
    ) -> list[TranscriptRecord]:
        rows = self._db.fetch_all(
            "SELECT * FROM transcript_segments WHERE project_id = ? "
            "AND lower(text) LIKE ? ORDER BY start_seconds LIMIT ?",
            (project_id, f"%{phrase.lower()}%", limit),
        )
        return [
            TranscriptRecord(
                id=row["id"],
                start_seconds=row["start_seconds"],
                end_seconds=row["end_seconds"],
                text=row["text"],
                speaker=row["speaker"],
            )
            for row in rows
        ]

    # -- current edit ---------------------------------------------------

    def clips(self, project_id: str, *, enabled_only: bool = True) -> list[ClipRecord]:
        """The current timeline, in order."""
        sql = "SELECT * FROM timeline_clips WHERE project_id = ? AND track = 'video'"
        parameters: list[Any] = [project_id]
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY clip_index"
        return [_clip(row) for row in self._db.fetch_all(sql, parameters)]

    def edit_duration_seconds(self, project_id: str) -> float:
        """Duration of the *current edit*, not of the source (§18)."""
        row = self._db.fetch_one(
            "SELECT COALESCE(SUM(timeline_end - timeline_start), 0.0) AS total "
            "FROM timeline_clips WHERE project_id = ? AND track = 'video' AND enabled = 1",
            (project_id,),
        )
        return float(row["total"]) if row is not None else 0.0

    def clip_count(self, project_id: str, *, enabled_only: bool = True) -> int:
        sql = (
            "SELECT COUNT(*) AS total FROM timeline_clips WHERE project_id = ? AND track = 'video'"
        )
        if enabled_only:
            sql += " AND enabled = 1"
        row = self._db.fetch_one(sql, (project_id,))
        return int(row["total"]) if row is not None else 0

    def source_duration_seconds(self, project_id: str) -> float:
        row = self._db.fetch_one(
            "SELECT COALESCE(SUM(duration_seconds), 0.0) AS total FROM media "
            "WHERE project_id = ? AND role = 'gameplay'",
            (project_id,),
        )
        return float(row["total"]) if row is not None else 0.0


def _moment(row: Any) -> MomentRecord:
    return MomentRecord(
        id=row["id"],
        moment_type=row["moment_type"],
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        context_start=row["context_start"],
        context_end=row["context_end"],
        score=row["score"],
        confidence=row["confidence"],
        dead_time_score=row["dead_time_score"],
        repetition_score=row["repetition_score"],
        score_breakdown=loads(row["score_breakdown"], {}),
        explanation=loads(row["explanation"], []),
        event_types=loads(row["event_ids"], []),
        needs_review=bool(row["needs_review"]),
        user_state=row["user_state"],
    )


def _event(row: Any) -> GameEventRecord:
    return GameEventRecord(
        id=row["id"],
        event_type=row["event_type"],
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        confidence=row["confidence"],
        importance=row["importance"],
        sources=loads(row["sources"], []),
        metadata=loads(row["metadata"], {}),
    )


def _clip(row: Any) -> ClipRecord:
    return ClipRecord(
        id=row["id"],
        clip_index=row["clip_index"],
        media_id=row["media_id"],
        moment_id=row["moment_id"],
        source_in=row["source_in"],
        source_out=row["source_out"],
        timeline_start=row["timeline_start"],
        timeline_end=row["timeline_end"],
        enabled=bool(row["enabled"]),
    )


__all__ = [
    "AnalysisReadiness",
    "ClipRecord",
    "GameEventRecord",
    "MomentRecord",
    "TranscriptRecord",
    "VideoKnowledgeBase",
]
