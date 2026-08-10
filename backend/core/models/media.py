"""Media models (SPEC sections 4, 42, 43, 45).

A source recording is registered at import and then only ever read. Derived
artefacts (proxy, audio, frames) live beside it in the project directory; the
original file is never rewritten (§42).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.models.enums import MediaRole, MediaState

#: Containers accepted at import (§4). Extensible by design: adding a container
#: means adding it here and confirming the probe/decode path handles it.
SUPPORTED_CONTAINERS: frozenset[str] = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi"})

#: Audio-only companions (§4: gameplay + separate microphone track).
SUPPORTED_AUDIO_CONTAINERS: frozenset[str] = frozenset({".wav", ".flac", ".m4a", ".mp3", ".ogg"})


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MediaImport(_Base):
    """Request to attach a source file to a project."""

    path: str = Field(min_length=1, description="Absolute path to the source recording.")
    role: MediaRole = MediaRole.GAMEPLAY
    copy_into_project: bool = Field(
        default=False,
        description=(
            "Copy the file into projects/<id>/source/ instead of referencing it in "
            "place. Referencing is the default: gameplay recordings are large and the "
            "original is never modified either way (§42)."
        ),
    )

    @field_validator("path")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Media path must not be blank.")
        return stripped


class MediaMetadata(_Base):
    """Container and stream metadata.

    Populated by the PROBE stage (Phase 2). Every field is optional because a
    media row exists from the moment of import, before ffprobe has run.
    """

    duration_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    fps: float | None = Field(default=None, gt=0)
    video_codec: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = Field(default=None, ge=1)
    channels: int | None = Field(default=None, ge=1)
    bitrate: int | None = Field(default=None, ge=0)
    has_video: bool | None = None
    has_audio: bool | None = None


class Media(_Base):
    """A source file attached to a project."""

    id: str
    project_id: str
    role: MediaRole = MediaRole.GAMEPLAY
    state: MediaState = MediaState.REGISTERED

    #: Where the file actually lives. Absolute when referenced in place.
    source_path: str
    filename: str
    container: str = Field(description="Lowercase extension including the dot, e.g. '.mp4'.")
    size_bytes: int = Field(ge=0)

    #: Content hash. Detects a file already imported into the project and forms
    #: the ``video_hash`` component of every analysis cache key (§48).
    checksum: str
    checksum_algorithm: Literal["sha256", "blake2b"] = "sha256"

    metadata: MediaMetadata = Field(default_factory=MediaMetadata)

    created_at: datetime
    updated_at: datetime

    error_code: str | None = None
    error_message: str | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_validator("container")
    @classmethod
    def _normalise_container(cls, value: str) -> str:
        normalised = value.lower().strip()
        if not normalised.startswith("."):
            normalised = f".{normalised}"
        return normalised

    @property
    def is_video(self) -> bool:
        return self.container in SUPPORTED_CONTAINERS


class MediaTrack(_Base):
    """One stream inside a media file (§45 ``media_tracks``).

    Filled in by the PROBE stage. Microphone detection (§19) works by finding a
    second audio track, or a separate audio-role media file.
    """

    id: str
    media_id: str
    track_index: int = Field(ge=0)
    kind: Literal["video", "audio", "subtitle", "data"]
    codec: str | None = None
    language: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    fps: float | None = Field(default=None, gt=0)
    sample_rate: int | None = Field(default=None, ge=1)
    channels: int | None = Field(default=None, ge=1)
    bitrate: int | None = Field(default=None, ge=0)
    is_default: bool = False


__all__ = [
    "SUPPORTED_AUDIO_CONTAINERS",
    "SUPPORTED_CONTAINERS",
    "Media",
    "MediaImport",
    "MediaMetadata",
    "MediaTrack",
]
