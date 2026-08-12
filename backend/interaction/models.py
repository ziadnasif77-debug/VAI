"""Interaction models: editing intent, questions, commands, conversation.

This is the vocabulary of the interaction layer. It sits *above* the pipeline:
nothing here is imported by analysis, moments, narrative, timeline or
rendering. Those layers consume a resolved :class:`EditingIntent` and know
nothing about chat.

Three interaction kinds, deliberately distinguished:

* ``EDITING_INSTRUCTION`` -- "focus on clutches" -- changes the editing intent.
* ``COMMAND``             -- "delete clip 5"    -- changes the EDL.
* ``QUESTION``            -- "what was the best moment?" -- changes nothing.

The intent is a *dynamic* structure: known dimensions are typed, and
``custom`` carries anything a future preset or instruction needs without a
schema migration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.core.models.enums import GameEventType, MomentType, VideoMode


class InteractionType(str, Enum):
    """What the user's message is trying to do."""

    QUESTION = "question"
    COMMAND = "command"
    EDITING_INSTRUCTION = "editing_instruction"


class IntentSource(str, Enum):
    """Where an intent change came from, for auditability."""

    DEFAULT = "default"
    PRESET = "preset"
    PROJECT_SETTINGS = "project_settings"
    INSTRUCTION = "instruction"
    COMMAND = "command"


class Pacing(str, Enum):
    SLOW = "slow"
    MEDIUM = "medium"
    FAST = "fast"
    VERY_FAST = "very_fast"


class DeadTimePolicy(str, Enum):
    KEEP = "keep"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class Level(str, Enum):
    """A generic low/medium/high dial, reused by several dimensions."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EffectsLevel(str, Enum):
    NONE = "none"
    MINIMAL = "minimal"
    MODERATE = "moderate"
    HEAVY = "heavy"


class CaptionPolicy(str, Enum):
    NONE = "none"
    IMPORTANT_ONLY = "important_only"
    STANDARD = "standard"
    FULL = "full"


class MusicPolicy(str, Enum):
    NONE = "none"
    SUBTLE = "subtle"
    PROMINENT = "prominent"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ListDelta(_Base):
    """A change to a list-valued dimension.

    "Focus on clutches" adds to the priorities rather than replacing them, so
    a later instruction never silently discards an earlier one (§11). ``set``
    is the explicit escape hatch for "only these".
    """

    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    set: list[str] | None = None

    def apply(self, current: list[str]) -> list[str]:
        """Return ``current`` with this delta applied, preserving order."""
        result = list(self.set) if self.set is not None else list(current)
        for value in self.add:
            if value not in result:
                result.append(value)
        return [value for value in result if value not in set(self.remove)]

    def is_empty(self) -> bool:
        return not self.add and not self.remove and self.set is None


class EditingIntent(_Base):
    """The resolved editing brief the pipeline reads.

    Every field has a usable default, so the system works with no instructions
    at all -- a project can go from import to final video without the user
    typing a word.
    """

    mode: VideoMode | None = None
    target_duration_seconds: int | None = None

    pacing: Pacing = Pacing.MEDIUM
    dead_time_policy: DeadTimePolicy = DeadTimePolicy.BALANCED
    context_preservation: Level = Level.MEDIUM
    effects: EffectsLevel = EffectsLevel.MODERATE
    captions: CaptionPolicy = CaptionPolicy.STANDARD
    music: MusicPolicy = MusicPolicy.SUBTLE
    variety: Level = Level.MEDIUM

    priority_moment_types: list[MomentType] = Field(default_factory=list)
    priority_events: list[GameEventType] = Field(default_factory=list)
    avoid_moment_types: list[MomentType] = Field(default_factory=list)

    style: str = "default"
    preset: str | None = None

    #: Forward compatibility. A new preset or instruction can carry a dimension
    #: the schema does not know yet without a migration (§3 "dynamic").
    custom: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _priority_and_avoid_disjoint(self) -> EditingIntent:
        overlap = set(self.priority_moment_types) & set(self.avoid_moment_types)
        if overlap:
            raise ValueError(
                "A moment type cannot be both prioritised and avoided: "
                + ", ".join(sorted(item.value for item in overlap))
            )
        return self


class IntentDelta(_Base):
    """One change to the intent. ``None`` means "leave this dimension alone".

    Instructions accumulate as an ordered log of deltas rather than replacing
    the whole intent, which is what makes a follow-up like "and fewer effects"
    keep everything said before it (§11).
    """

    mode: VideoMode | None = None
    target_duration_seconds: int | None = None
    pacing: Pacing | None = None
    dead_time_policy: DeadTimePolicy | None = None
    context_preservation: Level | None = None
    effects: EffectsLevel | None = None
    captions: CaptionPolicy | None = None
    music: MusicPolicy | None = None
    variety: Level | None = None
    style: str | None = None

    priority_moment_types: ListDelta | None = None
    priority_events: ListDelta | None = None
    avoid_moment_types: ListDelta | None = None

    custom: dict[str, Any] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        """Whether this delta would change nothing."""
        scalars = self.model_dump(
            exclude={"priority_moment_types", "priority_events", "avoid_moment_types", "custom"},
            exclude_none=True,
        )
        lists = [self.priority_moment_types, self.priority_events, self.avoid_moment_types]
        return not scalars and not self.custom and all(
            item is None or item.is_empty() for item in lists
        )


class IntentUpdate(_Base):
    """A recorded, ordered intent change with its provenance."""

    sequence: int = Field(ge=0)
    source: IntentSource
    delta: IntentDelta
    raw_text: str | None = Field(
        default=None, description="The user's words, kept verbatim for auditability."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceKind(str, Enum):
    """What an answer is grounded in."""

    MOMENT = "moment"
    GAME_EVENT = "game_event"
    AUDIO_EVENT = "audio_event"
    TRANSCRIPT = "transcript"
    SCENE = "scene"
    TIMELINE_CLIP = "timeline_clip"
    PROJECT = "project"


class Evidence(_Base):
    """One record backing an answer.

    Answers cite the rows they came from. An assertion with no evidence is a
    hallucination, and §12 forbids inventing reasons that are not in the data.
    """

    kind: EvidenceKind
    id: str | None = None
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    detail: str = ""
    score: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Evidence:
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds < self.start_seconds
        ):
            raise ValueError("Evidence end_seconds must not precede start_seconds.")
        return self


class AnswerSource(str, Enum):
    """How an answer was produced -- and therefore what it cost.

    §20: the cheapest source that can answer correctly is the one used.
    """

    DATABASE = "database"
    LLM = "llm"
    UNAVAILABLE = "unavailable"


class Answer(_Base):
    """A reply to a question about the video."""

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: AnswerSource = AnswerSource.DATABASE
    evidence: list[Evidence] = Field(default_factory=list)
    #: True when the pipeline has not produced the data the question needs.
    #: The answer then says so instead of guessing (§17).
    requires_analysis: bool = False

    @model_validator(mode="after")
    def _grounded(self) -> Answer:
        if self.source is AnswerSource.DATABASE and not self.evidence and self.confidence > 0.5:
            raise ValueError(
                "A confident database-backed answer must cite the records it came from."
            )
        return self


class CommandKind(str, Enum):
    """Commands that change the edit (§9)."""

    DELETE_CLIP = "delete_clip"
    RESTORE_CLIP = "restore_clip"
    DELETE_AT_TIMESTAMP = "delete_at_timestamp"
    ADD_MOMENT = "add_moment"
    SET_DURATION = "set_duration"
    REVERT_VERSION = "revert_version"
    #: The timeline has been able to trim, split and move since Phase 8, and
    #: the API and the timeline screen have exposed all three -- but the chat
    #: could not reach them, so "trim two seconds off the last clip" failed in
    #: conversation while the same edit was two clicks away. The capability was
    #: never the gap; the vocabulary was.
    TRIM_CLIP = "trim_clip"
    SPLIT_CLIP = "split_clip"
    MOVE_CLIP = "move_clip"


class EditCommand(_Base):
    """A parsed, validated command. Never a shell string, never free text.

    The LLM's role stops at producing one of these; applying it is ordinary
    application code (§9, and §85's rule that the model never executes
    anything).
    """

    kind: CommandKind
    clip_index: int | None = Field(default=None, ge=0)
    moment_id: str | None = None
    timestamp_seconds: float | None = Field(default=None, ge=0)
    target_duration_seconds: int | None = None
    version: int | None = Field(default=None, ge=1)
    #: Seconds to move a clip's in/out points. Negative starts earlier or ends
    #: sooner; the timeline refuses a trim that would invent footage or leave
    #: less than a readable clip (§42).
    start_delta: float | None = None
    end_delta: float | None = None
    #: Where a moved clip lands, numbered from 1 like every clip index here.
    to_index: int | None = Field(default=None, ge=1)
    raw_text: str | None = None

    @model_validator(mode="after")
    def _has_required_argument(self) -> EditCommand:
        required: dict[CommandKind, tuple[str, ...]] = {
            CommandKind.DELETE_CLIP: ("clip_index",),
            CommandKind.RESTORE_CLIP: ("clip_index",),
            CommandKind.DELETE_AT_TIMESTAMP: ("timestamp_seconds",),
            CommandKind.ADD_MOMENT: ("moment_id", "timestamp_seconds"),
            CommandKind.SET_DURATION: ("target_duration_seconds",),
            CommandKind.REVERT_VERSION: ("version",),
            CommandKind.TRIM_CLIP: ("start_delta", "end_delta"),
            CommandKind.SPLIT_CLIP: ("timestamp_seconds",),
            CommandKind.MOVE_CLIP: ("to_index",),
        }
        expected = required[self.kind]
        if not any(getattr(self, field) is not None for field in expected):
            raise ValueError(
                f"Command {self.kind.value!r} requires one of: {', '.join(expected)}."
            )
        return self


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationMessage(_Base):
    """One turn of the project conversation (§11)."""

    id: str
    project_id: str
    role: MessageRole
    text: str
    interaction_type: InteractionType | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class InteractionResult(_Base):
    """What the interaction layer did with one user message."""

    interaction_type: InteractionType
    message: str = Field(description="What to show the user.")
    answer: Answer | None = None
    intent: EditingIntent | None = None
    applied_command: EditCommand | None = None
    #: True when the edit changed and the video must be rendered again. The
    #: analysis is never re-run for this (§10).
    requires_rerender: bool = False
    edit_version: int | None = None


class EditVersion(_Base):
    """A snapshot of the edit, so "go back" costs nothing (§19)."""

    id: str
    project_id: str
    version: int = Field(ge=1)
    label: str | None = None
    reason: str | None = None
    intent: EditingIntent
    clips: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


__all__ = [
    "Answer",
    "AnswerSource",
    "CaptionPolicy",
    "CommandKind",
    "ConversationMessage",
    "DeadTimePolicy",
    "EditCommand",
    "EditVersion",
    "EditingIntent",
    "EffectsLevel",
    "Evidence",
    "EvidenceKind",
    "IntentDelta",
    "IntentSource",
    "IntentUpdate",
    "InteractionResult",
    "InteractionType",
    "Level",
    "ListDelta",
    "MessageRole",
    "MusicPolicy",
    "Pacing",
]
