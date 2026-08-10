"""Audio event persistence (SPEC sections 18, 19, 26, 45).

Every row records **which track heard it**. §19 is the reason: a spike on the
game track and a spike on the microphone are different evidence, and a schema
that lost that distinction would make the two indistinguishable to the
correlation stage that most depends on it (§27).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from backend.analysis.audio_events import AudioEvent, TrackRole
from backend.core.ids import new_id
from backend.core.models.enums import AudioEventType
from backend.database.connection import Database, dumps, loads

_COLUMNS = (
    "id, project_id, media_id, track_role, event_type, start_seconds, end_seconds, "
    "confidence, rms_db, peak_db, lufs, metadata"
)


class AudioEventRepository:
    """CRUD for the ``audio_events`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_media(
        self, project_id: str, media_id: str, events: Iterable[AudioEvent]
    ) -> int:
        """Replace every audio event of one file.

        Wholesale, like the transcript: a re-run means thresholds or the audio
        changed, and merging would leave events from a configuration nobody is
        using.
        """
        rows = [
            {
                "id": new_id("audio_event"),
                "project_id": project_id,
                "media_id": media_id,
                "track_role": event.track_role,
                "event_type": event.event_type.value,
                "start_seconds": max(event.start_seconds, 0.0),
                "end_seconds": max(event.end_seconds, event.start_seconds),
                "confidence": min(max(event.confidence, 0.0), 1.0),
                "rms_db": event.rms_db,
                "peak_db": event.peak_db,
                "lufs": event.lufs,
                "metadata": dumps(event.metadata),
            }
            for event in events
        ]
        self._db.execute("DELETE FROM audio_events WHERE media_id = ?", (media_id,))
        if rows:
            self._db.executemany(
                f"INSERT INTO audio_events ({_COLUMNS}) VALUES ("
                ":id, :project_id, :media_id, :track_role, :event_type, :start_seconds, "
                ":end_seconds, :confidence, :rms_db, :peak_db, :lufs, :metadata)",
                rows,
            )
        return len(rows)

    def list_for_media(
        self,
        media_id: str,
        *,
        track_role: TrackRole | None = None,
        event_type: AudioEventType | None = None,
        start: float | None = None,
        end: float | None = None,
        min_confidence: float | None = None,
    ) -> list[AudioEvent]:
        """Events in chronological order, filtered as asked."""
        sql = f"SELECT {_COLUMNS} FROM audio_events WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if track_role is not None:
            sql += " AND track_role = ?"
            parameters.append(track_role)
        if event_type is not None:
            sql += " AND event_type = ?"
            parameters.append(event_type.value)
        if start is not None:
            sql += " AND end_seconds >= ?"
            parameters.append(start)
        if end is not None:
            sql += " AND start_seconds <= ?"
            parameters.append(end)
        if min_confidence is not None:
            sql += " AND confidence >= ?"
            parameters.append(min_confidence)
        sql += " ORDER BY start_seconds ASC"
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def list_for_project(
        self, project_id: str, *, track_role: TrackRole | None = None
    ) -> list[AudioEvent]:
        sql = f"SELECT {_COLUMNS} FROM audio_events WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if track_role is not None:
            sql += " AND track_role = ?"
            parameters.append(track_role)
        sql += " ORDER BY start_seconds ASC"
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def counts_by_type(self, media_id: str) -> dict[str, int]:
        """Tally per event type, for the analysis screen and the stage log."""
        return {
            str(row["event_type"]): int(row["total"])
            for row in self._db.fetch_all(
                "SELECT event_type, COUNT(*) AS total FROM audio_events "
                "WHERE media_id = ? GROUP BY event_type",
                (media_id,),
            )
        }

    def delete_for_media(self, media_id: str) -> int:
        return self._db.execute(
            "DELETE FROM audio_events WHERE media_id = ?", (media_id,)
        ).rowcount


def _from_row(row: sqlite3.Row) -> AudioEvent:
    metadata = loads(row["metadata"])
    return AudioEvent(
        event_type=AudioEventType(row["event_type"]),
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        track_role=row["track_role"],
        confidence=row["confidence"],
        rms_db=row["rms_db"],
        peak_db=row["peak_db"],
        lufs=row["lufs"],
        metadata=metadata if isinstance(metadata, dict) else {},
    )


__all__ = ["AudioEventRepository"]
