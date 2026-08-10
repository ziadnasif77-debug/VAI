"""Media ingestion (SPEC sections 4, 42, 46 UPLOAD stage, 126 step 04).

Attaching a recording to a project means: check it exists and is a container we
accept, hash it, register it, and queue the stage chain. Stream metadata comes
from the PROBE stage (Phase 2) -- ingestion never opens the media.

The source file is never modified. Copying it into the project is opt-in, and
even then the copy is written once and then treated as read-only (§42).
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from backend.config.paths import Paths
from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, MediaError, StorageError
from backend.core.fs import file_checksum, sanitize_filename
from backend.core.ids import new_id
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.core.models.enums import JobStage, MediaRole, MediaState, ProjectStatus
from backend.core.models.media import (
    SUPPORTED_AUDIO_CONTAINERS,
    SUPPORTED_CONTAINERS,
    Media,
    MediaImport,
)
from backend.database.connection import Database
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.projects import ProjectRepository
from backend.services.job_manager import JobManager

logger = get_logger("services.media_ingestion", LogChannel.PIPELINE)

#: Roles that must be an audio-or-video container; the rest accept audio-only.
_VIDEO_ROLES = frozenset({MediaRole.GAMEPLAY, MediaRole.WEBCAM})


class MediaIngestionService:
    """Registers source recordings against a project."""

    def __init__(
        self,
        database: Database,
        paths: Paths,
        config: AppConfig,
        job_manager: JobManager | None = None,
    ) -> None:
        self._db = database
        self._paths = paths
        self._config = config
        self._media = MediaRepository(database)
        self._projects = ProjectRepository(database)
        self._jobs = job_manager or JobManager(database, config)

    def import_media(self, project_id: str, request: MediaImport) -> Media:
        """Attach a file to a project and queue its analysis chain."""
        project = self._projects.require(project_id)
        source = self._validate_source(request)

        with log_duration(
            logger,
            "Imported media",
            project_id=project_id,
            role=request.role.value,
        ) as fields:
            checksum = file_checksum(source)
            fields["checksum"] = checksum
            fields["size_bytes"] = source.stat().st_size

            existing = self._media.find_by_checksum(project_id, checksum)
            if existing is not None:
                raise MediaError(
                    f"This file is already imported into the project as {existing.id}.",
                    code=ErrorCode.MEDIA_ALREADY_IMPORTED,
                    details={
                        "media_id": existing.id,
                        "filename": existing.filename,
                        "checksum": checksum,
                    },
                    recoverable=False,
                )

            stored_path = source
            if request.copy_into_project:
                stored_path = self._copy_into_project(project_id, source)
                fields["copied"] = True

            now = datetime.now(timezone.utc)
            media = Media(
                id=new_id("media"),
                project_id=project_id,
                role=request.role,
                state=MediaState.REGISTERED,
                source_path=str(stored_path),
                filename=stored_path.name,
                container=stored_path.suffix.lower(),
                size_bytes=stored_path.stat().st_size,
                checksum=checksum,
                created_at=now,
                updated_at=now,
            )

            with self._db.transaction():
                self._media.create(media)
                self._jobs.queue_import_chain(project_id, media.id)
                if project.status is ProjectStatus.CREATED:
                    self._projects.update(
                        project.model_copy(update={"status": ProjectStatus.IMPORTING})
                    )

            fields["media_id"] = media.id
            fields["filename"] = media.filename

        return media

    def list_media(self, project_id: str, *, role: MediaRole | None = None) -> list[Media]:
        self._projects.require(project_id)
        return self._media.list_for_project(project_id, role=role)

    def get_media(self, media_id: str) -> Media:
        return self._media.require(media_id)

    def remove_media(self, media_id: str, *, delete_file: bool = False) -> None:
        """Detach a file from its project.

        ``delete_file`` only ever removes a copy the application itself made
        inside the project directory. A referenced original is never deleted.
        """
        media = self._media.require(media_id)
        project_source_dir = self._paths.project(media.project_id).source
        path = Path(media.source_path)

        with self._db.transaction():
            self._media.delete(media_id)

        if delete_file and _is_inside(path, project_source_dir):
            path.unlink(missing_ok=True)

        logger.info(
            "Media removed",
            extra={
                "project_id": media.project_id,
                "media_id": media_id,
                "file_deleted": delete_file and _is_inside(path, project_source_dir),
            },
        )

    # -- internals ------------------------------------------------------

    def _validate_source(self, request: MediaImport) -> Path:
        """Check the path exists, is a file, is non-empty and is a known container."""
        path = Path(request.path).expanduser()
        if not path.is_absolute():
            raise MediaError(
                "Media path must be absolute.",
                code=ErrorCode.PATH_NOT_ALLOWED,
                details={"path": str(path)},
                recoverable=False,
            )
        if not path.exists():
            raise MediaError(
                f"File not found: {path}",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"path": str(path)},
                recoverable=False,
            )
        if not path.is_file():
            raise MediaError(
                f"Not a file: {path}",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"path": str(path)},
                recoverable=False,
            )

        extension = path.suffix.lower()
        allowed = (
            SUPPORTED_CONTAINERS
            if request.role in _VIDEO_ROLES
            else SUPPORTED_CONTAINERS | SUPPORTED_AUDIO_CONTAINERS
        )
        if extension not in allowed:
            raise MediaError(
                f"Unsupported container {extension or '(none)'} for role "
                f"{request.role.value!r}.",
                code=ErrorCode.UNSUPPORTED_CONTAINER,
                details={
                    "path": str(path),
                    "container": extension,
                    "supported": sorted(allowed),
                },
                recoverable=False,
            )

        if path.stat().st_size == 0:
            raise MediaError(
                f"File is empty: {path}",
                code=ErrorCode.MEDIA_CORRUPTED,
                details={"path": str(path)},
                recoverable=False,
            )
        return path

    def _copy_into_project(self, project_id: str, source: Path) -> Path:
        """Copy a source into ``projects/<id>/source/``, never overwriting."""
        destination_dir = self._paths.project(project_id).source
        destination_dir.mkdir(parents=True, exist_ok=True)

        stem = sanitize_filename(source.stem)
        suffix = source.suffix.lower()
        destination = destination_dir / f"{stem}{suffix}"
        counter = 1
        while destination.exists():
            destination = destination_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            code = (
                ErrorCode.DISK_SPACE_EXHAUSTED
                if getattr(exc, "errno", None) == 28
                else ErrorCode.STORAGE_ERROR
            )
            raise StorageError(
                f"Cannot copy {source} into the project: {exc}",
                code=code,
                details={"source": str(source), "destination": str(destination)},
                cause=exc,
            ) from exc
        return destination


def _is_inside(path: Path, base: Path) -> bool:
    try:
        return base.resolve() in path.resolve().parents
    except OSError:  # pragma: no cover - unreadable path
        return False


#: The stage the import chain begins with. Kept here so the queueing logic and
#: the ingestion service agree on where the pipeline starts.
FIRST_STAGE = JobStage.IMPORT


__all__ = ["FIRST_STAGE", "MediaIngestionService"]
