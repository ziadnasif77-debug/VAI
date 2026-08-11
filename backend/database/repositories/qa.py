"""QA result persistence (SPEC §45, §76, §80).

Every check's outcome is stored, passes included. A table that held only
problems could not answer "was the audio checked?" — and the difference between
*checked and fine* and *never looked at* is the whole value of a QA record when
someone is deciding whether to publish.

Results are keyed to a render, not to a project: the question is always about a
specific file, and re-rendering produces a new one to ask about.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from backend.core.ids import new_id
from backend.core.models.enums import QAStatus
from backend.database.connection import Database, dumps, loads
from backend.qa.report import Finding

_COLUMNS = (
    "id, project_id, render_id, category, check_name, status, detail, measured, created_at"
)


class QaRepository:
    """CRUD for the ``qa_results`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_render(
        self, project_id: str, render_id: str | None, findings: Iterable[Finding]
    ) -> int:
        """Store one QA pass, replacing any earlier pass over the same render."""
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for finding in findings:
            row = finding.as_row()
            rows.append(
                {
                    "id": new_id("qa_result"),
                    "project_id": project_id,
                    "render_id": render_id,
                    "category": row["category"],
                    "check_name": row["check_name"],
                    "status": row["status"],
                    "detail": row["detail"],
                    "measured": dumps(row["measured"]),
                    "created_at": now,
                }
            )

        with self._db.transaction():
            if render_id is not None:
                self._db.execute(
                    "DELETE FROM qa_results WHERE render_id = ?", (render_id,)
                )
            else:
                self._db.execute(
                    "DELETE FROM qa_results WHERE project_id = ? AND render_id IS NULL",
                    (project_id,),
                )
            if rows:
                self._db.executemany(
                    f"INSERT INTO qa_results ({_COLUMNS}) VALUES ("
                    ":id, :project_id, :render_id, :category, :check_name, :status, "
                    ":detail, :measured, :created_at)",
                    rows,
                )
        return len(rows)

    def list_for_render(self, render_id: str) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM qa_results WHERE render_id = ? ORDER BY created_at, id",
            (render_id,),
        )
        return [_as_dict(row) for row in rows]

    def list_for_project(self, project_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM qa_results WHERE project_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        return [_as_dict(row) for row in rows]

    def failures_for_render(self, render_id: str) -> list[dict[str, Any]]:
        """Only the checks that block an export (§76)."""
        rows = self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM qa_results WHERE render_id = ? AND status = ?",
            (render_id, QAStatus.FAILED.value),
        )
        return [_as_dict(row) for row in rows]

    def count_for_render(self, render_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM qa_results WHERE render_id = ?", (render_id,)
        )
        return int(row["total"]) if row is not None else 0


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["measured"] = loads(record.get("measured") or "{}")
    return record


__all__ = ["QaRepository"]
