"""How much of what happened the pipeline could actually name (Phase 0.5).

Every number here answers one question: **did perception get better, or did
the output just change?** Before these existed the honest answer was
unavailable, and the cost of that showed twice in one week.

The first time, a summary reported 61% of events unnamed and the improvement
work was aimed at the naming rules. The second time — after the vision labels
were wired through — the ratio looked like it had *worsened*, from 0.426 to
0.447, when what had really happened was that nineteen false ``defeat`` events
had been removed from the named side. A quest tracker reading *"Defeat the
O.R.C. guards at the Milk Molar stash"* had been the most common named event
in the whole project.

So the ratio alone is not enough, and neither is any single number. What is
reported is a small set that cannot all move the wrong way at once:

``unknown_event_ratio``
    The headline. What fraction of what happened the pipeline could not name.

``naming_source_coverage``
    How many events had *any* source capable of naming them. An audio spike
    beside a shot change describes an instant nobody looked at, and no rule
    should ever name it. This separates "we cannot name it" from "nothing was
    looking".

``vision_frames_per_source_minute``
    How often anything looked at the screen at all. Measured at 4.4 on a
    77-minute recording — one frame per 13.6 seconds, against collisions and
    kills that last two. It is the ceiling every naming rule works under.

Everything is computed from data already in hand, so a caller pays nothing to
ask, and every value is a plain float a later run can be compared against.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.gaming.correlation import GENERIC_TYPES, GameEvent
from backend.gaming.events import AUDIO, SCENE, EventObservation

#: Sources that can only ever report *that* something happened. A cluster
#: holding nothing else had nobody looking at the screen, and an event it
#: yields is unnamed for a reason no rule can fix.
_UNNAMEABLE_SOURCES: frozenset[str] = frozenset({AUDIO, SCENE})


def report(
    events: Sequence[GameEvent],
    observations: Sequence[EventObservation] = (),
    *,
    duration_seconds: float = 0.0,
    vision_observations: int = 0,
    candidate_frames: int = 0,
) -> dict[str, float | int]:
    """The perception metrics for one recording.

    Args:
        events: what correlation produced.
        observations: what the detectors reported, for the coverage figure.
        duration_seconds: the *source* length, not the edit's. Density is a
            property of the recording that was analysed.
    """
    total = len(events)
    named = sum(1 for event in events if event.is_named)
    fused = sum(
        1
        for event in events
        if str(event.metadata.get("named_by", "")).startswith("fusion:")
    )
    minutes = duration_seconds / 60.0 if duration_seconds > 0 else 0.0

    metrics: dict[str, float | int] = {
        "events": total,
        "named_events": named,
        "fused_events": fused,
        "named_event_ratio": _ratio(named, total),
        "unknown_event_ratio": _ratio(total - named, total),
        "naming_source_coverage": _coverage(events),
        "multi_source_ratio": _ratio(
            sum(1 for event in events if event.agreement > 1), total
        ),
    }
    if minutes > 0:
        metrics["events_per_source_minute"] = round(total / minutes, 2)
        metrics["vision_frames_per_source_minute"] = round(vision_observations / minutes, 2)
        if candidate_frames:
            metrics["candidate_frames_per_source_minute"] = round(
                candidate_frames / minutes, 2
            )
    if observations:
        metrics["observations"] = len(observations)
        metrics["context_observations"] = sum(
            1 for item in observations if item.context_only
        )
    return metrics


def _coverage(events: Sequence[GameEvent]) -> float:
    """Fraction of events that had a source capable of naming them.

    The number that separates a naming failure from a looking failure. An
    event built only from an audio transient and a shot change was never
    nameable: nothing read the screen at that instant, and inventing a name
    for it would be the opposite of §23. Raising *this* is what Phase 0.4's
    sampling work is for; raising the ratio above it is what rules are for.
    """
    if not events:
        return 0.0
    covered = sum(
        1
        for event in events
        if set(event.sources) - _UNNAMEABLE_SOURCES
        or event.event_type not in GENERIC_TYPES
    )
    return round(covered / len(events), 4)


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


__all__ = ["report"]
