"""OCR and game-event persistence (SPEC sections 25, 26, 45, 49).

Two tables, one module: they are written by the same two stages and read
together by everything downstream, and splitting them would mean two files that
always change at once.

Every row records the game profile in force. "Detected with the generic
profile" and "detected with the Valorant profile" are different claims about
the same instant (§23, §49), and a schema that lost the difference would make a
profile's contribution unmeasurable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone

from ai.providers.base import TextDetection
from backend.core.ids import new_id
from backend.core.models.enums import GameEventType
from backend.core.versions import ANALYSIS_VERSION
from backend.database.connection import Database, dumps, loads
from backend.gaming.correlation import GameEvent

_OCR_COLUMNS = (
    "id, project_id, media_id, timestamp, text, confidence, region, box, "
    "game_profile, engine, created_at"
)

_EVENT_COLUMNS = (
    "id, project_id, media_id, event_type, start_seconds, end_seconds, confidence, "
    "importance, sources, game_profile, metadata, model_name, model_version, "
    "prompt_version, analysis_version, created_at"
)


class OcrRepository:
    """CRUD for the ``ocr_results`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_media(
        self,
        project_id: str,
        media_id: str,
        detections: Iterable[TextDetection],
        *,
        game_profile: str | None = None,
        engine: str | None = None,
    ) -> int:
        rows = self._rows(project_id, media_id, detections, game_profile, engine)
        self._db.execute("DELETE FROM ocr_results WHERE media_id = ?", (media_id,))
        return self._insert(rows)

    def add_for_media(
        self,
        project_id: str,
        media_id: str,
        detections: Iterable[TextDetection],
        *,
        game_profile: str | None = None,
        engine: str | None = None,
    ) -> int:
        """Append reads without touching the stored ones (V2-P0.4).

        The OCR stage replaces; the planned-frame pass adds. The two must not
        share a verb: the pass runs after every stage that consumed the OCR
        stage's reads, and a replace here would silently rewrite the evidence
        those stages were built on.
        """
        return self._insert(self._rows(project_id, media_id, detections, game_profile, engine))

    def _insert(self, rows: list[dict[str, object]]) -> int:
        if rows:
            self._db.executemany(
                f"INSERT INTO ocr_results ({_OCR_COLUMNS}) VALUES ("
                ":id, :project_id, :media_id, :timestamp, :text, :confidence, :region, "
                ":box, :game_profile, :engine, :created_at)",
                rows,
            )
        return len(rows)

    @staticmethod
    def _rows(
        project_id: str,
        media_id: str,
        detections: Iterable[TextDetection],
        game_profile: str | None,
        engine: str | None,
    ) -> list[dict[str, object]]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            {
                "id": new_id("ocr_result"),
                "project_id": project_id,
                "media_id": media_id,
                "timestamp": max(detection.timestamp, 0.0),
                "text": detection.text,
                "confidence": min(max(detection.confidence, 0.0), 1.0),
                "region": detection.region,
                "box": dumps(list(detection.box)) if detection.box else None,
                "game_profile": game_profile,
                "engine": engine,
                "created_at": now,
            }
            for detection in detections
        ]

    def list_for_media(
        self,
        media_id: str,
        *,
        region: str | None = None,
        start: float | None = None,
        end: float | None = None,
        min_confidence: float | None = None,
    ) -> list[TextDetection]:
        sql = f"SELECT {_OCR_COLUMNS} FROM ocr_results WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if region is not None:
            sql += " AND region = ?"
            parameters.append(region)
        if start is not None:
            sql += " AND timestamp >= ?"
            parameters.append(start)
        if end is not None:
            sql += " AND timestamp <= ?"
            parameters.append(end)
        if min_confidence is not None:
            sql += " AND confidence >= ?"
            parameters.append(min_confidence)
        sql += " ORDER BY timestamp ASC"
        return [_detection_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def count_for_media(self, media_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM ocr_results WHERE media_id = ?", (media_id,)
        )
        return int(row["total"]) if row is not None else 0

    def search(self, project_id: str, term: str, *, limit: int = 50) -> list[TextDetection]:
        """Find on-screen text, for the §17-addendum Q&A path."""
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return [
            _detection_from_row(row)
            for row in self._db.fetch_all(
                f"SELECT {_OCR_COLUMNS} FROM ocr_results WHERE project_id = ? "
                "AND text LIKE ? ESCAPE '\\' ORDER BY timestamp ASC LIMIT ?",
                (project_id, f"%{escaped}%", limit),
            )
        ]

    def delete_for_media(self, media_id: str) -> int:
        return self._db.execute(
            "DELETE FROM ocr_results WHERE media_id = ?", (media_id,)
        ).rowcount


class GameEventRepository:
    """CRUD for the ``game_events`` table (§26)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_media(
        self,
        project_id: str,
        media_id: str,
        events: Iterable[GameEvent],
        *,
        model_name: str | None = None,
        model_version: str | None = None,
        prompt_version: int | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "id": new_id("game_event"),
                "project_id": project_id,
                "media_id": media_id,
                "event_type": event.event_type.value,
                "start_seconds": max(event.start_seconds, 0.0),
                "end_seconds": max(event.end_seconds, event.start_seconds),
                "confidence": min(max(event.confidence, 0.0), 1.0),
                "importance": min(max(event.importance, 0.0), 1.0),
                "sources": dumps(list(event.sources)),
                "game_profile": event.game_profile,
                "metadata": dumps(event.metadata),
                "model_name": model_name,
                "model_version": model_version,
                "prompt_version": prompt_version,
                "analysis_version": ANALYSIS_VERSION,
                "created_at": now,
            }
            for event in events
        ]
        self._db.execute("DELETE FROM game_events WHERE media_id = ?", (media_id,))
        if rows:
            self._db.executemany(
                f"INSERT INTO game_events ({_EVENT_COLUMNS}) VALUES ("
                ":id, :project_id, :media_id, :event_type, :start_seconds, :end_seconds, "
                ":confidence, :importance, :sources, :game_profile, :metadata, :model_name, "
                ":model_version, :prompt_version, :analysis_version, :created_at)",
                rows,
            )
        return len(rows)

    def list_for_media(
        self,
        media_id: str,
        *,
        event_type: GameEventType | None = None,
        min_confidence: float | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> list[GameEvent]:
        sql = f"SELECT {_EVENT_COLUMNS} FROM game_events WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if event_type is not None:
            sql += " AND event_type = ?"
            parameters.append(event_type.value)
        if min_confidence is not None:
            sql += " AND confidence >= ?"
            parameters.append(min_confidence)
        if start is not None:
            sql += " AND end_seconds >= ?"
            parameters.append(start)
        if end is not None:
            sql += " AND start_seconds <= ?"
            parameters.append(end)
        sql += " ORDER BY start_seconds ASC"
        return [_event_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def list_for_project(self, project_id: str) -> list[GameEvent]:
        return [
            _event_from_row(row)
            for row in self._db.fetch_all(
                f"SELECT {_EVENT_COLUMNS} FROM game_events WHERE project_id = ? "
                "ORDER BY start_seconds ASC",
                (project_id,),
            )
        ]

    def counts_by_type(self, media_id: str) -> dict[str, int]:
        return {
            str(row["event_type"]): int(row["total"])
            for row in self._db.fetch_all(
                "SELECT event_type, COUNT(*) AS total FROM game_events "
                "WHERE media_id = ? GROUP BY event_type",
                (media_id,),
            )
        }

    def count_for_media(self, media_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM game_events WHERE media_id = ?", (media_id,)
        )
        return int(row["total"]) if row is not None else 0

    def delete_for_media(self, media_id: str) -> int:
        return self._db.execute(
            "DELETE FROM game_events WHERE media_id = ?", (media_id,)
        ).rowcount


def _detection_from_row(row: sqlite3.Row) -> TextDetection:
    box = loads(row["box"])
    return TextDetection(
        text=row["text"],
        confidence=row["confidence"],
        timestamp=row["timestamp"],
        region=row["region"],
        box=tuple(box) if isinstance(box, list) and len(box) == 4 else None,
    )


def _event_from_row(row: sqlite3.Row) -> GameEvent:
    metadata = loads(row["metadata"])
    return GameEvent(
        event_type=GameEventType(row["event_type"]),
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        confidence=row["confidence"],
        importance=row["importance"],
        sources=tuple(loads(row["sources"]) or []),
        metadata=metadata if isinstance(metadata, dict) else {},
        game_profile=row["game_profile"],
    )


__all__ = ["GameEventRepository", "OcrRepository"]
