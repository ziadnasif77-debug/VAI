"""Reading the sentence library, and refusing an incoherent one.

Every check here happens at load time rather than at planning time, so a
composition that could never ship whole is a configuration error the user sees
once -- not a silent rejection they would have to infer from a missing effect
in a finished video.
"""

from __future__ import annotations

from typing import Any

from backend.core.errors import ConfigurationError, ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import EffectType
from backend.emphasis.models import ROLES, Composition, CompositionMember
from backend.semantic.reader import LEVELS

logger = get_logger("emphasis.grammar", LogChannel.PIPELINE)


def load_library(config: Any, *, realisable: frozenset[str] | None = None) -> list[Composition]:
    """The compositions this build can actually speak.

    ``realisable`` is the set of effect names something can draw. A sentence
    naming an effect with no renderer would ship as a hole in the middle of a
    gesture -- three of the shipped library's effects had no builder at all
    when this was written, which is exactly the shape of that failure.
    """
    section = getattr(config, "compositions", None)
    if section is None or not section.enabled:
        return []
    library: list[Composition] = []
    for name, spec in section.library.items():
        members = tuple(_member(name, entry) for entry in spec.members)
        _coherent(name, members)
        if realisable is not None:
            missing = sorted(
                {member.effect.value for member in members} - realisable
            )
            if missing:
                logger.warning(
                    "A composition names effects this build cannot draw; skipping it",
                    extra={"composition": name, "missing": missing},
                )
                continue
        library.append(
            Composition(
                id=name,
                members=members,
                requires_level=tuple(spec.requires_level),
                requires_kind=tuple(spec.requires_kind),
                min_strength=float(spec.min_strength),
                cooldown_seconds=float(spec.cooldown_seconds),
                cluster_cost=int(spec.cluster_cost or section.default_cluster_cost),
            )
        )
    logger.info(
        "Loaded the composition library",
        extra={"compositions": [item.id for item in library]},
    )
    return library


def _member(composition: str, entry: Any) -> CompositionMember:
    return CompositionMember(
        role=entry.role,
        effect=EffectType(entry.effect),
        offset_seconds=float(entry.offset),
        duration_seconds=float(entry.duration),
        strength=float(entry.strength),
        depends_on=tuple(entry.depends_on),
    )


def _coherent(name: str, members: tuple[CompositionMember, ...]) -> None:
    """Refuse a sentence that could never be spoken whole.

    Three ways a library entry can be incoherent, each of which would show up
    in a finished video rather than in a log: a member depending on a role
    that is not in the sentence, a dependency cycle, and a member depending on
    something that happens after it.
    """
    if not members:
        raise ConfigurationError(
            f"composition {name!r} has no members",
            code=ErrorCode.CONFIG_INVALID,
        )
    present = {member.role for member in members}
    for member in members:
        unknown = sorted(set(member.depends_on) - present)
        if unknown:
            raise ConfigurationError(
                f"composition {name!r}: {member.role} depends on {unknown}, "
                "which the composition does not contain",
                code=ErrorCode.CONFIG_INVALID,
                details={"composition": name, "missing_roles": unknown},
            )
        if member.role in member.depends_on:
            raise ConfigurationError(
                f"composition {name!r}: {member.role} depends on itself",
                code=ErrorCode.CONFIG_INVALID,
            )
    earliest = {member.role: member.offset_seconds for member in members}
    for member in members:
        for required in member.depends_on:
            if earliest[required] > member.offset_seconds:
                raise ConfigurationError(
                    f"composition {name!r}: {member.role} at {member.offset_seconds:+.2f}s "
                    f"depends on {required} at {earliest[required]:+.2f}s, which has "
                    "not happened yet",
                    code=ErrorCode.CONFIG_INVALID,
                )
    for member in members:
        if member.role not in ROLES:
            raise ConfigurationError(
                f"composition {name!r}: {member.role!r} is not one of {list(ROLES)}",
                code=ErrorCode.CONFIG_INVALID,
            )


def validate_levels(levels: tuple[str, ...], *, composition: str) -> None:
    """A level the grader never produces would silence a composition forever."""
    unknown = sorted(set(levels) - set(LEVELS))
    if unknown:
        raise ConfigurationError(
            f"composition {composition!r} requires levels {unknown}, which do not exist",
            code=ErrorCode.CONFIG_INVALID,
        )


__all__ = ["load_library", "validate_levels"]
