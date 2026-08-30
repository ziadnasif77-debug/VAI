"""The vocabulary a composition needs and the flat planner never had.

``docs/DIRECTION.md`` states the grammar as doctrine:

    SETUP -> BUILDUP -> TENSION -> PAYOFF -> REACTION -> NEXT MOMENT

never

    MOMENT -> RANDOM EFFECT -> MOMENT -> RANDOM EFFECT

What shipped was the second shape with budgets, cooldowns and an escalation
dial bolted on, and the reason was not effort -- it was vocabulary. An
``EffectInstance`` has no group, no role and no dependency; every instance
carries an absolute start and knows nothing of any other. Worse, the planner
was handed moments carrying their event *types* and not their event *times*,
so there was no beat in the data to build around even if the types had
existed.

These are the missing nouns. An :class:`Anchor` is a beat with a real
timestamp. A :class:`Composition` is a named sentence of effects placed at
signed offsets around one, with dependencies between its parts -- so that a
composition ships whole or not at all, which is the difference between a
sentence and a pile of words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from backend.core.models.enums import EffectType
from backend.semantic.reader import Level

#: What a member is doing in the sentence. The order here is the order in
#: time: a member's role is also its place.
Role = Literal["setup", "buildup", "tension", "payoff", "reaction"]
ROLES: Final[tuple[Role, ...]] = ("setup", "buildup", "tension", "payoff", "reaction")


@dataclass(frozen=True, slots=True)
class Anchor:
    """A beat worth building around.

    ``seconds`` is an absolute source time, which is the whole point: the
    planner placed effects at a fixed fraction of a moment's length because a
    fraction was all it had.
    """

    id: str
    media_id: str
    seconds: float
    #: A game event type, or ``payoff`` when the beat comes from a moment's
    #: own measured shape rather than from a named event.
    kind: str
    strength: float
    level: Level
    moment_id: str | None = None
    clip_id: str | None = None
    #: The shot this beat sits in. A sentence whose build-up would start
    #: before its shot begins is not spoken: effects are stored relative to
    #: their clip, so a negative offset is not a placement, it is a defect.
    clip_start: float = 0.0
    clip_end: float = float("inf")


@dataclass(frozen=True, slots=True)
class CompositionMember:
    """One effect in a sentence, placed relative to the beat."""

    role: Role
    effect: EffectType
    #: Signed, in seconds. Negative is before the beat -- which is what a
    #: build-up is, and what no absolute-start instance could express.
    offset_seconds: float
    duration_seconds: float = 0.0
    strength: float = 1.0
    #: Roles this member needs. If one of them did not survive admission,
    #: neither does this -- and a composition that loses a member its parts
    #: depend on is refused whole.
    depends_on: tuple[Role, ...] = ()


@dataclass(frozen=True, slots=True)
class Composition:
    """A named sentence of effects, and the beats it may be spoken over."""

    id: str
    members: tuple[CompositionMember, ...]
    #: Semantic levels this sentence belongs at. A heavy payoff composition
    #: over a calm stretch is the noise the doctrine's decision filter exists
    #: to refuse.
    requires_level: tuple[Level, ...] = ()
    #: Anchor kinds it may be spoken over; empty means any.
    requires_kind: tuple[str, ...] = ()
    #: Minimum anchor strength.
    min_strength: float = 0.0
    #: How long before the same sentence may be spoken again.
    cooldown_seconds: float = 60.0
    #: What the whole sentence costs against the effects budget. A
    #: composition is one gesture, not five -- charging it per member would
    #: let the existing per-minute cap forbid every composition there is.
    cluster_cost: int = 2

    @property
    def span(self) -> tuple[float, float]:
        """Earliest and latest offset, so a caller can reserve the ground."""
        offsets = [member.offset_seconds for member in self.members]
        ends = [
            member.offset_seconds + member.duration_seconds for member in self.members
        ]
        return (min(offsets, default=0.0), max(ends, default=0.0))


@dataclass(frozen=True, slots=True)
class PlannedComposition:
    """One sentence, admitted, with the ground it occupies."""

    composition_id: str
    anchor: Anchor
    #: ``(member, absolute_seconds)`` in time order.
    placements: tuple[tuple[CompositionMember, float], ...] = field(default=())
    reason: str = ""

    @property
    def start_seconds(self) -> float:
        return min(seconds for _member, seconds in self.placements)

    @property
    def end_seconds(self) -> float:
        return max(
            seconds + member.duration_seconds for member, seconds in self.placements
        )


__all__ = [
    "ROLES",
    "Anchor",
    "Composition",
    "CompositionMember",
    "PlannedComposition",
    "Role",
]
