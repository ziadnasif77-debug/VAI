"""Publication models -- the seam for distributing a finished render.

The MVP delivers one way: write the rendered MP4 where the user asks
(:class:`~backend.core.models.enums.PublishTarget.LOCAL_FILE`). YouTube
auto-publish is planned but deliberately not implemented here.

What matters now is that adding it later is *additive*:

* the pipeline already ends in EXPORT and PUBLISH stages;
* the database already stores publication attempts and their outcomes;
* :class:`VideoMetadata` already carries everything an upload needs -- title,
  description, tags, category, visibility, chapters, thumbnail -- so metadata
  generation does not have to be retrofitted into the narrative stage;
* :class:`PublishRequest` / :class:`PublishResult` are destination-agnostic.

A new destination therefore means writing one publisher class and registering
it. It must not mean changing the analysis, moment, narrative, timeline or
render layers.

Two rules constrain any future destination, from §50/§51: nothing uploads
automatically, and credentials never live in these models -- a publisher
resolves them from the credential store at call time.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.core.models.enums import PublishStatus, PublishTarget, PublishVisibility

#: YouTube's own limits. Applied to every target so a title that is valid
#: locally cannot become invalid the day a real destination is enabled.
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 5000
MAX_TAGS = 500
MAX_TAGS_TOTAL_CHARS = 500


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Chapter(_Base):
    """A chapter marker derived from the story sections or selected moments.

    Chapters are useful in the local player and are exactly what a description
    field needs when a destination is added, so they are produced during
    narrative construction rather than at publish time.
    """

    title: str = Field(min_length=1, max_length=100)
    start_seconds: float = Field(ge=0)

    @field_validator("title")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Chapter title must not be blank.")
        return stripped


class VideoMetadata(_Base):
    """Publish-facing description of the finished video.

    Stored per project and versioned with it, so re-rendering keeps the
    metadata and re-editing can update it without a schema change.
    """

    title: str = Field(default="", max_length=MAX_TITLE_LENGTH)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LENGTH)
    tags: list[str] = Field(default_factory=list)
    category: str | None = Field(
        default=None, description="Destination-specific category, e.g. YouTube's 'Gaming'."
    )
    language: str = "auto"
    visibility: PublishVisibility = PublishVisibility.PRIVATE
    thumbnail_path: str | None = None
    chapters: list[Chapter] = Field(default_factory=list)
    made_for_kids: bool = False

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        cleaned = [tag.strip() for tag in value if tag.strip()]
        if len(cleaned) > MAX_TAGS:
            raise ValueError(f"At most {MAX_TAGS} tags are allowed.")
        total = sum(len(tag) for tag in cleaned)
        if total > MAX_TAGS_TOTAL_CHARS:
            raise ValueError(
                f"Tags exceed the {MAX_TAGS_TOTAL_CHARS} character budget ({total})."
            )
        return cleaned

    @model_validator(mode="after")
    def _chapters_ordered(self) -> VideoMetadata:
        starts = [chapter.start_seconds for chapter in self.chapters]
        if starts != sorted(starts):
            raise ValueError("Chapters must be ordered by start time.")
        if len(set(starts)) != len(starts):
            raise ValueError("Two chapters cannot start at the same timestamp.")
        if self.chapters and starts[0] != 0:
            raise ValueError("The first chapter must start at 0 seconds.")
        return self


class PublishRequest(_Base):
    """A user's explicit instruction to deliver a render somewhere.

    Never constructed by the pipeline on its own: publication is always a
    deliberate action (§51).
    """

    project_id: str
    render_id: str
    target: PublishTarget = PublishTarget.LOCAL_FILE
    metadata: VideoMetadata = Field(default_factory=VideoMetadata)
    destination: str | None = Field(
        default=None,
        description=(
            "Target-specific destination. A filesystem path for LOCAL_FILE; a "
            "channel or playlist identifier for a future upload target."
        ),
    )


class PublishResult(_Base):
    """Outcome of one publication attempt."""

    status: PublishStatus
    target: PublishTarget
    external_id: str | None = Field(
        default=None, description="Identifier assigned by the destination, when it assigns one."
    )
    external_url: str | None = None
    output_path: str | None = Field(
        default=None, description="Local path written, for filesystem destinations."
    )
    error_code: str | None = None
    error_message: str | None = None
    completed_at: datetime | None = None

    @field_validator("completed_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class Publication(_Base):
    """A persisted publication attempt (``publications`` table)."""

    id: str
    project_id: str
    render_id: str
    target: PublishTarget
    status: PublishStatus = PublishStatus.PENDING

    external_id: str | None = None
    external_url: str | None = None
    output_path: str | None = None

    error_code: str | None = None
    error_message: str | None = None

    #: The metadata as sent, so a later change to the project does not rewrite
    #: the history of what was actually published.
    metadata_snapshot: VideoMetadata = Field(default_factory=VideoMetadata)

    requested_at: datetime
    completed_at: datetime | None = None

    @field_validator("requested_at", "completed_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "MAX_TAGS",
    "MAX_TITLE_LENGTH",
    "Chapter",
    "Publication",
    "PublishRequest",
    "PublishResult",
    "VideoMetadata",
]
