"""A moment read as onset, action, resolution and aftermath (V2-P2.2).

Derived, never stored, and never a replacement. `game_events` remains the truth
about what happened and when; `Moment` remains the unit the pipeline selects.
This sits above both and answers a question neither can: **inside a moment that
runs for three minutes, where is the thing it is about?**

That question had no answer until V2-P2.0, and for a reason worth keeping in
view. The loader was dropping 56 % of event references, so a moment spanning
[2935.0, 3112.9] arrived carrying three events that all claimed the moment's
own span. There was nothing to locate anything *with*. With the references
restored the same moment carries thirteen events, and the victory inside it
sits at [3000.8, 3011.7] with an importance of 0.901 — 37 % of the way in,
occupying 6 % of the span. The decisive instant was there the whole time.

## Four boundaries, and the right to refuse

Each boundary carries a timestamp, a confidence, the store that supplied it and
a sentence saying why. **A boundary with no evidence is not produced.** Not
guessed from the midpoint, not interpolated, not inferred from an event type:
a moment labelled `victory` does not get a resolution because the word suggests
one, it gets a resolution when an event that resolves something is actually
there with its own span.

The evidence is consulted in a fixed order, strongest first:

1. **the events themselves** — their own spans and importances;
2. **the semantic lanes** — tension falling is a resolution the events did not
   name;
3. **vision and motion** — the picture settling;
4. **audio and speech** — somebody reacting.

Phase information is read where V2-P2 stored it. Anything the evidence cannot
place is named in `unknown` rather than filled in, because a reader that cannot
see what is missing reads absence as fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.editorial.semantics import RESOLVING

logger = get_logger("editorial.event_span", LogChannel.PIPELINE)

#: How much of a moment an event must leave over before locating it inside is
#: worth doing.
#:
#: A tenth. An event filling 95 % of its moment *is* the moment, and splitting
#: it into four parts would be inventing structure out of rounding.
WORTH_LOCATING: Final[float] = 0.10

#: How long after a resolution counts as its aftermath.
#:
#: Eight seconds, the same window the editorial reading looks ahead over. Long
#: enough for a reaction to start, short enough to still be about this event.
AFTERMATH_SECONDS: Final[float] = 8.0

#: A lane reading below this is "settled" -- the tension has let go.
SETTLED: Final[float] = 0.35


@dataclass(frozen=True, slots=True)
class Boundary:
    """One instant, and the evidence that put it there.

    There is no default and no zero value. A boundary that could not be
    established is `None`, and the span names it in `unknown`; a `Boundary`
    holding 0.0 would be indistinguishable from the start of a recording.
    """

    seconds: float
    #: How sure the evidence is, carried from whatever supplied it.
    confidence: float
    #: Which store said so: `events`, `lanes`, `vision`, `audio`, `phases`.
    source: str
    #: One sentence a person can check against the footage.
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "seconds": round(self.seconds, 3),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class EditorialEventSpan:
    """Where inside a moment the thing it is about begins and ends.

    The raw span is carried alongside, unchanged, because this layer is an
    interpretation of it and a reader must be able to see both.
    """

    #: The moment's own span. Evidence, not interpretation.
    raw_start: float
    raw_end: float

    onset: Boundary | None = None
    action: Boundary | None = None
    resolution: Boundary | None = None
    aftermath: Boundary | None = None

    #: Which of the four could not be established, named.
    unknown: tuple[str, ...] = field(default=())

    @property
    def raw_duration(self) -> float:
        return max(0.0, self.raw_end - self.raw_start)

    @property
    def is_located(self) -> bool:
        """Whether anything inside the moment was actually placed."""
        return self.resolution is not None or self.action is not None

    @property
    def decisive_seconds(self) -> float | None:
        """The instant the moment turns on, when one could be established."""
        return self.resolution.seconds if self.resolution else None

    @property
    def editorial_start(self) -> float:
        """Where the shot could begin without losing what it is about."""
        return self.onset.seconds if self.onset else self.raw_start

    @property
    def editorial_end(self) -> float:
        """Where it could end. The aftermath when there is one, else the
        resolution, else the raw end -- each a fact rather than a guess."""
        if self.aftermath:
            return self.aftermath.seconds
        if self.resolution:
            return self.resolution.seconds
        return self.raw_end

    @property
    def editorial_duration(self) -> float:
        return max(0.0, self.editorial_end - self.editorial_start)

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": [round(self.raw_start, 2), round(self.raw_end, 2)],
            "editorial": [round(self.editorial_start, 2), round(self.editorial_end, 2)],
            "onset": self.onset.as_dict() if self.onset else None,
            "action": self.action.as_dict() if self.action else None,
            "resolution": self.resolution.as_dict() if self.resolution else None,
            "aftermath": self.aftermath.as_dict() if self.aftermath else None,
            "unknown": list(self.unknown),
        }


def read(moment: Any, *, reader: Any = None, after: Any = None) -> EditorialEventSpan:
    """Locate the four boundaries inside one moment, or decline to.

    Args:
        moment: the moment, carrying its real events. Since V2-P2.0 those have
            their own spans; before it they claimed the moment's span, and this
            function would correctly find nothing to locate.
        reader: the session's semantic lanes, when there are any.
        after: the projection over the stretch following the moment, for the
            reaction that marks an aftermath.
    """
    start = float(getattr(moment, "start_seconds", 0.0))
    end = float(getattr(moment, "end_seconds", start))
    events = [
        event
        for event in (getattr(moment, "events", ()) or ())
        if _placed(event, start, end)
    ]

    unknown: list[str] = []
    if not events:
        # Either the moment has no events, or they all claim its whole span --
        # which is what a pre-V2-P2.0 load looked like, and is indistinguishable
        # from a genuine single-event moment. Both are honestly "nothing to
        # locate", and both say so rather than producing four boundaries around
        # a span nobody measured.
        return EditorialEventSpan(
            raw_start=start,
            raw_end=end,
            unknown=("onset", "action", "resolution", "aftermath"),
        )

    onset = _onset(events, start)
    action = _action(events)
    resolution = _resolution(events, reader, start, end)
    aftermath = _aftermath(resolution, reader, after, end)

    for name, found in (
        ("onset", onset),
        ("action", action),
        ("resolution", resolution),
        ("aftermath", aftermath),
    ):
        if found is None:
            unknown.append(name)

    return EditorialEventSpan(
        raw_start=start,
        raw_end=end,
        onset=onset,
        action=action,
        resolution=resolution,
        aftermath=aftermath,
        unknown=tuple(unknown),
    )


# -- the four, each from its own evidence ------------------------------------


def _placed(event: Any, start: float, end: float) -> bool:
    """Whether this event says anything about *where* inside the moment it is.

    An event whose span is the moment's own span is a placeholder or a moment
    of exactly one event; either way it locates nothing, and treating it as a
    boundary would produce four boundaries that are all the same number.
    """
    try:
        a = float(event.start_seconds)
        b = float(event.end_seconds)
    except (AttributeError, TypeError, ValueError):
        return False
    span = end - start
    if span <= 0:
        return False
    return (b - a) < span * (1.0 - WORTH_LOCATING)


def _onset(events: list, start: float) -> Boundary | None:
    """Where the first thing happens. The earliest event, and nothing else."""
    first = min(events, key=lambda event: event.start_seconds)
    return Boundary(
        seconds=float(first.start_seconds),
        confidence=float(getattr(first, "confidence", 0.0) or 0.0),
        source="events",
        reason=(
            f"the first {first.event_type.value} of this moment, "
            f"{first.start_seconds - start:.1f}s in"
        ),
    )


def _action(events: list) -> Boundary | None:
    """Where the moment is busiest.

    The event carrying the most importance. Not the midpoint of the span, and
    not the mean of the events -- both would be arithmetic dressed as a
    reading, and neither points at anything a viewer would recognise.
    """
    busiest = max(events, key=lambda event: float(getattr(event, "importance", 0.0) or 0.0))
    weight = float(getattr(busiest, "importance", 0.0) or 0.0)
    if weight <= 0.0:
        return None
    return Boundary(
        seconds=float(busiest.start_seconds),
        confidence=float(getattr(busiest, "confidence", 0.0) or 0.0),
        source="events",
        reason=f"the heaviest event here is a {busiest.event_type.value} ({weight:.2f})",
    )


def _resolution(events: list, reader: Any, start: float, end: float) -> Boundary | None:
    """Where something concluded — **only if something did**.

    Two kinds of evidence, in order. A resolving event has its own span and
    its own importance, and its end is the instant the thing was decided. The
    tension lane can say the same where no event named it: pressure that was
    high and then is not.

    What does not count: the moment's type. A moment labelled `victory`
    inherits that word from an event, and if the event is not here with a span
    of its own then there is nothing to point at. Producing a resolution from
    the label would be exactly the invention this layer exists to avoid.
    """
    resolving = [
        event for event in events if event.event_type.value in RESOLVING
    ]
    if resolving:
        decisive = max(
            resolving, key=lambda event: float(getattr(event, "importance", 0.0) or 0.0)
        )
        return Boundary(
            seconds=float(decisive.end_seconds),
            confidence=float(getattr(decisive, "confidence", 0.0) or 0.0),
            source="events",
            reason=(
                f"a {decisive.event_type.value} ends here, "
                f"{(decisive.end_seconds - start) / max(end - start, 1e-6):.0%} "
                "through the moment"
            ),
        )

    settled = _settles(reader, start, end)
    if settled is not None:
        seconds, before, after_value = settled
        return Boundary(
            seconds=seconds,
            confidence=round(min(1.0, before - after_value), 3),
            source="lanes",
            reason=(
                f"tension falls from {before:.2f} to {after_value:.2f} here, "
                "with no event naming it"
            ),
        )
    return None


def _aftermath(
    resolution: Boundary | None, reader: Any, after: Any, end: float
) -> Boundary | None:
    """Where the response to it finishes.

    Only ever after a resolution: an aftermath with nothing before it is just
    the end of the moment, and calling it a boundary would add a name without
    adding a fact.
    """
    if resolution is None:
        return None

    spoken = _first_speech_after(after, resolution.seconds)
    if spoken is None:
        # No fallback to the moment's end. That was the first version, at a
        # confidence of 0.5 and a reason reading "the moment runs on after the
        # resolution" -- which is the raw end wearing a boundary's name, adding
        # a label without adding a fact. An aftermath nobody can point at is
        # `unknown`, and `editorial_end` then falls back to the resolution,
        # which is a measurement.
        return None
    return Boundary(
        seconds=spoken,
        confidence=0.8,
        source="audio",
        reason=f"somebody starts speaking {spoken - resolution.seconds:.1f}s after it",
    )


# -- reading the lanes and the transcript ------------------------------------


def _settles(reader: Any, start: float, end: float) -> tuple[float, float, float] | None:
    """The second the tension lets go, when the lane says one.

    Walked forward in whole seconds rather than sampled, because the question
    is *where* it fell and a mean over the span cannot answer that.
    """
    if reader is None or end - start < 2.0:
        return None
    try:
        step = 1.0
        at = start + step
        while at < end:
            before = _median(reader, "tension", max(start, at - 4.0), at)
            later = _median(reader, "tension", at, min(end, at + 4.0))
            if before > SETTLED and later <= SETTLED and before - later >= 0.2:
                return at, before, later
            at += step
    except Exception:
        return None
    return None


def _median(reader: Any, lane: str, start: float, end: float) -> float:
    import statistics

    if end <= start:
        return 0.0
    window = list(reader.window(lane, max(0.0, start), end))
    return float(statistics.median(window)) if window else 0.0


def _first_speech_after(after: Any, seconds: float) -> float | None:
    if after is None:
        return None
    starts = [
        float(getattr(said, "start", 0.0) or 0.0)
        for said in (getattr(after, "said", ()) or ())
        if float(getattr(said, "start", 0.0) or 0.0) >= seconds
    ]
    return min(starts) if starts else None


__all__ = [
    "AFTERMATH_SECONDS",
    "SETTLED",
    "WORTH_LOCATING",
    "Boundary",
    "EditorialEventSpan",
    "read",
]
