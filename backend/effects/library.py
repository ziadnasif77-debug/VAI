"""The effects library -- what exists, what it costs, who renders it.

Reads ``config/effects.yaml`` into a queryable form. Adding an effect is a
configuration change plus a renderer implementation; nothing in the planner or
the timeline needs to know the new name.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.config.schema import AppConfig, EffectSpecConfig, EffectStyleProfile
from backend.core.errors import ErrorCode, ValidationError
from backend.core.models.enums import (
    EffectCategory,
    EffectEngine,
    EffectType,
    GameEventType,
    MomentType,
)
from backend.interaction.models import EffectsLevel

#: How the editing intent's effects dial maps onto the global intensity.
INTENSITY_BY_LEVEL: dict[EffectsLevel, float] = {
    EffectsLevel.NONE: 0.0,
    EffectsLevel.MINIMAL: 0.3,
    EffectsLevel.MODERATE: 0.6,
    EffectsLevel.HEAVY: 1.0,
}

#: Multipliers a style profile applies to a candidate's ranking.
BOOST_MULTIPLIER = 1.6
SUPPRESS_MULTIPLIER = 0.0


@dataclass(frozen=True, slots=True)
class EffectDefinition:
    """One library entry, resolved for a given style and intensity."""

    effect: EffectType
    spec: EffectSpecConfig

    @property
    def engine(self) -> EffectEngine:
        return self.spec.engine

    @property
    def category(self) -> EffectCategory:
        return self.spec.category

    @property
    def is_structural(self) -> bool:
        """Transitions and captions: always allowed, never "earned"."""
        return self.spec.triggers.is_structural

    def matches(
        self,
        *,
        moment_type: MomentType | None,
        events: list[GameEventType],
        score: float,
    ) -> bool:
        """Whether this effect is appropriate for a moment."""
        triggers = self.spec.triggers
        if score < triggers.min_score:
            return False
        if triggers.is_structural:
            return True
        if moment_type is not None and moment_type in triggers.moment_types:
            return True
        event_names = {event.value for event in events}
        return bool(event_names & set(triggers.events))


class EffectLibrary:
    """Queryable view over the configured effects."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config.effects

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def definition(self, effect: EffectType) -> EffectDefinition:
        """Return one effect's definition.

        Raises:
            ValidationError: when the effect is not in the library. A timeline
                referencing an unknown effect must fail loudly, not render
                silently without it.
        """
        spec = self._config.spec(effect)
        if spec is None:
            raise ValidationError(
                f"Effect {effect.value!r} is not defined in the effects library.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={
                    "effect": effect.value,
                    "available": sorted(item.value for item in self._config.library),
                },
                recoverable=False,
            )
        return EffectDefinition(effect=effect, spec=spec)

    def available(self, style: str = "default") -> list[EffectDefinition]:
        """Effects enabled in the library and not suppressed by the style."""
        profile = self._config.profile(style)
        suppressed = set(profile.suppress)
        return [
            EffectDefinition(effect=effect, spec=spec)
            for effect, spec in self._config.library.items()
            if spec.enabled and effect not in suppressed
        ]

    def candidates_for(
        self,
        *,
        moment_type: MomentType,
        events: list[GameEventType],
        score: float,
        style: str = "default",
    ) -> list[EffectDefinition]:
        """Effects appropriate for one moment, excluding structural ones."""
        return [
            definition
            for definition in self.available(style)
            if not definition.is_structural
            and definition.matches(moment_type=moment_type, events=events, score=score)
        ]

    def profile(self, style: str) -> EffectStyleProfile:
        return self._config.profile(style)

    def intensity_for(self, level: EffectsLevel, style: str = "default") -> float:
        """Combine the intent's dial with the style's own intensity.

        The intent wins when it asks for less: a user who said "fewer effects"
        must not be overridden by a style that likes them.
        """
        requested = INTENSITY_BY_LEVEL[level]
        style_intensity = self._config.profile(style).intensity
        return min(requested, style_intensity) if requested < 1.0 else style_intensity

    def priority_multiplier(self, effect: EffectType, style: str) -> float:
        """Style weighting applied when ranking candidates."""
        profile = self._config.profile(style)
        if effect in profile.suppress:
            return SUPPRESS_MULTIPLIER
        if effect in profile.boost:
            return BOOST_MULTIPLIER
        return 1.0

    def engines_used(self, style: str = "default") -> set[EffectEngine]:
        """Which renderers a style can call on.

        Lets the render stage skip the Remotion pass entirely when no enabled
        effect needs it.
        """
        return {definition.engine for definition in self.available(style)}

    def params_for(self, effect: EffectType, strength: float) -> dict[str, object]:
        """Scale an effect's parameters by ``strength``.

        Only magnitudes scale. Durations, positions, colours and mode flags are
        left alone: a weaker zoom is a smaller zoom, not a shorter one.
        """
        definition = self.definition(effect)
        scalable = {
            "scale": 1.0,
            "amplitude_px": 0.0,
            "strength": 0.0,
            "peak_opacity": 0.0,
            "gain_db": None,
        }
        params: dict[str, object] = {}
        for key, value in definition.spec.params.items():
            if key in scalable and isinstance(value, (int, float)):
                if key == "scale":
                    # A scale of 1.0 is "no zoom"; interpolate from there.
                    params[key] = 1.0 + (float(value) - 1.0) * strength
                elif key == "gain_db":
                    # Decibels are already logarithmic: reduce, never amplify.
                    params[key] = float(value) - (1.0 - strength) * 6.0
                else:
                    params[key] = float(value) * strength
            else:
                params[key] = value
        return params


__all__ = [
    "BOOST_MULTIPLIER",
    "INTENSITY_BY_LEVEL",
    "SUPPRESS_MULTIPLIER",
    "EffectDefinition",
    "EffectLibrary",
]
