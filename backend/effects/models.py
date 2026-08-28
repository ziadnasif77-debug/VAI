"""Effect models (SPEC §68, §69, §70).

An effect is data on the timeline, never a call into a renderer. The planner
decides *which* effect goes *where*; FFmpeg or Remotion decides how to realise
it. That split is what lets the effects library grow without touching either
renderer, and what keeps §69's "never apply globally" checkable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.models.enums import (
    EffectCategory,
    EffectEngine,
    EffectType,
    GameEventType,
    MomentType,
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EffectInstance(_Base):
    """One effect placed on the timeline.

    Times are relative to the clip when ``clip_id`` is set, and to the programme
    timeline otherwise -- the same convention the ``timeline_effects`` table
    uses.
    """

    effect: EffectType
    engine: EffectEngine
    category: EffectCategory
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    clip_id: str | None = None
    moment_id: str | None = None
    params: dict[str, object] = Field(default_factory=dict)
    #: How strongly the effect is applied, after style and intent scaling.
    strength: float = Field(default=1.0, ge=0.0, le=1.0)
    #: Why the planner chose it. Surfaced in review so a user can see the
    #: reasoning rather than a mystery zoom (§80).
    reason: str = ""

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds

    def overlaps(self, other: EffectInstance) -> bool:
        return self.start_seconds < other.end_seconds and other.start_seconds < self.end_seconds


class EffectCandidate(_Base):
    """An effect the planner is considering, with its score.

    Kept separate from :class:`EffectInstance` so budget enforcement can rank
    and drop candidates before anything reaches the timeline.
    """

    effect: EffectType
    moment_id: str
    moment_type: MomentType
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    clip_id: str | None = None
    #: Moment score that earned it, before style weighting.
    moment_score: float = Field(ge=0.0, le=1.0)
    #: Ranking score: moment score adjusted by the style's boost/suppress.
    priority: float = Field(ge=0.0)
    matched_events: list[GameEventType] = Field(default_factory=list)
    #: Doctrine §9: this candidate's rung on its effect's escalation ladder,
    #: as a multiplier on the plan's intensity. 1.0 outside any ladder; 0.0
    #: means the run's opening firing, which stays quiet.
    intensity_multiplier: float = Field(default=1.0, ge=0.0)
    reason: str = ""


class EffectPlan(_Base):
    """The full set of effects for one edit, plus what was dropped and why.

    The rejections matter: silently discarding half the candidates would make
    the effects engine impossible to reason about or tune.
    """

    instances: list[EffectInstance] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    intensity: float = Field(ge=0.0, le=1.0)
    style: str = "default"

    @model_validator(mode="after")
    def _ordered(self) -> EffectPlan:
        starts = [instance.start_seconds for instance in self.instances]
        if starts != sorted(starts):
            raise ValueError("Effect instances must be ordered by start time.")
        return self

    @property
    def count(self) -> int:
        return len(self.instances)

    def for_engine(self, engine: EffectEngine) -> list[EffectInstance]:
        """Effects one renderer is responsible for.

        The FFmpeg pass and the Remotion overlay pass each read only their own
        half, so neither has to know the other exists.
        """
        return [instance for instance in self.instances if instance.engine is engine]

    def by_category(self, category: EffectCategory) -> list[EffectInstance]:
        return [instance for instance in self.instances if instance.category is category]

    def density_per_minute(self, duration_seconds: float) -> float:
        """Effects per minute of finished video."""
        if duration_seconds <= 0:
            return 0.0
        return self.count / (duration_seconds / 60.0)


__all__ = ["EffectCandidate", "EffectInstance", "EffectPlan"]
