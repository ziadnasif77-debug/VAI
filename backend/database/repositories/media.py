"""Media persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone

from backend.core.errors import ErrorCode, NotFoundError
from backend.core.models.enums import MediaRole, MediaState
from backend.core.models.media import Media, MediaMetadata, MediaTrack
from backend.database.connection import Database

_COLUMNS = (
    "id, project_id, role, state, source_path, filename, container, size_bytes, checksum, "
    "checksum_algorithm, duration_seconds, width, height, fps, video_codec, audio_codec, "
    "sample_rate, channels, bitrate, has_video, has_audio, created_at, updated_at, "
    "error_code, error_message"
)

_TRACK_COLUMNS = (
    "id, media_id, track_index, kind, codec, language, width, height, fps, "
    "sample_rate, channels, bitrate, is_default"
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


class MediaTrackRepository:
    """CRUD for the ``media_tracks`` table (§45).

    Written by the PROBE stage. The rows are what §19 reads to decide whether a
    recording carries a separate microphone, so they are replaced wholesale on
    every probe rather than merged: a re-probe reflects the file as it is now.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_media(self, media_id: str, tracks: Iterable[MediaTrack]) -> list[MediaTrack]:
        """Replace every track row of one media file.

        Callers wrap this in a transaction so a probe never leaves a file with
        half its streams recorded.
        """
        rows = list(tracks)
        self._db.execute("DELETE FROM media_tracks WHERE media_id = ?", (media_id,))
        if rows:
            self._db.executemany(
                f"INSERT INTO media_tracks ({_TRACK_COLUMNS}) VALUES ("
                ":id, :media_id, :track_index, :kind, :codec, :language, :width, :height, "
                ":fps, :sample_rate, :channels, :bitrate, :is_default)",
                [_track_to_row(track) for track in rows],
            )
        return rows

    def list_for_media(self, media_id: str) -> list[MediaTrack]:
        return [
            _track_from_row(row)
            for row in self._db.fetch_all(
                f"SELECT {_TRACK_COLUMNS} FROM media_tracks WHERE media_id = ? "
                "ORDER BY track_index ASC",
                (media_id,),
            )
        ]

    def audio_tracks(self, media_id: str) -> list[MediaTrack]:
        """Audio streams only -- the input to microphone detection (§19)."""
        return [track for track in self.list_for_media(media_id) if track.kind == "audio"]


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


def _track_to_row(track: MediaTrack) -> dict[str, object]:
    return {
        "id": track.id,
        "media_id": track.media_id,
        "track_index": track.track_index,
        "kind": track.kind,
        "codec": track.codec,
        "language": track.language,
        "width": track.width,
        "height": track.height,
        "fps": track.fps,
        "sample_rate": track.sample_rate,
        "channels": track.channels,
        "bitrate": track.bitrate,
        "is_default": int(track.is_default),
    }


def _track_from_row(row: sqlite3.Row) -> MediaTrack:
    return MediaTrack(
        id=row["id"],
        media_id=row["media_id"],
        track_index=row["track_index"],
        kind=row["kind"],
        codec=row["codec"],
        language=row["language"],
        width=row["width"],
        height=row["height"],
        fps=row["fps"],
        sample_rate=row["sample_rate"],
        channels=row["channels"],
        bitrate=row["bitrate"],
        is_default=bool(row["is_default"]),
    )


def _to_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _to_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


__all__ = ["MediaRepository", "MediaTrackRepository"]
