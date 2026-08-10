"""Project lifecycle (SPEC sections 42, 43, 122, 127).

Owns the three things the repository deliberately does not: validation against
the duration policy, the §43 directory tree, and the ``project.json`` manifest.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

from backend.config.paths import Paths, ProjectPaths
from backend.config.schema import AppConfig
from backend.core.duration import TargetDurationError
from backend.core.errors import ErrorCode, StorageError, ValidationError
from backend.core.fs import atomic_write_json, read_json
from backend.core.ids import new_id
from backend.core.logging import LogChannel, get_logger, log_context
from backend.core.models.enums import ProjectStatus
from backend.core.models.jobs import EDIT_STAGES, JobStage, downstream_of
from backend.core.models.project import (
    Project,
    ProjectCreate,
    ProjectManifest,
    ProjectUpdate,
)
from backend.core.versions import (
    ANALYSIS_VERSION,
    APPLICATION_VERSION,
    PROMPT_VERSIONS,
    SCHEMA_VERSION,
)
from backend.database.connection import Database
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.projects import ProjectRepository

logger = get_logger("services.project_manager", LogChannel.APPLICATION)

#: Fields whose change invalidates the edit stages but never the analysis.
#: §127: after any edit the video must be regenerated without re-analysing the
#: source.
_EDIT_INVALIDATING_FIELDS = frozenset(
    {"target_duration_seconds", "mode", "resolution", "fps"}
)


class ProjectManager:
    """Creates, reads, updates and deletes projects."""

    def __init__(self, database: Database, paths: Paths, config: AppConfig) -> None:
        self._db = database
        self._paths = paths
        self._config = config
        self._projects = ProjectRepository(database)
        self._jobs = JobRepository(database)

    # -- creation -------------------------------------------------------

    def create(self, request: ProjectCreate) -> Project:
        """Create a project: validate, insert, scaffold §43, write the manifest."""
        policy = self._config.duration_policy
        try:
            target_seconds = policy.validate(request.target_duration_seconds)
        except TargetDurationError as exc:
            raise ValidationError(
                str(exc),
                code=ErrorCode.INVALID_TARGET_DURATION,
                details={
                    "requested_seconds": request.target_duration_seconds,
                    "min_seconds": policy.min_seconds,
                    "max_seconds": policy.max_seconds,
                },
                recoverable=False,
                cause=exc,
            ) from exc

        self._validate_output_format(request.resolution, request.fps)
        if request.mode not in self._config.output.modes:
            raise ValidationError(
                f"Video mode {request.mode.value!r} is not enabled.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={
                    "mode": request.mode.value,
                    "enabled": [mode.value for mode in self._config.output.modes],
                },
                recoverable=False,
            )

        project_id = new_id("project")
        project_paths = self._paths.project(project_id)
        now = datetime.now(timezone.utc)

        project = Project(
            id=project_id,
            name=request.name,
            created_at=now,
            updated_at=now,
            status=ProjectStatus.CREATED,
            target_duration_seconds=target_seconds,
            mode=request.mode,
            game=request.game,
            resolution=request.resolution,
            fps=request.fps,
            aspect_ratio=self._config.video.aspect_ratio,
            language=request.language,
            project_directory=str(project_paths.root),
            application_version=APPLICATION_VERSION,
            analysis_version=ANALYSIS_VERSION,
            schema_version=SCHEMA_VERSION,
        )

        with log_context(project_id=project_id):
            # The row goes in first: a directory without a row is invisible
            # garbage, while a row without directories is repaired on open.
            with self._db.transaction():
                self._projects.create(project)
            try:
                project_paths.create()
                self.write_manifest(project)
            except OSError as exc:
                with self._db.transaction():
                    self._projects.delete(project_id)
                raise StorageError(
                    f"Cannot create project directory {project_paths.root}: {exc}",
                    code=ErrorCode.STORAGE_ERROR,
                    details={"project_directory": str(project_paths.root)},
                    cause=exc,
                ) from exc

            logger.info(
                "Project created",
                extra={
                    "project_name": project.name,
                    "target_duration_seconds": target_seconds,
                    "mode": project.mode.value,
                    "game": project.game,
                },
            )
        return project

    # -- reads ----------------------------------------------------------

    def get(self, project_id: str) -> Project:
        """Return a project or raise ``PROJECT_NOT_FOUND``."""
        return self._projects.require(project_id)

    def list(
        self,
        *,
        status: ProjectStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Project]:
        return self._projects.list(status=status, limit=limit, offset=offset)

    def count(self, *, status: ProjectStatus | None = None) -> int:
        return self._projects.count(status=status)

    @property
    def paths(self) -> Paths:
        """The application path layout this manager writes into."""
        return self._paths

    def paths_for(self, project: Project | str) -> ProjectPaths:
        """Return the §43 layout for a project."""
        project_id = project if isinstance(project, str) else project.id
        return self._paths.project(project_id)

    # -- updates --------------------------------------------------------

    def update(self, project_id: str, changes: ProjectUpdate) -> Project:
        """Apply changes, invalidating only what genuinely became stale.

        Changing the target duration or mode invalidates the edit stages and
        leaves every analysis artefact intact (§127).
        """
        project = self._projects.require(project_id)
        changed = changes.changed_fields()
        if not changed:
            return project

        if "target_duration_seconds" in changed:
            policy = self._config.duration_policy
            try:
                changed["target_duration_seconds"] = policy.validate(
                    changed["target_duration_seconds"]
                )
            except TargetDurationError as exc:
                raise ValidationError(
                    str(exc),
                    code=ErrorCode.INVALID_TARGET_DURATION,
                    details={
                        "requested_seconds": changed["target_duration_seconds"],
                        "min_seconds": policy.min_seconds,
                        "max_seconds": policy.max_seconds,
                    },
                    recoverable=False,
                    cause=exc,
                ) from exc

        if "resolution" in changed or "fps" in changed:
            self._validate_output_format(
                changed.get("resolution", project.resolution),
                changed.get("fps", project.fps),
            )

        invalidates_edit = bool(_EDIT_INVALIDATING_FIELDS & set(changed))
        if invalidates_edit:
            changed["version"] = project.version + 1

        updated = project.model_copy(update=changed)

        with log_context(project_id=project_id), self._db.transaction():
            updated = self._projects.update(updated)
            if invalidates_edit:
                removed = self._jobs.delete_for_stages(project_id, set(EDIT_STAGES))
                logger.info(
                    "Edit stages invalidated; analysis preserved",
                    extra={
                        "changed_fields": sorted(changed),
                        "jobs_removed": removed,
                        "project_version": updated.version,
                    },
                )

        self.write_manifest(updated)
        return updated

    def set_status(self, project_id: str, status: ProjectStatus) -> Project:
        """Move a project to a new lifecycle state."""
        project = self._projects.require(project_id)
        if project.status is status:
            return project
        with self._db.transaction():
            updated = self._projects.update(project.model_copy(update={"status": status}))
        logger.info(
            "Project status changed",
            extra={
                "project_id": project_id,
                "from_status": project.status.value,
                "to_status": status.value,
            },
        )
        self.write_manifest(updated)
        return updated

    def invalidate_from(self, project_id: str, stage: JobStage) -> set[JobStage]:
        """Drop the jobs of ``stage`` and everything downstream of it.

        Used by re-analysis (§90): re-running vision must also invalidate game
        events, moments and the whole edit chain, but not the transcript.
        """
        affected = {stage, *downstream_of(stage)}
        with self._db.transaction():
            removed = self._jobs.delete_for_stages(project_id, affected)
        logger.info(
            "Stages invalidated",
            extra={
                "project_id": project_id,
                "from_stage": stage.value,
                "stages": sorted(item.value for item in affected),
                "jobs_removed": removed,
            },
        )
        return affected

    # -- deletion -------------------------------------------------------

    def delete(self, project_id: str, *, delete_files: bool = False) -> None:
        """Delete a project row, and optionally its directory.

        ``delete_files`` defaults to False: a project directory holds imported
        recordings, and removing footage is not something to do implicitly.
        """
        project = self._projects.require(project_id)
        project_paths = self._paths.project(project_id)

        with self._db.transaction():
            self._projects.delete(project_id)

        if delete_files and project_paths.root.exists():
            try:
                shutil.rmtree(project_paths.root)
            except OSError as exc:
                raise StorageError(
                    f"Project row deleted but its directory could not be removed: {exc}",
                    code=ErrorCode.STORAGE_ERROR,
                    details={"project_directory": str(project_paths.root)},
                    cause=exc,
                ) from exc

        logger.info(
            "Project deleted",
            extra={
                "project_id": project_id,
                "project_name": project.name,
                "files_deleted": delete_files,
            },
        )

    # -- manifest -------------------------------------------------------

    def write_manifest(self, project: Project) -> None:
        """Write ``project.json`` (§43) with full version provenance (§49)."""
        manifest = ProjectManifest.for_project(project, dict(PROMPT_VERSIONS))
        target = self._paths.project(project.id).manifest
        try:
            atomic_write_json(target, manifest.model_dump(mode="json"))
        except OSError as exc:
            raise StorageError(
                f"Cannot write project manifest {target}: {exc}",
                code=ErrorCode.STORAGE_ERROR,
                details={"manifest": str(target)},
                cause=exc,
            ) from exc

    def read_manifest(self, project_id: str) -> ProjectManifest:
        """Read ``project.json`` from disk."""
        target = self._paths.project(project_id).manifest
        if not target.is_file():
            raise StorageError(
                f"Project manifest not found: {target}",
                code=ErrorCode.STORAGE_ERROR,
                details={"manifest": str(target)},
                recoverable=False,
            )
        return ProjectManifest.model_validate(read_json(target))

    def repair_directories(self, project_id: str) -> ProjectPaths:
        """Recreate any missing §43 subdirectory. Idempotent."""
        project = self._projects.require(project_id)
        project_paths = self._paths.project(project.id).create()
        self.write_manifest(project)
        return project_paths

    # -- helpers --------------------------------------------------------

    def _validate_output_format(self, resolution: int, fps: int) -> None:
        video = self._config.video
        if resolution not in video.supported_resolutions:
            raise ValidationError(
                f"Resolution {resolution}p is not supported.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={
                    "resolution": resolution,
                    "supported": video.supported_resolutions,
                },
                recoverable=False,
            )
        if fps not in video.supported_fps:
            raise ValidationError(
                f"{fps} fps is not supported.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"fps": fps, "supported": video.supported_fps},
                recoverable=False,
            )


__all__ = ["ProjectManager"]
