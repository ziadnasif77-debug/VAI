"""Local-file publisher -- the MVP's export path.

Copies the QA-passed render to a location the user chose. Nothing leaves the
machine (§50, §51).
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from backend.core.errors import ErrorCode
from backend.core.fs import ensure_directory, sanitize_filename
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import PublishStatus, PublishTarget
from backend.core.models.publishing import PublishRequest, PublishResult
from backend.publishing.base import PublishError

logger = get_logger("publishing.local_file", LogChannel.RENDERING)


class LocalFilePublisher:
    """Writes the finished render to a filesystem destination."""

    target = PublishTarget.LOCAL_FILE

    def __init__(self, *, default_directory: Path | None = None) -> None:
        self._default_directory = Path(default_directory) if default_directory else None

    def is_configured(self) -> bool:
        """Always available: it needs no credentials."""
        return True

    def publish(self, request: PublishRequest, render_path: Path) -> PublishResult:
        """Copy ``render_path`` to the requested destination."""
        source = Path(render_path)
        if not source.is_file():
            raise PublishError(
                f"Render file not found: {source}",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"render_path": str(source), "render_id": request.render_id},
                recoverable=False,
            )

        destination = self._resolve_destination(request, source)
        if destination.resolve() == source.resolve():
            raise PublishError(
                "Publish destination is the render itself; choose a different location.",
                code=ErrorCode.PUBLISH_FAILED,
                details={"destination": str(destination)},
                recoverable=False,
            )

        try:
            ensure_directory(destination.parent)
            # copy2 preserves timestamps, which keeps the exported file's
            # provenance obvious next to the original render.
            shutil.copy2(source, destination)
        except OSError as exc:
            code = (
                ErrorCode.DISK_SPACE_EXHAUSTED
                if getattr(exc, "errno", None) == 28
                else ErrorCode.PUBLISH_FAILED
            )
            raise PublishError(
                f"Cannot write {destination}: {exc}",
                code=code,
                details={"destination": str(destination), "source": str(source)},
                cause=exc,
            ) from exc

        logger.info(
            "Exported render to local file",
            extra={
                "project_id": request.project_id,
                "destination": str(destination),
                "size_bytes": destination.stat().st_size,
            },
        )
        return PublishResult(
            status=PublishStatus.COMPLETED,
            target=self.target,
            output_path=str(destination),
            completed_at=datetime.now(timezone.utc),
        )

    def _resolve_destination(self, request: PublishRequest, source: Path) -> Path:
        """Work out where the file should land.

        An explicit ``destination`` wins. A directory receives a filename built
        from the video title; a full path is used as given. With no destination
        the configured default directory is used.
        """
        if request.destination:
            candidate = Path(request.destination).expanduser()
            if candidate.is_dir() or not candidate.suffix:
                return candidate / self._filename_for(request, source)
            return candidate

        if self._default_directory is not None:
            return self._default_directory / self._filename_for(request, source)

        raise PublishError(
            "No destination given and no default export directory configured.",
            code=ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED,
            details={"project_id": request.project_id},
            recoverable=False,
        )

    @staticmethod
    def _filename_for(request: PublishRequest, source: Path) -> str:
        """Build a Windows-safe filename from the video title."""
        title = request.metadata.title.strip()
        stem = sanitize_filename(title) if title else source.stem
        return f"{stem}{source.suffix}"


__all__ = ["LocalFilePublisher"]
