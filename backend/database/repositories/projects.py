"""Project persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from backend.core.errors import ErrorCode, NotFoundError
from backend.core.models.enums import ProjectStatus, VideoMode
from backend.core.models.project import Project
from backend.database.connection import Database

_COLUMNS = (
    "id, name, created_at, updated_at, status, target_duration_seconds, mode, game, "
    "detected_game, resolution, fps, aspect_ratio, language, project_directory, version, "
    "application_version, analysis_version, schema_version"
)


class ProjectRepository:
    """CRUD for the ``projects`` table.

    Owns SQL and row/model conversion only. Directory creation, manifest
    writing and validation belong to the project manager.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def create(self, project: Project) -> Project:
        """Insert a new project row."""
        self._db.execute(
            f"INSERT INTO projects ({_COLUMNS}) VALUES ("
            ":id, :name, :created_at, :updated_at, :status, :target_duration_seconds, "
            ":mode, :game, :detected_game, :resolution, :fps, :aspect_ratio, :language, "
            ":project_directory, :version, :application_version, :analysis_version, "
            ":schema_version)",
            _to_row(project),
        )
        return project

    def get(self, project_id: str) -> Project | None:
        """Return a project, or ``None`` when it does not exist."""
        row = self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM projects WHERE id = ?", (project_id,)
        )
        return _from_row(row) if row is not None else None

    def require(self, project_id: str) -> Project:
        """Return a project or raise :class:`NotFoundError`."""
        project = self.get(project_id)
        if project is None:
            raise NotFoundError(
                f"Project {project_id!r} does not exist.",
                code=ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": project_id},
                recoverable=False,
            )
        return project

    def list(
        self,
        *,
        status: ProjectStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Project]:
        """Return projects, most recently updated first (dashboard order, §58)."""
        sql = f"SELECT {_COLUMNS} FROM projects"
        parameters: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            parameters.append(status.value)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        parameters.extend([limit, offset])
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def count(self, *, status: ProjectStatus | None = None) -> int:
        if status is None:
            row = self._db.fetch_one("SELECT COUNT(*) AS total FROM projects")
        else:
            row = self._db.fetch_one(
                "SELECT COUNT(*) AS total FROM projects WHERE status = ?", (status.value,)
            )
        return int(row["total"]) if row is not None else 0

    def update(self, project: Project) -> Project:
        """Persist a modified project, refreshing ``updated_at``."""
        updated = project.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        cursor = self._db.execute(
            "UPDATE projects SET name = :name, updated_at = :updated_at, status = :status, "
            "target_duration_seconds = :target_duration_seconds, mode = :mode, game = :game, "
            "detected_game = :detected_game, resolution = :resolution, fps = :fps, "
            "aspect_ratio = :aspect_ratio, language = :language, "
            "project_directory = :project_directory, version = :version, "
            "application_version = :application_version, analysis_version = :analysis_version, "
            "schema_version = :schema_version WHERE id = :id",
            _to_row(updated),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(
                f"Project {project.id!r} does not exist.",
                code=ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": project.id},
                recoverable=False,
            )
        return updated

    def delete(self, project_id: str) -> bool:
        """Delete a project and everything cascading from it.

        Returns whether a row was removed. Files on disk are the project
        manager's responsibility -- the repository never touches the filesystem.
        """
        cursor = self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0

    def exists(self, project_id: str) -> bool:
        return self._db.fetch_one("SELECT 1 FROM projects WHERE id = ?", (project_id,)) is not None


def _to_row(project: Project) -> dict[str, object]:
    return {
        "id": project.id,
        "name": project.name,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "status": project.status.value,
        "target_duration_seconds": project.target_duration_seconds,
        "mode": project.mode.value,
        "game": project.game,
        "detected_game": project.detected_game,
        "resolution": project.resolution,
        "fps": project.fps,
        "aspect_ratio": project.aspect_ratio,
        "language": project.language,
        "project_directory": project.project_directory,
        "version": project.version,
        "application_version": project.application_version,
        "analysis_version": project.analysis_version,
        "schema_version": project.schema_version,
    }


def _from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        status=ProjectStatus(row["status"]),
        target_duration_seconds=row["target_duration_seconds"],
        mode=VideoMode(row["mode"]),
        game=row["game"],
        detected_game=row["detected_game"],
        resolution=row["resolution"],
        fps=row["fps"],
        aspect_ratio=row["aspect_ratio"],
        language=row["language"],
        project_directory=row["project_directory"],
        version=row["version"],
        application_version=row["application_version"],
        analysis_version=row["analysis_version"],
        schema_version=row["schema_version"],
    )


__all__ = ["ProjectRepository"]
