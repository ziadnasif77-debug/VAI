"""Choosing which sentence is spoken over which beat.

The flat planner scores every candidate effect independently and admits them
best-first under a budget. That is the right algorithm for decorations and the
wrong one for sentences: judged member by member, a composition survives
partially, and half a build-up rendered without its payoff is worse than no
build-up at all -- it is noise wearing the shape of intent.

So admission here is atomic, the budget counts a sentence as one gesture, and
every decision -- taken or refused -- carries the reason it was made. The
doctrine's own decision filter is the last gate: *if none apply, the edit does
not happen.*

Nothing in this module reads YAML conditionals or writes them. The library is
data; the choosing is here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import EffectType, GameEventType
from backend.emphasis.models import (
    Anchor,
    Composition,
    CompositionMember,
    PlannedComposition,
)
from backend.semantic.reader import SemanticReader

logger = get_logger("emphasis.engine", LogChannel.PIPELINE)

#: A beat this close to a stronger one is the same beat reported twice.
ANCHOR_MERGE_SECONDS: Final[float] = 1.5

#: Repetition is a defect the doctrine names. The same sentence twice in a row,
#: however far apart, reads as a template -- so a second identical composition
#: needs a clearly stronger beat than the one before it.
REPEAT_STRENGTH_STEP: Final[float] = 0.15


def anchors_from(
    *,
    media_id: str,
    events: Sequence[tuple[float, float, str, float]],
    phases: Sequence[tuple[str, float, float, float, str]] = (),
    reader: SemanticReader | None = None,
) -> list[Anchor]:
    """Beats worth building around, strongest first at each instant.

    Two sources, deliberately: named game events, which say *what* happened,
    and the payoff phases P2 measures, which say *where the moment lands* even
    when nothing could be named -- and on real footage most events cannot be
    named at all.

    Args:
        events: ``(start, end, event_type, strength)`` for strong events only.
        phases: ``(moment_id, start, end, confidence, name)``.
    """
    found: list[Anchor] = []
    for start, _end, kind, strength in events:
        found.append(
            Anchor(
                id=f"event@{start:.2f}",
                media_id=media_id,
                seconds=float(start),
                kind=str(kind),
                strength=float(strength),
                level=reader.level_for(start, start + 1.0) if reader else "normal",
            )
        )
    for moment_id, start, _end, confidence, name in phases:
        if name != "payoff":
            continue
        found.append(
            Anchor(
                id=f"payoff@{start:.2f}",
                media_id=media_id,
                seconds=float(start),
                kind="payoff",
                strength=float(confidence),
                level=reader.level_for(start, start + 1.0) if reader else "normal",
                moment_id=moment_id,
            )
        )
    return _merged(found)


def _merged(anchors: list[Anchor]) -> list[Anchor]:
    """One beat per instant: the strongest wins, the rest are the same beat."""
    kept: list[Anchor] = []
    for anchor in sorted(anchors, key=lambda item: (-item.strength, item.seconds)):
        if any(
            abs(anchor.seconds - other.seconds) < ANCHOR_MERGE_SECONDS for other in kept
        ):
            continue
        kept.append(anchor)
    return sorted(kept, key=lambda item: item.seconds)


def compose(
    anchors: Sequence[Anchor],
    library: Sequence[Composition],
    *,
    budget: int,
    min_gap_seconds: float,
    duration_seconds: float = float("inf"),
    within: Sequence[tuple[float, float]] = (),
) -> tuple[list[PlannedComposition], list[str]]:
    """Speak as many sentences as the beats and the budget allow.

    Strongest beat first, because a budget spent on a weak beat is a budget
    the session's own summit no longer has. Returns what was spoken and, in
    the same breath, why everything else was not -- silently discarding half
    the candidates is what makes an effects engine impossible to reason about.
    """
    spoken: list[PlannedComposition] = []
    refused: list[str] = []
    spent = 0
    last_spoken: dict[str, tuple[float, float]] = {}

    for anchor in sorted(anchors, key=lambda item: (-item.strength, item.seconds)):
        if within and not any(start <= anchor.seconds <= end for start, end in within):
            continue
        choice = _for_anchor(anchor, library, refused=refused)
        if choice is None:
            continue
        if spent + choice.cluster_cost > budget:
            refused.append(
                f"{choice.id} at {anchor.seconds:.1f}s: the effects budget is spent"
            )
            continue
        previous = last_spoken.get(choice.id)
        if previous is not None:
            when, strength = previous
            # Absolute distance: beats are considered strongest-first, so
            # the one already spoken may well be LATER in the video, and a
            # signed difference made the cooldown fire on a sentence a hundred
            # seconds earlier with "spoken -170s ago".
            apart = abs(anchor.seconds - when)
            if apart < choice.cooldown_seconds:
                refused.append(
                    f"{choice.id} at {anchor.seconds:.1f}s: spoken {apart:.0f}s away"
                )
                continue
            if anchor.strength < strength + REPEAT_STRENGTH_STEP:
                # The same sentence again over a beat no stronger than the
                # last one is a template, not an emphasis.
                refused.append(
                    f"{choice.id} at {anchor.seconds:.1f}s: no stronger than its last outing"
                )
                continue
        placements = _placements(choice, anchor)
        start = min(seconds for _member, seconds in placements)
        end = max(seconds + member.duration_seconds for member, seconds in placements)
        if any(
            start < other.end_seconds + min_gap_seconds
            and other.start_seconds - min_gap_seconds < end
            for other in spoken
        ):
            refused.append(
                f"{choice.id} at {anchor.seconds:.1f}s: another composition is speaking"
            )
            continue
        if start < 0.0 or end > duration_seconds:
            # A sentence is spoken over the VIDEO, not inside one shot. The
            # first version required it to fit in its clip and spoke nothing
            # at all: P3 cuts a climax to 1.8 seconds, and a build-up that
            # begins 1.6 seconds before the beat cannot live there. Each
            # member is bound to whichever shot contains it instead, so the
            # gesture crosses cuts the way an editor's would.
            refused.append(
                f"{choice.id} at {anchor.seconds:.1f}s: it runs past the edit"
            )
            continue

        spoken.append(
            PlannedComposition(
                composition_id=choice.id,
                anchor=anchor,
                placements=placements,
                reason=(
                    f"{choice.id} over a {anchor.level} {anchor.kind} "
                    f"at {anchor.seconds:.1f}s (strength {anchor.strength:.2f})"
                ),
            )
        )
        spent += choice.cluster_cost
        last_spoken[choice.id] = (anchor.seconds, anchor.strength)

    spoken.sort(key=lambda item: item.start_seconds)
    logger.info(
        "Composed emphasis",
        extra={"spoken": len(spoken), "refused": len(refused), "spent": spent, "budget": budget},
    )
    return spoken, refused


def _for_anchor(
    anchor: Anchor, library: Sequence[Composition], *, refused: list[str]
) -> Composition | None:
    """The heaviest sentence this beat can carry, or nothing.

    Nothing is a real answer and the common one: the doctrine's filter says an
    edit that answers none of its questions does not happen, and most beats in
    a session are not payoffs.
    """
    eligible = [
        composition
        for composition in library
        if (not composition.requires_level or anchor.level in composition.requires_level)
        and (not composition.requires_kind or anchor.kind in composition.requires_kind)
        and anchor.strength >= composition.min_strength
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item.cluster_cost, len(item.members)))


def _placements(
    composition: Composition, anchor: Anchor
) -> tuple[tuple[CompositionMember, float], ...]:
    """Members at absolute times, in time order, whole or not at all.

    There is no partial return: a member whose dependency is missing takes the
    sentence down with it, which is checked when the library is loaded rather
    than here -- by the time a composition is chosen, it is coherent.
    """
    placed = [
        (member, anchor.seconds + member.offset_seconds)
        for member in composition.members
    ]
    placed.sort(key=lambda item: item[1])
    return tuple(placed)


def as_effects(
    spoken: Sequence[PlannedComposition], *, library_for: Any
) -> list[dict[str, Any]]:
    """Compositions as rows the effects layer already understands.

    Each member becomes one effect carrying its sentence: ``composition_id``
    names the group, ``group_role`` names its part, and ``anchor_seconds``
    records the beat it was placed around -- so a later reader can tell a
    composed impact from a decorative one.
    """
    rows: list[dict[str, Any]] = []
    for planned in spoken:
        for member, seconds in planned.placements:
            rows.append(
                {
                    "effect": member.effect,
                    "start_seconds": seconds,
                    "duration_seconds": member.duration_seconds,
                    "strength": member.strength,
                    "composition_id": planned.composition_id,
                    "group_role": member.role,
                    "anchor_seconds": planned.anchor.seconds,
                    "offset_seconds": member.offset_seconds,
                    "moment_id": planned.anchor.moment_id,
                    "clip_id": planned.anchor.clip_id,
                    "reason": planned.reason,
                }
            )
    rows.sort(key=lambda row: row["start_seconds"])
    return rows


def strong_event_types() -> frozenset[str]:
    """Event types an anchor may be built on.

    Everything except the one that means "we could not name this". A beat we
    cannot name is not a beat to build a sentence around.
    """
    return frozenset(
        kind.value for kind in GameEventType if kind is not GameEventType.UNKNOWN_EVENT
    )


__all__ = [
    "ANCHOR_MERGE_SECONDS",
    "EffectType",
    "anchors_from",
    "as_effects",
    "compose",
    "strong_event_types",
]
