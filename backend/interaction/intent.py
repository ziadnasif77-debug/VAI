"""Editing intent resolution (§2, §3, §4, §11).

The pipeline never reads a user's sentence. It reads an :class:`EditingIntent`
produced here from three ordered inputs::

    preset  ->  project settings  ->  accumulated instruction deltas

Accumulation is the point. "Focus on clutches", then "fewer effects", then
"make it 25 minutes" must compose, not overwrite each other, and the log of
deltas is what makes that true and auditable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from backend.config.schema import AppConfig, PresetConfig
from backend.core.errors import ErrorCode, ValidationError
from backend.core.models.enums import MomentType
from backend.interaction.models import (
    CaptionPolicy,
    DeadTimePolicy,
    EditingIntent,
    EffectsLevel,
    IntentDelta,
    IntentUpdate,
    Level,
    ListDelta,
    MusicPolicy,
    Pacing,
)

#: Preset keys mapped to their enum, so an invalid preset value fails loudly at
#: resolution rather than silently falling back to a default.
_ENUM_FIELDS: dict[str, type] = {
    "pacing": Pacing,
    "dead_time_policy": DeadTimePolicy,
    "context_preservation": Level,
    "effects": EffectsLevel,
    "captions": CaptionPolicy,
    "music": MusicPolicy,
    "variety": Level,
}

#: Preset fields copied straight through.
_PASSTHROUGH_FIELDS = ("mode", "style")

#: Fields a preset may not define -- they belong to the project, not the style.
_PRESET_RESERVED = frozenset({"label", "description", "target_duration_seconds"})


class IntentResolver:
    """Builds the effective editing intent for a project."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    # -- presets --------------------------------------------------------

    def available_presets(self) -> dict[str, PresetConfig]:
        return dict(self._config.presets)

    def preset(self, name: str) -> PresetConfig:
        """Return a preset by name.

        Raises:
            ValidationError: when the name is not configured.
        """
        try:
            return self._config.presets[name]
        except KeyError as exc:
            raise ValidationError(
                f"Unknown editing preset {name!r}.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"preset": name, "available": sorted(self._config.presets)},
                recoverable=False,
            ) from exc

    def base_intent(self, preset_name: str | None = None) -> EditingIntent:
        """Return the intent a preset describes, before any instruction."""
        name = preset_name or self._config.interaction.default_preset
        preset = self.preset(name)
        payload = preset.model_dump()

        values: dict[str, Any] = {"preset": name}
        custom: dict[str, Any] = {}

        for key, value in payload.items():
            if key in _PRESET_RESERVED or value is None:
                continue
            if key in _ENUM_FIELDS:
                values[key] = _coerce_enum(key, value, _ENUM_FIELDS[key], name)
            elif key in _PASSTHROUGH_FIELDS:
                values[key] = value
            elif key in {"priority_moment_types", "avoid_moment_types", "priority_events"}:
                if value:
                    values[key] = value
            else:
                # An unrecognised preset key is a forward-compatible extension,
                # not an error (§3 "dynamic and extensible").
                custom[key] = value

        if custom:
            values["custom"] = custom
        return EditingIntent(**values)

    # -- resolution -----------------------------------------------------

    def resolve(
        self,
        *,
        preset_name: str | None = None,
        updates: Sequence[IntentUpdate] = (),
        project_target_duration_seconds: int | None = None,
        project_mode: Any = None,
    ) -> EditingIntent:
        """Compose the effective intent.

        Args:
            preset_name: preset to start from; the configured default if absent.
            updates: instruction deltas, applied in ``sequence`` order.
            project_target_duration_seconds: the project's own target. It wins
                over the preset, and over any instruction only when
                ``intent.project_settings_override_preset`` is set and no
                instruction has explicitly changed the duration.
            project_mode: the project's video mode, treated the same way.
        """
        intent = self.base_intent(preset_name)

        ordered = sorted(updates, key=lambda item: item.sequence)
        duration_set_by_instruction = any(
            update.delta.target_duration_seconds is not None for update in ordered
        )
        mode_set_by_instruction = any(update.delta.mode is not None for update in ordered)

        settings_win = self._config.interaction.intent.project_settings_override_preset
        if settings_win and project_mode is not None and not mode_set_by_instruction:
            intent = intent.model_copy(update={"mode": project_mode})
        if (
            settings_win
            and project_target_duration_seconds is not None
            and not duration_set_by_instruction
        ):
            intent = intent.model_copy(
                update={"target_duration_seconds": project_target_duration_seconds}
            )

        for update in ordered:
            intent = self.apply(intent, update.delta)
        return intent

    def apply(self, intent: EditingIntent, delta: IntentDelta) -> EditingIntent:
        """Return ``intent`` with ``delta`` applied."""
        if delta.is_empty():
            return intent

        changes: dict[str, Any] = delta.model_dump(
            exclude={
                "priority_moment_types",
                "priority_events",
                "avoid_moment_types",
                "custom",
            },
            exclude_none=True,
        )

        if delta.priority_moment_types is not None:
            changes["priority_moment_types"] = delta.priority_moment_types.apply(
                [item.value for item in intent.priority_moment_types]
            )
        if delta.avoid_moment_types is not None:
            changes["avoid_moment_types"] = delta.avoid_moment_types.apply(
                [item.value for item in intent.avoid_moment_types]
            )
        if delta.priority_events is not None:
            changes["priority_events"] = delta.priority_events.apply(
                [item.value for item in intent.priority_events]
            )
        if delta.custom:
            changes["custom"] = {**intent.custom, **delta.custom}

        merged = {**intent.model_dump(), **changes}
        # Adding to one list may collide with the other; the newest instruction
        # wins, so "actually, keep the fails" does what the user expects. This
        # runs before validation because the intent model rejects a type that is
        # both prioritised and avoided -- the collision is transient, not a state
        # the intent is ever allowed to hold.
        merged = _resolve_priority_conflicts(merged, delta)

        # Re-validate rather than model_copy: a list delta yields plain strings,
        # and model_copy would store them unvalidated, leaving the intent holding
        # str where the pipeline expects MomentType.
        return EditingIntent.model_validate(merged)

    def describe(self, intent: EditingIntent) -> str:
        """A one-line human summary, for the chat reply and the UI."""
        parts = [f"pacing {intent.pacing.value}", f"effects {intent.effects.value}"]
        if intent.target_duration_seconds:
            minutes = intent.target_duration_seconds / 60
            parts.insert(0, f"target {minutes:g} min")
        if intent.priority_moment_types:
            parts.append(
                "prioritising " + ", ".join(item.value for item in intent.priority_moment_types)
            )
        if intent.avoid_moment_types:
            parts.append(
                "avoiding " + ", ".join(item.value for item in intent.avoid_moment_types)
            )
        parts.append(f"dead time {intent.dead_time_policy.value}")
        return "; ".join(parts)


def _resolve_priority_conflicts(
    merged: dict[str, Any], delta: IntentDelta
) -> dict[str, Any]:
    """Drop a type from whichever list the newest delta did *not* touch."""
    priorities = [_name(item) for item in merged.get("priority_moment_types", [])]
    avoided = [_name(item) for item in merged.get("avoid_moment_types", [])]
    overlap = set(priorities) & set(avoided)
    if not overlap:
        return merged

    newly_prioritised = set(_added(delta.priority_moment_types))
    newly_avoided = set(_added(delta.avoid_moment_types))

    kept_priorities = [
        item for item in priorities if item not in overlap or item in newly_prioritised
    ]
    kept_avoided = [
        item for item in avoided if item not in overlap or item in newly_avoided
    ]
    # If a type ends up in both new sets, prioritising wins: the user asked for
    # it more recently in the same breath.
    kept_avoided = [item for item in kept_avoided if item not in set(kept_priorities)]

    return {
        **merged,
        "priority_moment_types": kept_priorities,
        "avoid_moment_types": kept_avoided,
    }


def _name(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _added(delta: ListDelta | None) -> list[str]:
    if delta is None:
        return []
    return [*delta.add, *(delta.set or [])]


def _coerce_enum(field: str, value: Any, enum_type: type, preset_name: str) -> Any:
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValidationError(
            f"Preset {preset_name!r} sets {field}={value!r}, which is not one of: {allowed}.",
            code=ErrorCode.CONFIG_INVALID,
            details={"preset": preset_name, "field": field, "value": value},
            recoverable=False,
        ) from exc


def moment_types(names: Sequence[str]) -> list[MomentType]:
    """Convert names to :class:`MomentType`, ignoring unknown ones."""
    known = {member.value for member in MomentType}
    return [MomentType(name) for name in names if name in known]


__all__ = ["IntentResolver", "moment_types"]
