"""What the pipeline recorded about a stretch of a recording (Phase A).

Phase 0's specification is explicit about the shape this must take:

    **No evidence table.** The analysis tables are the evidence; Phase A will
    project over them, not copy them.

So there is no writer here, no migration and no new row. Every analysis stage
already stored what it found, keyed by media and time; this reads across those
stores and answers one question:

    what did anything notice between these two seconds of this recording?

The value is not the data — the data was always there. It is that four
different callers had started answering that question four different ways. The
Critic gathered per clip, the perception report gathered per event, and two
throwaway scripts gathered per instant, each with its own idea of what "near"
means and its own silent failure when an observation could not be attributed
to a recording. One projection, one definition of near, one place to fix.

**Attribution is by construction, not by convention.** Every store here is
keyed by media id, because two recordings of one session both have a second
40, and an observation attributed to the wrong one describes footage it never
saw. The projection takes what the caller fetched per recording rather than a
flat list it would have to guess about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.core.duration import format_duration


class _Timed(Protocol):
    """Anything with a start. Every analysis store produces one of these."""

    @property
    def start_seconds(self) -> float: ...


@dataclass(frozen=True, slots=True)
class Span:
    """A stretch of one recording, and the only thing "near" is measured against."""

    media_id: str
    start_seconds: float
    end_seconds: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)

    def contains(self, seconds: float | None) -> bool:
        return seconds is not None and self.start_seconds <= seconds < self.end_seconds

    def widened(self, seconds: float) -> Span:
        """The same stretch with room either side, never before the recording."""
        return Span(
            media_id=self.media_id,
            start_seconds=max(0.0, self.start_seconds - seconds),
            end_seconds=self.end_seconds + seconds,
        )

    def label(self) -> str:
        return f"[{format_duration(self.start_seconds)}–{format_duration(self.end_seconds)}]"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything anything recorded inside one span."""

    span: Span
    #: Vision observations, in time order.
    seen: tuple[Any, ...] = ()
    #: Transcript segments, in time order.
    said: tuple[Any, ...] = ()
    #: Correlated game events, in time order.
    events: tuple[Any, ...] = ()
    #: Audio events, in time order.
    heard: tuple[Any, ...] = ()
    #: Scene boundaries that fall inside.
    cuts: tuple[Any, ...] = ()
    #: Text read off frames, in time order.
    read: tuple[Any, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether nothing at all was recorded here.

        The distinction this exists for: "nothing happened" and "nobody
        looked" are different statements, and only the second is a reason to
        distrust everything else said about this stretch. Measured on a real
        edit, one clip in eleven -- thirty seconds of a finished video -- came
        back empty.
        """
        return not (self.seen or self.said or self.events or self.heard or self.read)

    @property
    def labels(self) -> tuple[str, ...]:
        """Every distinct vision label inside, commonest first."""
        counts: dict[str, int] = {}
        for observation in self.seen:
            for label in getattr(observation, "labels", ()) or ():
                counts[str(label)] = counts.get(str(label), 0) + 1
        return tuple(sorted(counts, key=lambda label: (-counts[label], label)))

    @property
    def named_events(self) -> tuple[Any, ...]:
        """The events something could put a name to.

        `unexpected_event` is the correlator saying it could not, and passing
        that to a reader as though it were a finding is how a model ends up
        telling a story about something nobody identified.
        """
        return tuple(event for event in self.events if getattr(event, "is_named", False))

    @property
    def sources(self) -> tuple[str, ...]:
        """Which detectors contributed, sorted. The §21 provenance question."""
        found: set[str] = set()
        for event in self.events:
            found.update(str(item) for item in getattr(event, "sources", ()) or ())
        return tuple(sorted(found))

    def words(self, limit: int = 0) -> str:
        """What was said inside, joined; empty when nothing was."""
        spoken = [
            text for segment in self.said if (text := (getattr(segment, "text", "") or "").strip())
        ]
        joined = " ".join(spoken)
        return joined if not limit or len(joined) <= limit else joined[:limit] + "..."

    def summary(self) -> dict[str, Any]:
        return {
            "span": self.span.label(),
            "seconds": round(self.span.duration, 2),
            "seen": len(self.seen),
            "said": len(self.said),
            "events": len(self.events),
            "named_events": len(self.named_events),
            "heard": len(self.heard),
            "labels": list(self.labels[:6]),
        }


@dataclass(frozen=True, slots=True)
class Stores:
    """What the caller fetched, keyed by recording.

    A mapping rather than a flat sequence, and that is the whole point: the
    stored observations do not carry their own media id, so the only place
    that knows which recording they came from is the caller that asked for
    them. Handing this projection a flat list would make it guess.
    """

    seen: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    said: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    events: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    heard: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    cuts: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    read: Mapping[str, Sequence[Any]] = field(default_factory=dict)


def project(span: Span, stores: Stores) -> Evidence:
    """Everything recorded inside ``span``, from the recording it belongs to."""
    return Evidence(
        span=span,
        seen=_inside(span, stores.seen, "timestamp"),
        said=_inside(span, stores.said, "start"),
        events=_inside(span, stores.events, "start_seconds"),
        heard=_inside(span, stores.heard, "start_seconds"),
        cuts=_inside(span, stores.cuts, "start_seconds"),
        read=_inside(span, stores.read, "timestamp"),
    )


def _inside(span: Span, store: Mapping[str, Sequence[Any]], attribute: str) -> tuple[Any, ...]:
    """The records of one store that fall inside the span, in time order.

    The time attribute differs by store -- vision calls it ``timestamp``,
    transcript calls it ``start``, events call it ``start_seconds`` -- and
    naming it here rather than duck-typing means a store whose shape changes
    fails at the call site instead of silently matching nothing. That failure
    mode is not hypothetical: an earlier version of this read `media_id` off
    an object that has never had one, so every lookup returned nothing and
    every caller saw an empty result that looked like quiet footage.
    """
    records = [
        record
        for record in store.get(span.media_id, ())
        if span.contains(getattr(record, attribute, None))
    ]
    records.sort(key=lambda record: getattr(record, attribute, 0.0))
    return tuple(records)


__all__ = ["Evidence", "Span", "Stores", "project"]
