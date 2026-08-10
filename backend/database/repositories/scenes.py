"""Scene persistence (SPEC sections 17, 45, 56).

Shot boundaries and their keyframes. §17's constraint travels with the data:
these are **supporting information, not edit points**, and nothing that reads
this table should treat a boundary as a place to cut.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from backend.analysis.scenes import Scene
from backend.core.ids import new_id
from backend.database.connection import Database

_COLUMNS = (
    "id, project_id, media_id, scene_index, start_seconds, end_seconds, "
    "change_score, keyframe_path"
)


class SceneRepository:
    """CRUD for the ``scenes`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_media(
        self,
        project_id: str,
        media_id: str,
        scenes: Iterable[Scene],
        *,
        keyframes: dict[int, str] | None = None,
    ) -> int:
        """Replace a file's scene list.

        Args:
            keyframes: scene index to keyframe path, when the stage extracted
                previews (§56).
        """
        paths = keyframes or {}
        rows = [
            {
                "id": new_id("scene"),
                "project_id": project_id,
                "media_id": media_id,
                "scene_index": scene.index,
                "start_seconds": max(scene.start_seconds, 0.0),
                "end_seconds": max(scene.end_seconds, scene.start_seconds + 1e-6),
                "change_score": scene.change_score,
                "keyframe_path": paths.get(scene.index),
            }
            for scene in scenes
        ]
        self._db.execute("DELETE FROM scenes WHERE media_id = ?", (media_id,))
        if rows:
            self._db.executemany(
                f"INSERT INTO scenes ({_COLUMNS}) VALUES ("
                ":id, :project_id, :media_id, :scene_index, :start_seconds, :end_seconds, "
                ":change_score, :keyframe_path)",
                rows,
            )
        return len(rows)

    def list_for_media(
        self, media_id: str, *, start: float | None = None, end: float | None = None
    ) -> list[Scene]:
        sql = f"SELECT {_COLUMNS} FROM scenes WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if start is not None:
            sql += " AND end_seconds >= ?"
            parameters.append(start)
        if end is not None:
            sql += " AND start_seconds <= ?"
            parameters.append(end)
        sql += " ORDER BY scene_index ASC"
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def count_for_media(self, media_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM scenes WHERE media_id = ?", (media_id,)
        )
        return int(row["total"]) if row is not None else 0

    def keyframe_paths(self, media_id: str) -> dict[int, str]:
        """Scene index to preview image, for the §56 review screens."""
        return {
            int(row["scene_index"]): str(row["keyframe_path"])
            for row in self._db.fetch_all(
                "SELECT scene_index, keyframe_path FROM scenes "
                "WHERE media_id = ? AND keyframe_path IS NOT NULL",
                (media_id,),
            )
        }

    def delete_for_media(self, media_id: str) -> int:
        return self._db.execute("DELETE FROM scenes WHERE media_id = ?", (media_id,)).rowcount


def _from_row(row: sqlite3.Row) -> Scene:
    return Scene(
        index=row["scene_index"],
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        change_score=row["change_score"],
    )


__all__ = ["SceneRepository"]
