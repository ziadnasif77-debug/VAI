"""One situation, however many events it was reported as (Phase B).

Phase 0 deferred this and said why: *"Relations between events are worth
building once the events have names."* They have names now — 0.23 to 0.43
unknown across three real recordings, against 0.61 to 0.93 before — so the
question became answerable, and it was answered by measuring rather than by
design.

What the measurement found, across 255 named events on three real recordings:

* consecutive named events are **close** — median gap 12 seconds, and half to
  two-thirds of neighbouring pairs fall within fifteen;
* the commonest neighbour by a wide margin is **the same type again** —
  ``low_health → low_health`` nineteen times, ``combat → combat`` eighteen,
  ``collision → collision`` eighteen;
* and the gap distribution for same-type pairs is **indistinguishable** from
  different-type pairs (median 12.0 against 11.4, first quartile 8.0 for both).

That last one is the load-bearing negative result. Time alone cannot tell
"this fight is still going" from "something else happened nearby", so an
episode is not a time-window merge. **Type identity is the signal**; the window
only stops a run reaching across the whole recording.

So: a run of the same named type, each within :data:`DEFAULT_GAP_SECONDS` of
the last, becomes one episode. Different types stay different events, close
together, and are related rather than merged — because a `combat` and a
`low_health` ten seconds apart are two true things about one situation, and
flattening them would lose which was which.

Nothing here is destructive. The member events are carried on the episode, and
the correlator's output is untouched: this is a reading of it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import GameEventType
from backend.gaming.correlation import GENERIC_TYPES

logger = get_logger("gaming.episodes", LogChannel.PIPELINE)

#: How long a same-type run may pause and still be one situation.
#:
#: Measured rather than chosen. Correlation already merges observations within
#: six seconds, so anything below that is settled before this runs. Above it,
#: the fraction of named events absorbed into a run climbs 23% at ten seconds,
#: 33% at fifteen, 38% at twenty, 44% at thirty — and then flattens, buying
#: three points between thirty and forty-five. Twenty sits at the knee, above
#: the first quartile of observed gaps (8.0 s) and below the point where the
#: rule starts merging things that are merely nearby.
DEFAULT_GAP_SECONDS: float = 20.0

#: How close two *differently* typed events must be to be called related. The
#: same measured distribution, and deliberately the same number: there is no
#: evidence for a second threshold, and inventing one would imply a distinction
#: the data does not show.
DEFAULT_LINK_SECONDS: float = 20.0


@dataclass(frozen=True, slots=True)
class Episode:
    """A run of one named event type, read as the single situation it was."""

    event_type: GameEventType
    media_id: str
    start_seconds: float
    end_seconds: float
    #: The events this was reported as, in time order. Kept whole: an episode
    #: is a reading of the correlator's output, never a replacement for it.
    events: tuple[Any, ...] = ()

    @property
    def duration(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)

    @property
    def parts(self) -> int:
        return len(self.events)

    @property
    def is_merged(self) -> bool:
        """Whether this was reported as more than one event."""
        return self.parts > 1

    @property
    def confidence(self) -> float:
        """The best any single part managed.

        Not an average: three sightings of one fight are not less certain than
        one, and averaging would punish an episode for having been seen twice.
        """
        return max((float(getattr(e, "confidence", 0.0)) for e in self.events), default=0.0)

    @property
    def peak_seconds(self) -> float:
        """When the situation was at its strongest -- the most confident part."""
        best = max(
            self.events,
            key=lambda event: float(getattr(event, "confidence", 0.0)),
            default=None,
        )
        return float(getattr(best, "start_seconds", self.start_seconds))

    @property
    def sources(self) -> tuple[str, ...]:
        found: set[str] = set()
        for event in self.events:
            found.update(str(item) for item in getattr(event, "sources", ()) or ())
        return tuple(sorted(found))

    def summary(self) -> dict[str, Any]:
        return {
            "type": self.event_type.value,
            "start": round(self.start_seconds, 2),
            "seconds": round(self.duration, 2),
            "parts": self.parts,
            "confidence": round(self.confidence, 3),
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class Link:
    """Two episodes of different types that happened together.

    A relation rather than a merge. `combat` and `low_health` ten seconds
    apart are two true statements about one situation, and which was which is
    the part worth keeping -- an editor cutting the moment needs to know the
    player was hurt *during* the fight, not that something combat-shaped and
    health-shaped occurred.
    """

    earlier: Episode
    later: Episode
    gap_seconds: float

    @property
    def overlapping(self) -> bool:
        return self.gap_seconds <= 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "earlier": self.earlier.event_type.value,
            "later": self.later.event_type.value,
            "gap": round(self.gap_seconds, 2),
            "overlapping": self.overlapping,
        }


@dataclass(frozen=True, slots=True)
class Reading:
    """What one recording's named events amount to."""

    episodes: tuple[Episode, ...] = ()
    links: tuple[Link, ...] = ()

    @property
    def merged_events(self) -> int:
        """How many events this reading absorbed into a longer situation."""
        return sum(episode.parts - 1 for episode in self.episodes)

    def summary(self) -> dict[str, Any]:
        return {
            "episodes": len(self.episodes),
            "merged_events": self.merged_events,
            "links": len(self.links),
            "types": sorted({episode.event_type.value for episode in self.episodes}),
        }


def read(
    events: Iterable[Any],
    *,
    media_id: str,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    link_seconds: float = DEFAULT_LINK_SECONDS,
) -> Reading:
    """Group one recording's named events into episodes, and relate the rest.

    Args:
        events: correlated game events for a single recording. Unnamed ones
            are left out entirely -- an `unknown_event` is the correlator
            saying it could not identify this, and a run of things nobody
            identified is not a situation, it is a gap.
        gap_seconds: how long a same-type run may pause and still be one
            situation.
        link_seconds: how close two different types must be to be related.
    """
    named = sorted(
        (event for event in events if _is_named(event)),
        key=lambda event: float(getattr(event, "start_seconds", 0.0)),
    )
    if not named:
        return Reading()

    episodes = _runs(named, media_id=media_id, gap_seconds=gap_seconds)
    reading = Reading(
        episodes=tuple(episodes),
        links=tuple(_links(episodes, link_seconds=link_seconds)),
    )
    if reading.merged_events:
        logger.info("Read events as episodes", extra=reading.summary())
    return reading


def _is_named(event: Any) -> bool:
    """Whether the correlator could put a specific name to this.

    Read off the event type rather than an ``is_named`` attribute, and that is
    a deliberate correction: trusting the attribute meant anything without one
    was silently treated as unnamed and dropped, so a caller holding a
    perfectly good record got an empty reading that looked like quiet footage.
    The type is the fact; the property is a convenience over it.
    """
    kind = getattr(event, "event_type", None)
    return kind is not None and kind not in GENERIC_TYPES


def _runs(named: Sequence[Any], *, media_id: str, gap_seconds: float) -> list[Episode]:
    """Consecutive same-type events, each within ``gap_seconds`` of the last.

    The gap is measured from the previous event's **end**, not its start: a
    forty-second fight followed five seconds later by more of it is one
    situation, and measuring start-to-start would call it a fifty-second pause.
    """
    episodes: list[Episode] = []
    run: list[Any] = [named[0]]
    for event in named[1:]:
        previous = run[-1]
        continues = event.event_type is previous.event_type and (
            float(event.start_seconds) - float(previous.end_seconds) <= gap_seconds
        )
        if continues:
            run.append(event)
            continue
        episodes.append(_episode(run, media_id))
        run = [event]
    episodes.append(_episode(run, media_id))
    return episodes


def _episode(run: Sequence[Any], media_id: str) -> Episode:
    return Episode(
        event_type=run[0].event_type,
        media_id=media_id,
        start_seconds=float(run[0].start_seconds),
        # The last event's end, not the last start: an episode covers the
        # footage its parts cover.
        end_seconds=max(float(event.end_seconds) for event in run),
        events=tuple(run),
    )


def _links(episodes: Sequence[Episode], *, link_seconds: float) -> list[Link]:
    """Neighbouring episodes of different types, close enough to be one moment.

    Only neighbours. Relating every pair within the window would make a busy
    minute a complete graph, which says nothing that "it was busy" does not.
    """
    found: list[Link] = []
    for earlier, later in pairwise(episodes):
        if earlier.event_type is later.event_type:
            continue
        gap = later.start_seconds - earlier.end_seconds
        if gap <= link_seconds:
            found.append(Link(earlier=earlier, later=later, gap_seconds=gap))
    return found


__all__ = [
    "DEFAULT_GAP_SECONDS",
    "DEFAULT_LINK_SECONDS",
    "Episode",
    "Link",
    "Reading",
    "read",
]
