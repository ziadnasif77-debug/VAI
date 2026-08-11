"""Render persistence (SPEC §45, §80).

A render is a fact about a moment in time: this edit, at this version, produced
this file with this encoder. Keeping the row means "which encoder made the
video that looked wrong" is answerable months later, and it is the record §80's
explainability rests on for the output as much as for the moments.

Rows accumulate rather than being replaced. Re-rendering after an edit is
normal, and a project with five renders has five files someone may still have
open.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from backend.core.ids import new_id
from backend.database.connection import Database

_COLUMNS = (
    "id, project_id, status, engine, output_path, resolution, fps, video_codec, "
    "audio_codec, encoder, duration_seconds, size_bytes, started_at, completed_at, "
    "render_seconds, error_code, error_message, project_version, created_at"
)


class RenderRepository:
    """CRUD for the ``renders`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def start(
        self,
        project_id: str,
        *,
        resolution: int,
        fps: int,
        encoder: str,
        engine: str = "ffmpeg",
        project_version: int = 1,
    ) -> str:
        """Record that a render began, and return its id.

        Written before the encode rather than after it, so a render that
        crashes leaves evidence that it was attempted. A row that only appears
        on success cannot answer "why is there no video?".
        """
        now = datetime.now(timezone.utc).isoformat()
        render_id = new_id("render")
        self._db.execute(
            f"INSERT INTO renders ({_COLUMNS}) VALUES ("
            ":id, :project_id, :status, :engine, :output_path, :resolution, :fps, "
            ":video_codec, :audio_codec, :encoder, :duration_seconds, :size_bytes, "
            ":started_at, :completed_at, :render_seconds, :error_code, :error_message, "
            ":project_version, :created_at)",
            {
                "id": render_id,
                "project_id": project_id,
                "status": "running",
                "engine": engine,
                "output_path": None,
                "resolution": resolution,
                "fps": fps,
                "video_codec": None,
                "audio_codec": None,
                "encoder": encoder,
                "duration_seconds": None,
                "size_bytes": None,
                "started_at": now,
                "completed_at": None,
                "render_seconds": None,
                "error_code": None,
                "error_message": None,
                "project_version": project_version,
                "created_at": now,
            },
        )
        return render_id

    def complete(
        self,
        render_id: str,
        *,
        output_path: str,
        duration_seconds: float,
        size_bytes: int,
        video_codec: str,
        audio_codec: str,
        render_seconds: float,
    ) -> None:
        self._db.execute(
            "UPDATE renders SET status = 'completed', output_path = ?, "
            "duration_seconds = ?, size_bytes = ?, video_codec = ?, audio_codec = ?, "
            "render_seconds = ?, completed_at = ? WHERE id = ?",
            (
                output_path,
                duration_seconds,
                size_bytes,
                video_codec,
                audio_codec,
                render_seconds,
                datetime.now(timezone.utc).isoformat(),
                render_id,
            ),
        )

    def fail(self, render_id: str, *, error_code: str, error_message: str) -> None:
        self._db.execute(
            "UPDATE renders SET status = 'failed', error_code = ?, error_message = ?, "
            "completed_at = ? WHERE id = ?",
            (
                error_code,
                error_message[:2000],
                datetime.now(timezone.utc).isoformat(),
                render_id,
            ),
        )

    def latest(self, project_id: str) -> dict[str, Any] | None:
        row = self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM renders WHERE project_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        return _as_dict(row) if row is not None else None

    def list_for_project(self, project_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM renders WHERE project_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        return [_as_dict(row) for row in rows]

    def count_for_project(self, project_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM renders WHERE project_id = ?", (project_id,)
        )
        return int(row["total"]) if row is not None else 0


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


__all__ = ["RenderRepository"]
