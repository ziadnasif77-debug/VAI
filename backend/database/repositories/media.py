"""Media persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from backend.core.errors import ErrorCode, NotFoundError
from backend.core.models.enums import MediaRole, MediaState
from backend.core.models.media import Media, MediaMetadata
from backend.database.connection import Database

_COLUMNS = (
    "id, project_id, role, state, source_path, filename, container, size_bytes, checksum, "
    "checksum_algorithm, duration_seconds, width, height, fps, video_codec, audio_codec, "
    "sample_rate, channels, bitrate, has_video, has_audio, created_at, updated_at, "
    "error_code, error_message"
)


class MediaRepository:
    """CRUD for the ``media`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def create(self, media: Media) -> Media:
        self._db.execute(
            f"INSERT INTO media ({_COLUMNS}) VALUES ("
            ":id, :project_id, :role, :state, :source_path, :filename, :container, "
            ":size_bytes, :checksum, :checksum_algorithm, :duration_seconds, :width, :height, "
            ":fps, :video_codec, :audio_codec, :sample_rate, :channels, :bitrate, :has_video, "
            ":has_audio, :created_at, :updated_at, :error_code, :error_message)",
            _to_row(media),
        )
        return media

    def get(self, media_id: str) -> Media | None:
        row = self._db.fetch_one(f"SELECT {_COLUMNS} FROM media WHERE id = ?", (media_id,))
        return _from_row(row) if row is not None else None

    def require(self, media_id: str) -> Media:
        media = self.get(media_id)
        if media is None:
            raise NotFoundError(
                f"Media {media_id!r} does not exist.",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"media_id": media_id},
                recoverable=False,
            )
        return media

    def list_for_project(
        self, project_id: str, *, role: MediaRole | None = None
    ) -> list[Media]:
        sql = f"SELECT {_COLUMNS} FROM media WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if role is not None:
            sql += " AND role = ?"
            parameters.append(role.value)
        sql += " ORDER BY created_at ASC"
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def find_by_checksum(self, project_id: str, checksum: str) -> Media | None:
        """Return an already-imported file with the same content (§48 identity)."""
        row = self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM media WHERE project_id = ? AND checksum = ?",
            (project_id, checksum),
        )
        return _from_row(row) if row is not None else None

    def update(self, media: Media) -> Media:
        updated = media.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        cursor = self._db.execute(
            "UPDATE media SET role = :role, state = :state, source_path = :source_path, "
            "filename = :filename, container = :container, size_bytes = :size_bytes, "
            "checksum = :checksum, checksum_algorithm = :checksum_algorithm, "
            "duration_seconds = :duration_seconds, width = :width, height = :height, "
            "fps = :fps, video_codec = :video_codec, audio_codec = :audio_codec, "
            "sample_rate = :sample_rate, channels = :channels, bitrate = :bitrate, "
            "has_video = :has_video, has_audio = :has_audio, updated_at = :updated_at, "
            "error_code = :error_code, error_message = :error_message WHERE id = :id",
            _to_row(updated),
        )
        if cursor.rowcount == 0:
            raise NotFoundError(
                f"Media {media.id!r} does not exist.",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"media_id": media.id},
                recoverable=False,
            )
        return updated

    def delete(self, media_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM media WHERE id = ?", (media_id,))
        return cursor.rowcount > 0

    def total_duration(self, project_id: str) -> float:
        """Total probed duration of a project's gameplay sources, in seconds."""
        row = self._db.fetch_one(
            "SELECT COALESCE(SUM(duration_seconds), 0.0) AS total FROM media "
            "WHERE project_id = ? AND duration_seconds IS NOT NULL",
            (project_id,),
        )
        return float(row["total"]) if row is not None else 0.0


def _to_row(media: Media) -> dict[str, object]:
    metadata = media.metadata
    return {
        "id": media.id,
        "project_id": media.project_id,
        "role": media.role.value,
        "state": media.state.value,
        "source_path": media.source_path,
        "filename": media.filename,
        "container": media.container,
        "size_bytes": media.size_bytes,
        "checksum": media.checksum,
        "checksum_algorithm": media.checksum_algorithm,
        "duration_seconds": metadata.duration_seconds,
        "width": metadata.width,
        "height": metadata.height,
        "fps": metadata.fps,
        "video_codec": metadata.video_codec,
        "audio_codec": metadata.audio_codec,
        "sample_rate": metadata.sample_rate,
        "channels": metadata.channels,
        "bitrate": metadata.bitrate,
        "has_video": _to_int(metadata.has_video),
        "has_audio": _to_int(metadata.has_audio),
        "created_at": media.created_at.isoformat(),
        "updated_at": media.updated_at.isoformat(),
        "error_code": media.error_code,
        "error_message": media.error_message,
    }


def _from_row(row: sqlite3.Row) -> Media:
    return Media(
        id=row["id"],
        project_id=row["project_id"],
        role=MediaRole(row["role"]),
        state=MediaState(row["state"]),
        source_path=row["source_path"],
        filename=row["filename"],
        container=row["container"],
        size_bytes=row["size_bytes"],
        checksum=row["checksum"],
        checksum_algorithm=row["checksum_algorithm"],
        metadata=MediaMetadata(
            duration_seconds=row["duration_seconds"],
            width=row["width"],
            height=row["height"],
            fps=row["fps"],
            video_codec=row["video_codec"],
            audio_codec=row["audio_codec"],
            sample_rate=row["sample_rate"],
            channels=row["channels"],
            bitrate=row["bitrate"],
            has_video=_to_bool(row["has_video"]),
            has_audio=_to_bool(row["has_audio"]),
        ),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _to_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _to_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


__all__ = ["MediaRepository"]
