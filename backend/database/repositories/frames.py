"""Sampled frame persistence (SPEC sections 16, 15, 45).

The ``frames`` table is the worklist the vision cascade draws from. §15 forbids
sending every frame to the model, so the rows carry two things beyond a path:
the ``sampling_level`` that says why the frame was taken, and the ``analyzed``
flag that says whether a model has already seen it.

That flag is what makes the expensive stage resumable. Vision analysis of a
two-hour recording is measured in minutes to hours; a crash partway through
must resume at the first unanalysed frame, not at the first frame (§47).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence

from backend.core.ids import new_id
from backend.database.connection import Database
from backend.media.frames import ExtractedFrame

_COLUMNS = (
    "id, project_id, media_id, timestamp, sampling_level, image_path, width, height, "
    "motion_score, analyzed"
)


class FrameRow:
    """One persisted frame.

    A small class rather than a Pydantic model: a two-hour recording produces
    thousands of these and they are read in bulk by the vision stage, so they
    stay cheap to build.
    """

    __slots__ = (
        "analyzed",
        "height",
        "id",
        "image_path",
        "media_id",
        "motion_score",
        "project_id",
        "sampling_level",
        "timestamp",
        "width",
    )

    def __init__(
        self,
        *,
        id: str,
        project_id: str,
        media_id: str,
        timestamp: float,
        sampling_level: str,
        image_path: str,
        width: int | None = None,
        height: int | None = None,
        motion_score: float | None = None,
        analyzed: bool = False,
    ) -> None:
        self.id = id
        self.project_id = project_id
        self.media_id = media_id
        self.timestamp = timestamp
        self.sampling_level = sampling_level
        self.image_path = image_path
        self.width = width
        self.height = height
        self.motion_score = motion_score
        self.analyzed = analyzed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FrameRow(t={self.timestamp:.3f}, level={self.sampling_level!r})"


class FrameRepository:
    """CRUD for the ``frames`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def add_many(
        self,
        project_id: str,
        media_id: str,
        frames: Iterable[ExtractedFrame],
    ) -> int:
        """Record extracted frames, ignoring ones already present.

        ``INSERT OR IGNORE`` against the ``(media_id, timestamp, sampling_level)``
        unique constraint: re-running FRAMES after an interruption must not
        duplicate rows for frames that survived on disk (§47).
        """
        rows = [
            {
                "id": new_id("frame"),
                "project_id": project_id,
                "media_id": media_id,
                "timestamp": frame.timestamp,
                "sampling_level": frame.level,
                "image_path": str(frame.path),
                "width": None,
                "height": None,
                "motion_score": None,
                "analyzed": 0,
            }
            for frame in frames
        ]
        if not rows:
            return 0
        self._db.executemany(
            f"INSERT OR IGNORE INTO frames ({_COLUMNS}) VALUES ("
            ":id, :project_id, :media_id, :timestamp, :sampling_level, :image_path, "
            ":width, :height, :motion_score, :analyzed)",
            rows,
        )
        return len(rows)

    def list_for_media(
        self,
        media_id: str,
        *,
        level: str | None = None,
        analyzed: bool | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
    ) -> list[FrameRow]:
        """Frames of one file in chronological order, optionally filtered."""
        sql = f"SELECT {_COLUMNS} FROM frames WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if level is not None:
            sql += " AND sampling_level = ?"
            parameters.append(level)
        if analyzed is not None:
            sql += " AND analyzed = ?"
            parameters.append(int(analyzed))
        if start is not None:
            sql += " AND timestamp >= ?"
            parameters.append(start)
        if end is not None:
            sql += " AND timestamp <= ?"
            parameters.append(end)
        sql += " ORDER BY timestamp ASC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def count_for_media(self, media_id: str, *, level: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS total FROM frames WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if level is not None:
            sql += " AND sampling_level = ?"
            parameters.append(level)
        row = self._db.fetch_one(sql, parameters)
        return int(row["total"]) if row is not None else 0

    def mark_analyzed(self, frame_ids: Sequence[str]) -> int:
        """Flag frames a model has already looked at (§47 resume)."""
        if not frame_ids:
            return 0
        self._db.executemany(
            "UPDATE frames SET analyzed = 1 WHERE id = ?",
            [(frame_id,) for frame_id in frame_ids],
        )
        return len(frame_ids)

    def reset_analyzed(self, media_id: str, *, level: str | None = None) -> int:
        """Forget that a pass looked at these frames.

        The OCR stage calls this when it replaces a recording's reads: the
        planned-frame pass (V2-P0.4) marks the base frames it read, and reads
        that were just deleted must not leave their frames marked as done.
        """
        sql = "UPDATE frames SET analyzed = 0 WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if level is not None:
            sql += " AND sampling_level = ?"
            parameters.append(level)
        cursor = self._db.execute(sql, parameters)
        return int(getattr(cursor, "rowcount", 0) or 0)

    def delete_for_media(self, media_id: str, *, level: str | None = None) -> int:
        """Drop frame rows, so a re-run does not read the previous pass's list."""
        sql = "DELETE FROM frames WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if level is not None:
            sql += " AND sampling_level = ?"
            parameters.append(level)
        return self._db.execute(sql, parameters).rowcount


def _from_row(row: sqlite3.Row) -> FrameRow:
    return FrameRow(
        id=row["id"],
        project_id=row["project_id"],
        media_id=row["media_id"],
        timestamp=row["timestamp"],
        sampling_level=row["sampling_level"],
        image_path=row["image_path"],
        width=row["width"],
        height=row["height"],
        motion_score=row["motion_score"],
        analyzed=bool(row["analyzed"]),
    )


__all__ = ["FrameRepository", "FrameRow"]
