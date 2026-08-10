"""Project models (SPEC sections 42, 43, 59, 122)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.duration import (
    ABSOLUTE_MAX_OUTPUT_SECONDS,
    ABSOLUTE_MIN_OUTPUT_SECONDS,
)
from backend.core.models.enums import ProjectStatus, VideoMode
from backend.core.versions import (
    ANALYSIS_VERSION,
    APPLICATION_VERSION,
    PROJECT_MANIFEST_VERSION,
    SCHEMA_VERSION,
)

_LANGUAGE_RE = re.compile(r"^(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)$")

#: Game identifier: a profile directory name under ``profiles/`` (§22), or
#: ``auto`` to let detection decide, or ``generic`` for an unknown game (§23).
_GAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")

#: Declared once so the API, the manifest and the database agree. The *effective*
#: band may be narrowed by config/output.yaml; :class:`DurationPolicy` enforces
#: that at the service layer. These annotations are the outer product limits.
TargetDurationSeconds = Annotated[
    int,
    Field(
        ge=ABSOLUTE_MIN_OUTPUT_SECONDS,
        le=ABSOLUTE_MAX_OUTPUT_SECONDS,
        description=(
            "Requested output duration in seconds. Product limits from §6: "
            f"{ABSOLUTE_MIN_OUTPUT_SECONDS} (10:00) to {ABSOLUTE_MAX_OUTPUT_SECONDS} (60:00)."
        ),
    ),
]

#: §5: MVP renders 720p or 1080p at 30 or 60 fps, 16:9.
OutputResolution = Annotated[int, Field(ge=720, le=1080)]
OutputFps = Annotated[int, Field(ge=24, le=60)]


def utcnow() -> datetime:
    """Timezone-aware current time. All persisted timestamps are UTC."""
    return datetime.now(timezone.utc)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ProjectCreate(_Base):
    """Import-screen input (§59).

    The user picks video, game, target duration, mode, resolution and fps.
    """

    name: str = Field(min_length=1, max_length=200)
    target_duration_seconds: TargetDurationSeconds
    mode: VideoMode = VideoMode.STORY
    game: str = Field(
        default="auto",
        description="Game profile id, 'auto' to detect, or 'generic' for an unknown game.",
    )
    resolution: OutputResolution = 1080
    fps: OutputFps = 60
    language: str = Field(default="auto", max_length=32)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Project name must not be blank.")
        return stripped

    @field_validator("game")
    @classmethod
    def _valid_game(cls, value: str) -> str:
        normalised = value.strip().lower()
        if not _GAME_RE.match(normalised):
            raise ValueError(
                f"Invalid game id {value!r}; expected a profile directory name such as "
                "'valorant', or 'auto'/'generic'."
            )
        return normalised

    @field_validator("language")
    @classmethod
    def _valid_language(cls, value: str) -> str:
        normalised = value.strip()
        if not _LANGUAGE_RE.match(normalised):
            raise ValueError(
                f"Invalid language tag {value!r}; expected 'auto' or an ISO code such as "
                "'en' or 'ar'."
            )
        return normalised


class ProjectUpdate(_Base):
    """Mutable project fields. ``None`` means "leave unchanged".

    Changing the target duration or mode re-runs narrative and rendering only --
    never the analysis (§127).
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    target_duration_seconds: TargetDurationSeconds | None = None
    mode: VideoMode | None = None
    game: str | None = None
    resolution: OutputResolution | None = None
    fps: OutputFps | None = None
    language: str | None = Field(default=None, max_length=32)
    status: ProjectStatus | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Project name must not be blank.")
        return stripped

    @field_validator("game")
    @classmethod
    def _valid_game(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = value.strip().lower()
        if not _GAME_RE.match(normalised):
            raise ValueError(f"Invalid game id {value!r}.")
        return normalised

    @field_validator("language")
    @classmethod
    def _valid_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = value.strip()
        if not _LANGUAGE_RE.match(normalised):
            raise ValueError(f"Invalid language tag {value!r}.")
        return normalised

    def changed_fields(self) -> dict[str, Any]:
        """Return only the explicitly supplied fields."""
        return self.model_dump(exclude_unset=True, exclude_none=True)


class Project(_Base):
    """A persisted project.

    ``project_directory`` holds every derived artefact (§43). Source recordings
    are referenced, never rewritten (§42).
    """

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    status: ProjectStatus = ProjectStatus.CREATED

    target_duration_seconds: TargetDurationSeconds
    mode: VideoMode = VideoMode.STORY
    game: str = "auto"
    detected_game: str | None = Field(
        default=None, description="Game identified by detection, when it differs from `game`."
    )
    resolution: OutputResolution = 1080
    fps: OutputFps = 60
    aspect_ratio: str = "16:9"
    language: str = "auto"

    project_directory: str
    version: int = Field(default=1, ge=1, description="Project edit version.")

    application_version: str = APPLICATION_VERSION
    analysis_version: int = ANALYSIS_VERSION
    schema_version: int = SCHEMA_VERSION

    @field_validator("created_at", "updated_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @property
    def effective_game(self) -> str:
        """The profile to load: the detected game when known, else the request."""
        if self.game == "auto":
            return self.detected_game or "generic"
        return self.game


class ProjectManifest(_Base):
    """On-disk ``project.json`` (§43) with full version provenance (§49)."""

    manifest_version: int = PROJECT_MANIFEST_VERSION
    project: Project
    prompt_versions: dict[str, int] = Field(default_factory=dict)
    written_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def for_project(cls, project: Project, prompt_versions: dict[str, int]) -> ProjectManifest:
        return cls(project=project, prompt_versions=dict(prompt_versions))


__all__ = [
    "OutputFps",
    "OutputResolution",
    "Project",
    "ProjectCreate",
    "ProjectManifest",
    "ProjectUpdate",
    "TargetDurationSeconds",
    "utcnow",
]
