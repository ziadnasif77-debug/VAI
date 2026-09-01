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
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

from backend.core.ids import new_id
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import GameEventType, MomentType
from backend.core.versions import ANALYSIS_VERSION
from backend.database.connection import Database, dumps, loads
from backend.gaming.correlation import GameEvent
from backend.moments.formation import Moment

logger = get_logger("database.moments", LogChannel.PIPELINE)

#: Event type names this project has used and renamed.
#:
#: ``unexpected_event`` became ``unknown_event`` in V2-P2, which migrated the
#: `game_events` table and did not migrate the `event_ids` JSON on `moments`.
#: The loader filtered stored names against the enum and dropped whatever did
#: not match, silently, for every read since -- **623 of 1,119 references on
#: this machine, and 210 of 435 moments left with no events at all.** Every
#: one of those was a moment whose events were all unnamed, which is exactly
#: the population `surprise` describes.
#:
#: A rename is a migration whether or not anyone wrote one. This is that
#: migration, applied at read time so no stored row has to be rewritten and an
#: older database keeps working.
_RENAMED_EVENT_TYPES: dict[str, str] = {
    "unexpected_event": GameEventType.UNKNOWN_EVENT.value,
}

_COLUMNS = (
    "id, project_id, media_id, moment_type, start_seconds, end_seconds, context_start, "
    "context_end, score, confidence, dead_time_score, repetition_score, score_breakdown, "
    "explanation, event_ids, needs_review, user_state, thumbnail_path, analysis_version, "
    "created_at, phases"
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
                    # V2-P2: the moment's own shape, measured rather than
                    # assumed, for every stage that needs to know where
                    # inside it the payoff is.
                    "phases": dumps(moment.metadata.get("phases", [])),
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
                ":needs_review, :user_state, :thumbnail_path, :analysis_version, "
                ":created_at, :phases)",
                rows,
            )
        return len(rows)

    def _events_for(self, media_ids: "Iterable[str]") -> dict[str, list[GameEvent]]:
        """Every recording's game events, once per recording.

        Batched deliberately. A moment-by-moment fetch is one query per moment
        per read, and the story stage reads every moment of a project; the
        editorial reading learned the same lesson in V2-P11 and gathers its
        stores exactly once for the same reason.
        """
        from backend.database.repositories.gaming import GameEventRepository

        repository = GameEventRepository(self._db)
        found: dict[str, list[GameEvent]] = {}
        for media_id in dict.fromkeys(media_ids):
            if not media_id:
                continue
            try:
                found[media_id] = list(repository.list_for_media(media_id))
            except Exception:
                # A moment whose events cannot be read is loaded with named
                # placeholders rather than not at all -- the §95 rule, applied
                # to storage.
                logger.warning(
                    "Could not read this recording's events; moments keep "
                    "placeholder spans",
                    extra={"media_id": media_id},
                )
        return found

    def _hydrated(self, rows: "Sequence[sqlite3.Row]") -> list[Moment]:
        """Rows as moments, carrying their real events wherever they exist."""
        events = self._events_for(row["media_id"] for row in rows)
        return [_from_row(row, events.get(row["media_id"])) for row in rows]

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
        return self._hydrated(self._db.fetch_all(sql, parameters))

    def list_for_project(self, project_id: str, *, limit: int | None = None) -> list[Moment]:
        sql = f"SELECT {_COLUMNS} FROM moments WHERE project_id = ? ORDER BY score DESC"
        parameters: list[object] = [project_id]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return self._hydrated(self._db.fetch_all(sql, parameters))

    def in_time_order(self, media_id: str) -> list[Moment]:
        """Moments in recording order — what the timeline needs (§40)."""
        return self._hydrated(
            self._db.fetch_all(
                f"SELECT {_COLUMNS} FROM moments WHERE media_id = ? "
                "ORDER BY start_seconds ASC",
                (media_id,),
            )
        )

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


def _restore(
    row: sqlite3.Row, types: list[str], stored: "Sequence[GameEvent] | None"
) -> tuple[GameEvent, ...]:
    """This moment's real events, or named placeholders when they cannot be had.

    The match is by span and by type multiset together. `event_ids` records
    types and not identifiers, so the events inside the moment's span are the
    candidates -- and they are only accepted when the types they carry are
    exactly the types the row recorded. A partial match means the stores have
    moved under this moment, and inventing a correspondence there would be the
    reinterpretation this repository is not allowed to do.
    """
    if not types:
        return ()
    if stored:
        start, end = row["start_seconds"], row["end_seconds"]
        inside = [
            event
            for event in stored
            if event.end_seconds > start and event.start_seconds < end
        ]
        if sorted(event.event_type.value for event in inside) == sorted(types):
            return tuple(sorted(inside, key=lambda event: event.start_seconds))
    return tuple(
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


def _stored_types(row: sqlite3.Row) -> tuple[list[str], list[str]]:
    """The event type names on a moment row, renamed, and the ones left over.

    Returns ``(resolved, unresolvable)``. Nothing is dropped without appearing
    in the second list, because that is how 56 % of this database's event
    references went missing for a phase and a half: the loader filtered against
    the enum and said nothing about what it removed.
    """
    known = {item.value for item in GameEventType}
    resolved: list[str] = []
    unresolvable: list[str] = []
    for value in loads(row["event_ids"]) or []:
        name = _RENAMED_EVENT_TYPES.get(str(value), str(value))
        (resolved if name in known else unresolvable).append(name)
    return resolved, unresolvable


def _from_row(row: sqlite3.Row, stored: Sequence[GameEvent] | None = None) -> Moment:
    """Rebuild a moment from storage.

    ``stored`` is this recording's real events, when the caller fetched them.
    With it, the moment carries what actually happened -- each event's own
    span, importance, confidence and sources. Without it the types are kept so
    the moment still describes itself, and the spans are the moment's own.

    That fallback used to be the only behaviour, on the reasoning that the full
    records live in ``game_events`` and storing them twice would mean two
    copies that can disagree. The reasoning holds; what it missed is that the
    placeholders are **indistinguishable from real values**. `Moment.importance`
    read 0.0 for every loaded moment, every event reported the moment's own
    span, and the layers above could not tell. So the records are read from the
    one source rather than fabricated, and `game_events` remains the truth.
    """
    types, unresolvable = _stored_types(row)
    if unresolvable:
        logger.warning(
            "A moment references event types this build does not know",
            extra={
                "moment_id": row["id"],
                "unresolvable": sorted(set(unresolvable)),
                "count": len(unresolvable),
            },
        )
    events = _restore(row, types, stored)
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
            "phases": _phases(row),
        },
    )


def _phases(row) -> list[dict]:
    """The stored phases, or none. A moment written before V2-P2 has no
    column value, and an absent shape is not a flat one."""
    try:
        stored = row["phases"]
    except (IndexError, KeyError):
        return []
    if not stored:
        return []
    try:
        loaded = loads(stored)
    except ValueError:
        return []
    return loaded if isinstance(loaded, list) else []


__all__ = ["MomentRepository"]
