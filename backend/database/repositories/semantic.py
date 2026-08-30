"""Storage for the Semantic Timeline (``semantic_timelines``).

One row per recording. The lanes ride as JSON because they are read whole and
never queried by value -- a table of thirty thousand floats would buy nothing
and cost every read a join.
"""

from __future__ import annotations

from datetime import datetime, timezone
from json import dumps, loads
from typing import Any

from backend.database.connection import Database

_COLUMNS = "media_id, signature, builder_version, hz, duration_seconds, lanes, built_at"


class SemanticRepository:
    """CRUD for the stored session lanes."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, media_id: str, *, signature: str) -> dict[str, Any] | None:
        """The stored timeline, or ``None`` when it is absent or stale.

        A signature mismatch returns ``None`` rather than the old row: the
        caller's job is to rebuild, and handing back lanes built from evidence
        that has since changed is the exact failure the file cache had.
        """
        row = self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM semantic_timelines WHERE media_id = ?",
            (media_id,),
        )
        if row is None or row["signature"] != signature:
            return None
        return {
            "media_id": row["media_id"],
            "hz": int(row["hz"]),
            "duration_seconds": float(row["duration_seconds"]),
            "lanes": loads(row["lanes"]),
        }

    def save(
        self,
        media_id: str,
        *,
        signature: str,
        builder_version: str,
        hz: int,
        duration_seconds: float,
        lanes: dict[str, list[float]],
    ) -> None:
        """Store the lanes, replacing whatever was there for this recording."""
        self._db.execute(
            f"INSERT OR REPLACE INTO semantic_timelines ({_COLUMNS}) "
            "VALUES (:media_id, :signature, :builder_version, :hz, "
            ":duration_seconds, :lanes, :built_at)",
            {
                "media_id": media_id,
                "signature": signature,
                "builder_version": builder_version,
                "hz": int(hz),
                "duration_seconds": float(duration_seconds),
                # Four decimals: the lanes are 0..1 and every consumer either
                # grades them into five levels or averages them. Storing full
                # doubles would triple the row for no decision's sake.
                "lanes": dumps(
                    {name: [round(value, 4) for value in lane] for name, lane in lanes.items()}
                ),
                "built_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def delete_for_media(self, media_id: str) -> int:
        cursor = self._db.execute(
            "DELETE FROM semantic_timelines WHERE media_id = ?", (media_id,)
        )
        return cursor.rowcount


__all__ = ["SemanticRepository"]
