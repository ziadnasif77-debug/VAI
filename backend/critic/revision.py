"""Acting on a critique, within the rules the edit already has (Phase E).

A note is an opinion until something applies it, and applying it is where the
constraints live. Three of them, in order of authority:

1. **§42's operations are the only vocabulary.** A note becomes a `trim` or a
   `set_enabled`, both of which validate their own arguments and refuse to
   invent footage. There is no path from a model's answer to an arbitrary
   change of the timeline.
2. **§39 has a veto.** The length was the one hard constraint at selection time
   and it still is. Trims and drops are applied while the edit stays inside its
   tolerance, and the moment the next change would take it out, that change is
   refused and recorded. A video of the wrong length is a worse defect than the
   one being fixed.
3. **§78 gives the person the last word.** Every applied change and every
   refused one comes back as a note, so "the Critic shortened clip 3 by two
   seconds" appears in the render record rather than being something the user
   discovers by watching.

Refusals are ordered, not random: the cheapest changes are applied first, so a
budget spent on one large drop cannot swallow six small trims that would each
have improved the video.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.core.duration import DurationPolicy, format_duration
from backend.core.errors import ValidationError
from backend.core.logging import LogChannel, get_logger
from backend.critic.evidence import EditEvidence
from backend.critic.models import Action, Critique, Note
from backend.timeline import operations
from backend.timeline.models import Timeline

logger = get_logger("critic.revision", LogChannel.PIPELINE)

#: How much of an *already too short* edit the review may still take out.
#:
#: The duration floor protects a video that is currently the right length from
#: being cut out of it. It cannot protect one that never reached it -- and on
#: those, applying the floor literally means the Critic can do nothing at all,
#: which is backwards: a forty-second edit built from a forty-second recording
#: is exactly where a loading screen at the front is most worth removing. So
#: below the floor the limit becomes proportional. The video is already short
#: and honestly reported as such (§39); the review may make it a little
#: shorter and better, not a lot shorter.
MAX_SHORTFALL_REMOVAL: float = 0.15


@dataclass(frozen=True, slots=True)
class Revision:
    """What the critique changed, and what it was not allowed to change."""

    timeline: Timeline
    applied: tuple[str, ...] = field(default_factory=tuple)
    refused: tuple[str, ...] = field(default_factory=tuple)
    seconds_removed: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def summary(self) -> dict[str, Any]:
        return {
            "applied": len(self.applied),
            "refused": len(self.refused),
            "seconds_removed": round(self.seconds_removed, 2),
            "notes": [*self.applied, *self.refused],
        }


def apply(
    timeline: Timeline,
    critique: Critique,
    evidence: EditEvidence,
    *,
    policy: DurationPolicy,
    target_seconds: float,
    allow_drops: bool = True,
) -> Revision:
    """Apply what the critique asked for, as far as the duration allows.

    Args:
        evidence: the rows the Critic was shown. A note's ``clip`` indexes
            this, and this is the only thing it indexes -- resolving it against
            the timeline's own clip order would silently address a different
            clip whenever a disabled one sits between them.
        allow_drops: whether ``drop`` is honoured at all. Off is a defensible
            setting: trimming a clip's dead opening is nearly always right,
            while removing a clip the optimiser chose is a bigger claim.
    """
    if critique.is_empty:
        return Revision(timeline=timeline)

    current = evidence.total_seconds
    floor = _floor(current, policy=policy, target_seconds=target_seconds)
    applied: list[str] = []
    refused: list[str] = []

    for note in _cheapest_first(critique.actionable, evidence):
        row = evidence.clips[note.clip] if note.clip < len(evidence.clips) else None
        if row is None:
            continue
        cost = _cost(note, row.seconds)
        if not allow_drops and note.action is Action.DROP:
            refused.append(f"clip {note.clip}: {_describe(note)} -- drops are switched off")
            continue
        if current - cost < floor:
            refused.append(
                f"clip {note.clip}: {_describe(note)} -- it would take the edit under "
                f"{format_duration(floor)}"
            )
            continue

        try:
            timeline = _operation(timeline, note, row.clip.id)
        except ValidationError as error:
            # §42 refusing a trim is the timeline saying the change does not
            # fit its own source. That is a fact about the footage, not a bug,
            # and it is reported rather than swallowed.
            refused.append(f"clip {note.clip}: {_describe(note)} -- {error}")
            continue

        current -= cost
        applied.append(f"clip {note.clip}: {_describe(note)}{_because(note)}")

    revision = Revision(
        timeline=timeline,
        applied=tuple(applied),
        refused=tuple(refused),
        seconds_removed=max(0.0, evidence.total_seconds - current),
    )
    if revision.changed:
        logger.info("The Critic revised the edit", extra=revision.summary())
    return revision


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _floor(current: float, *, policy: DurationPolicy, target_seconds: float) -> float:
    """The shortest this review is allowed to leave the edit.

    Two regimes, because there are two different things to protect. An edit
    that currently lands inside §39's tolerance must not be cut out of it --
    that is the whole point of the constraint. An edit that never got there
    cannot be protected by it, so what is protected instead is the proportion:
    the review may improve a short video, not shrink it.
    """
    tolerated = target_seconds - policy.tolerance_for(target_seconds)
    if current >= tolerated:
        return tolerated
    return current * (1.0 - MAX_SHORTFALL_REMOVAL)


def _cheapest_first(notes: Sequence[Note], evidence: EditEvidence) -> list[Note]:
    """Small changes before large ones.

    Applied in the model's own order, one `drop` early in the list can consume
    the whole duration budget and refuse five trims behind it -- each of which
    would have cost two seconds and improved a clip. Sorting by cost is the
    difference between spending the budget and being spent by it.
    """
    return sorted(
        notes,
        key=lambda note: (
            _cost(note, evidence.clips[note.clip].seconds)
            if note.clip < len(evidence.clips)
            else 0.0,
            note.clip,
        ),
    )


def _cost(note: Note, clip_seconds: float) -> float:
    """Seconds this note takes out of the edit."""
    if note.action is Action.DROP:
        return clip_seconds
    if note.action in {Action.TRIM_START, Action.TRIM_END}:
        return note.seconds
    return 0.0


def _operation(timeline: Timeline, note: Note, clip_id: str) -> Timeline:
    if note.action is Action.DROP:
        return operations.delete(timeline, clip_id)
    if note.action is Action.TRIM_START:
        return operations.trim(timeline, clip_id, start_delta=note.seconds)
    if note.action is Action.TRIM_END:
        return operations.trim(timeline, clip_id, end_delta=-note.seconds)
    return timeline


def _describe(note: Note) -> str:
    if note.action is Action.DROP:
        return "dropped"
    if note.action is Action.TRIM_START:
        return f"trimmed {note.seconds:.1f}s off the start"
    if note.action is Action.TRIM_END:
        return f"trimmed {note.seconds:.1f}s off the end"
    return "kept"


def _because(note: Note) -> str:
    return f" ({note.reason})" if note.reason else ""


__all__ = ["Revision", "apply"]
