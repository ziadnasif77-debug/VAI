"""Moment persistence (SPEC sections 28-34, 45, 79, 80).

Two columns here carry more weight than the rest. ``score_breakdown`` is the
per-dimension working behind the total (§32), and ``explanation`` is the
sentences that justify it (§80). Both are stored rather than recomputed because
the Q&A layer answers "why did you pick this?" from stored data with a
citation, and a justification regenerated later could disagree with the
decision it was supposed to explain.

``user_state`` is never written by the pipeline. §78 and §121 give the user the
final word on every moment, and an analysis re-run must not quietly revert a
choice they made.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone

from backend.core.ids import new_id
from backend.core.models.enums import GameEventType, MomentType
from backend.core.versions import ANALYSIS_VERSION
from backend.database.connection import Database, dumps, loads
from backend.gaming.correlation import GameEvent
from backend.moments.formation import Moment

_COLUMNS = (
    "id, project_id, media_id, moment_type, start_seconds, end_seconds, context_start, "
    "context_end, score, confidence, dead_time_score, repetition_score, score_breakdown, "
    "explanation, event_ids, needs_review, user_state, thumbnail_path, analysis_version, "
    "created_at"
)


class MomentRepository:
    """CRUD for the ``moments`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_media(
        self,
        project_id: str,
        media_id: str,
        moments: Iterable[Moment],
        *,
        needs_review_below: float = 0.0,
    ) -> int:
        """Replace a file's moments, preserving any user decisions.

        A user who rejected a moment and then re-ran the analysis must not find
        it accepted again (§78). Their state is read back before the delete and
        re-applied to whatever moment now occupies that instant.
        """
        preserved = self._user_states(media_id)
        now = datetime.now(timezone.utc).isoformat()

        rows = []
        for moment in moments:
            start = max(moment.start_seconds, 0.0)
            context_start = min(max(moment.context_start, 0.0), start)
            end = max(moment.end_seconds, start)
            rows.append(
                {
                    "id": new_id("moment"),
                    "project_id": project_id,
                    "media_id": media_id,
                    "moment_type": moment.moment_type.value,
                    "start_seconds": start,
                    "end_seconds": end,
                    "context_start": context_start,
                    "context_end": max(moment.context_end, end),
                    "score": min(max(moment.score, 0.0), 1.0),
                    "confidence": min(max(moment.confidence, 0.0), 1.0),
                    "dead_time_score": min(max(moment.dead_time_score, 0.0), 1.0),
                    "repetition_score": min(max(moment.repetition_score, 0.0), 1.0),
                    "score_breakdown": dumps(moment.score_breakdown),
                    "explanation": dumps(list(moment.explanation)),
                    "event_ids": dumps(
                        [event.event_type.value for event in moment.events]
                    ),
                    "needs_review": int(moment.confidence < needs_review_below),
                    "user_state": preserved.get(round(start, 3), "auto"),
                    "thumbnail_path": moment.metadata.get("thumbnail_path"),
                    "analysis_version": ANALYSIS_VERSION,
                    "created_at": now,
                }
            )

        self._db.execute("DELETE FROM moments WHERE media_id = ?", (media_id,))
        if rows:
            self._db.executemany(
                f"INSERT INTO moments ({_COLUMNS}) VALUES ("
                ":id, :project_id, :media_id, :moment_type, :start_seconds, :end_seconds, "
                ":context_start, :context_end, :score, :confidence, :dead_time_score, "
                ":repetition_score, :score_breakdown, :explanation, :event_ids, "
                ":needs_review, :user_state, :thumbnail_path, :analysis_version, :created_at)",
                rows,
            )
        return len(rows)

    def list_for_media(
        self,
        media_id: str,
        *,
        moment_type: MomentType | None = None,
        min_score: float | None = None,
        needs_review: bool | None = None,
        limit: int | None = None,
    ) -> list[Moment]:
        """Moments ranked best first — the order the review screen wants."""
        sql = f"SELECT {_COLUMNS} FROM moments WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if moment_type is not None:
            sql += " AND moment_type = ?"
            parameters.append(moment_type.value)
        if min_score is not None:
            sql += " AND score >= ?"
            parameters.append(min_score)
        if needs_review is not None:
            sql += " AND needs_review = ?"
            parameters.append(int(needs_review))
        sql += " ORDER BY score DESC, start_seconds ASC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def list_for_project(self, project_id: str, *, limit: int | None = None) -> list[Moment]:
        sql = f"SELECT {_COLUMNS} FROM moments WHERE project_id = ? ORDER BY score DESC"
        parameters: list[object] = [project_id]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def in_time_order(self, media_id: str) -> list[Moment]:
        """Moments in recording order — what the timeline needs (§40)."""
        return [
            _from_row(row)
            for row in self._db.fetch_all(
                f"SELECT {_COLUMNS} FROM moments WHERE media_id = ? "
                "ORDER BY start_seconds ASC",
                (media_id,),
            )
        ]

    def count_for_media(self, media_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM moments WHERE media_id = ?", (media_id,)
        )
        return int(row["total"]) if row is not None else 0

    def counts_by_type(self, media_id: str) -> dict[str, int]:
        return {
            str(row["moment_type"]): int(row["total"])
            for row in self._db.fetch_all(
                "SELECT moment_type, COUNT(*) AS total FROM moments "
                "WHERE media_id = ? GROUP BY moment_type",
                (media_id,),
            )
        }

    def set_user_state(self, moment_id: str, state: str) -> bool:
        """Record the user's decision about a moment (§78, §121)."""
        return (
            self._db.execute(
                "UPDATE moments SET user_state = ? WHERE id = ?", (state, moment_id)
            ).rowcount
            > 0
        )

    def total_context_seconds(self, media_id: str) -> float:
        """Total viewing time of every moment — the §39 duration budget's input."""
        row = self._db.fetch_one(
            "SELECT COALESCE(SUM(context_end - context_start), 0.0) AS total "
            "FROM moments WHERE media_id = ?",
            (media_id,),
        )
        return float(row["total"]) if row is not None else 0.0

    def delete_for_media(self, media_id: str) -> int:
        return self._db.execute(
            "DELETE FROM moments WHERE media_id = ?", (media_id,)
        ).rowcount

    def _user_states(self, media_id: str) -> dict[float, str]:
        return {
            round(float(row["start_seconds"]), 3): str(row["user_state"])
            for row in self._db.fetch_all(
                "SELECT start_seconds, user_state FROM moments "
                "WHERE media_id = ? AND user_state != 'auto'",
                (media_id,),
            )
        }


def _from_row(row: sqlite3.Row) -> Moment:
    """Rebuild a moment from storage.

    The events themselves are not reconstructed -- their types are kept so the
    moment still describes itself, but the full event records live in
    ``game_events`` and are read from there when something needs them. Storing
    them twice would mean two copies that can disagree.
    """
    known = {item.value for item in GameEventType}
    types = [value for value in (loads(row["event_ids"]) or []) if value in known]
    events = tuple(
        GameEvent(
            event_type=GameEventType(value),
            start_seconds=row["start_seconds"],
            end_seconds=row["end_seconds"],
            confidence=row["confidence"],
            importance=0.0,
            sources=(),
        )
        for value in types
    )
    breakdown = loads(row["score_breakdown"])
    explanation = loads(row["explanation"])
    return Moment(
        media_id=row["media_id"],
        moment_type=MomentType(row["moment_type"]),
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        events=events,
        context_start=row["context_start"],
        context_end=row["context_end"],
        score=row["score"],
        score_breakdown=breakdown if isinstance(breakdown, dict) else {},
        explanation=tuple(explanation) if isinstance(explanation, list) else (),
        dead_time_score=row["dead_time_score"],
        repetition_score=row["repetition_score"],
        metadata={
            "id": row["id"],
            "needs_review": bool(row["needs_review"]),
            "user_state": row["user_state"],
            "thumbnail_path": row["thumbnail_path"],
        },
    )


__all__ = ["MomentRepository"]
