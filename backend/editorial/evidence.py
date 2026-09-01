"""One moment, read the way an editor reads a shot (V2-P11).

The pipeline knows what happened, when, and how strongly. What it has never
assembled in one place is the thing an editor actually looks at:

    What was true before this? What changed? What is true after?
    Where can this be cut without hurting it, and where must it not be?
    What is this shot *for* -- setup, action, payoff, reaction?

Every input already exists. :mod:`backend.evidence.projection` reads across the
analysis stores for any span with one definition of "near"; the semantic lanes
give intensity, tension, motion, speech and events at 2 Hz; V2-P2's phases say
which part of its own arc a moment is in. This module is the reading, not a new
measurement, and there is deliberately no table behind it: the analysis stores
are the evidence, and a stored copy would be a second thing to invalidate.

That is also what keeps §11 true. A style change, a duration change, a new
selection strategy -- none of them touch this, because it is derived on demand
from stores that did not change.

**Every field here has a consumer.** The temptation in a structure this shape
is to add what an editor *might* want; a field nobody reads is the orphaned
configuration key of the domain model, and this project has spent a week
finding those. What is absent is as deliberate as what is present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.evidence import Span, Stores, project

logger = get_logger("editorial.evidence", LogChannel.PIPELINE)

#: How far either side of a moment counts as "before" and "after".
#:
#: Long enough that a state has time to be one, short enough that it is still
#: about this moment. The semantic lanes are sampled every half second, so this
#: is sixteen samples a side -- enough for a median to mean something.
CONTEXT_SECONDS: Final[float] = 8.0

#: A lane reading above this is "present" for the boolean questions: is someone
#: talking, is the picture moving. The lanes are percentile-normalised within
#: the session, so this is a share of that session's own range.
PRESENT: Final[float] = 0.5

#: A cut inside speech is the defect V2-P3 exists to prevent, so a candidate
#: point closer than this to spoken words is not offered at all.
SPEECH_MARGIN: Final[float] = 0.25


@dataclass(frozen=True, slots=True)
class State:
    """What the session was doing across one stretch.

    Five lanes, medians rather than means: a single spike in an eight-second
    window is not what "before" was like, and a mean lets it pretend to be.
    """

    intensity: float = 0.0
    tension: float = 0.0
    motion: float = 0.0
    audio: float = 0.0
    speech: float = 0.0

    @property
    def speaking(self) -> bool:
        return self.speech >= PRESENT

    @property
    def moving(self) -> bool:
        return self.motion >= PRESENT

    def as_dict(self) -> dict[str, float]:
        return {
            "intensity": round(self.intensity, 3),
            "tension": round(self.tension, 3),
            "motion": round(self.motion, 3),
            "audio": round(self.audio, 3),
            "speech": round(self.speech, 3),
        }


@dataclass(frozen=True, slots=True)
class CutPoints:
    """Where this shot may begin and end, and where it must not.

    Candidates come from things the pipeline already found -- scene boundaries
    and the level changes the semantic timeline records -- rather than from a
    grid. A cut on a seam the footage already has is invisible; a cut on a
    round number is a cut.
    """

    #: Seconds, absolute in the recording, where an in-point would land on a
    #: seam the footage already has.
    into: tuple[float, ...] = ()
    #: The same for an out-point.
    out_of: tuple[float, ...] = ()
    #: Spans where a cut would land inside speech. V2-P3's rule, as data.
    forbidden: tuple[tuple[float, float], ...] = ()

    def safe(self, at: float) -> bool:
        """Whether a cut at this second would fall inside someone's sentence."""
        return not any(
            start - SPEECH_MARGIN <= at <= end + SPEECH_MARGIN
            for start, end in self.forbidden
        )

    def best_in(self, default: float) -> float:
        """The latest safe seam at or before the moment starts, or the default."""
        usable = [point for point in self.into if point <= default and self.safe(point)]
        return max(usable) if usable else default

    def best_out(self, default: float) -> float:
        """The earliest safe seam at or after the moment ends, or the default."""
        usable = [point for point in self.out_of if point >= default and self.safe(point)]
        return min(usable) if usable else default


@dataclass(frozen=True, slots=True)
class EditorialEvidence:
    """One moment, with what an editor would need in order to cut it.

    Identity is by moment and recording. There is no evidence id because there
    is no evidence row: this is a projection, and giving it an identity of its
    own would invite something to store it.
    """

    media_id: str
    moment_id: str
    #: The situation this belongs to, when one was read. Empty when the moment
    #: stands alone, which is a true answer rather than a missing one.
    situation_id: str = ""

    source_start: float = 0.0
    source_end: float = 0.0
    context_start: float = 0.0
    context_end: float = 0.0

    before: State = field(default_factory=State)
    during: State = field(default_factory=State)
    after: State = field(default_factory=State)

    #: What the correlator named here, commonest first, `unknown_event`
    #: excluded -- an unnamed event is the correlator saying it could not tell,
    #: and passing that on as a finding is how a story gets told about nothing.
    events: tuple[str, ...] = ()
    #: What the vision model saw, commonest first.
    subjects: tuple[str, ...] = ()
    #: What was said inside, or empty.
    speech: str = ""

    #: V2-P2's reading of this moment's own arc, when it could name one.
    phase: str = ""
    phase_confidence: float = 0.0

    cuts: CutPoints = field(default_factory=CutPoints)

    #: Where inside this moment the thing it is about begins and ends (V2-P2.2).
    #:
    #: An interpretation laid over the raw span, never a replacement for it:
    #: `source_start` and `source_end` above remain what the moment stage
    #: measured, and a reader can compare the two. Empty when nothing inside
    #: could be located, which is the honest answer for a moment of one event.
    span: Any = None

    #: Whether anything at all was recorded here. "Nothing happened" and
    #: "nobody looked" are different statements and only the second is a
    #: reason to distrust everything else said about this stretch.
    observed: bool = True
    #: Fields that could not be filled, named. A reader that cannot see what is
    #: missing will read absence as zero.
    unknown: tuple[str, ...] = ()

    @property
    def duration(self) -> float:
        return max(0.0, self.source_end - self.source_start)

    @property
    def rising(self) -> bool:
        """Whether the session was more intense after this than before it."""
        return self.after.intensity > self.before.intensity

    @property
    def resolves(self) -> bool:
        """Whether the tension this moment carried let go afterwards.

        The editorial question behind "is this a payoff": something was
        building, and then it was not.
        """
        return self.during.tension > self.after.tension

    @property
    def reaction_follows(self) -> bool:
        """Whether somebody speaks after this that was not speaking during it."""
        return self.after.speaking and not self.during.speaking

    def as_dict(self) -> dict[str, Any]:
        return {
            "moment_id": self.moment_id,
            "situation_id": self.situation_id,
            "source": [round(self.source_start, 2), round(self.source_end, 2)],
            "before": self.before.as_dict(),
            "during": self.during.as_dict(),
            "after": self.after.as_dict(),
            "events": list(self.events),
            "subjects": list(self.subjects[:6]),
            "phase": self.phase,
            "phase_confidence": round(self.phase_confidence, 3),
            "rising": self.rising,
            "resolves": self.resolves,
            "reaction_follows": self.reaction_follows,
            "cut_in": round(self.cuts.best_in(self.source_start), 2),
            "cut_out": round(self.cuts.best_out(self.source_end), 2),
            "observed": self.observed,
            "unknown": list(self.unknown),
        }


def read(
    moment: Any,
    *,
    stores: Stores,
    reader: Any = None,
    phases: Any = None,
    situation_id: str = "",
) -> EditorialEvidence:
    """Read one moment as a shot.

    Args:
        moment: the candidate, with its own span and context.
        stores: what the caller fetched per recording, for
            :func:`backend.evidence.project`.
        reader: the session's semantic lanes. Without one the states come back
            empty and say so in ``unknown`` rather than reading as calm.
        phases: V2-P2's phase classifier output for this moment, when the
            caller has it.
        situation_id: the editorial situation this belongs to, if any.
    """
    media_id = str(getattr(moment, "media_id", ""))
    start = float(getattr(moment, "start_seconds", 0.0))
    end = float(getattr(moment, "end_seconds", start))
    unknown: list[str] = []

    span = Span(media_id=media_id, start_seconds=start, end_seconds=end)
    seen = project(span.widened(CONTEXT_SECONDS), stores)
    inside = project(span, stores)

    if reader is None:
        unknown.extend(["before", "during", "after"])
        before = during = after = State()
    else:
        before = _state(reader, start - CONTEXT_SECONDS, start)
        during = _state(reader, start, end)
        after = _state(reader, end, end + CONTEXT_SECONDS)

    phase, confidence = _phase(phases)
    if not phase:
        unknown.append("phase")

    return EditorialEvidence(
        media_id=media_id,
        moment_id=str(getattr(moment, "id", "") or ""),
        situation_id=situation_id,
        source_start=start,
        source_end=end,
        context_start=float(getattr(moment, "context_start", start)),
        context_end=float(getattr(moment, "context_end", end)),
        before=before,
        during=during,
        after=after,
        events=_names(inside),
        subjects=seen.labels[:8],
        speech=inside.words(limit=280),
        phase=phase,
        phase_confidence=confidence,
        cuts=_cuts(seen, span),
        span=_editorial_span(moment, reader, stores),
        observed=not seen.is_empty,
        unknown=tuple(unknown),
    )


def _editorial_span(moment: Any, reader: Any, stores: Stores) -> Any:
    """Read the moment's internal structure, or None when there is none.

    Never fatal, and never a substitute: a moment whose boundaries cannot be
    placed keeps its raw span and every consumer falls back to it, which is
    what every consumer did before this layer existed.
    """
    from backend.editorial import event_span

    try:
        media_id = str(getattr(moment, "media_id", ""))
        end = float(getattr(moment, "end_seconds", 0.0))
        following = project(
            Span(
                media_id=media_id,
                start_seconds=end,
                end_seconds=end + CONTEXT_SECONDS,
            ),
            stores,
        )
        return event_span.read(moment, reader=reader, after=following)
    except Exception:
        logger.info(
            "Could not read this moment's internal structure; the raw span stands",
            extra={"moment_id": str(getattr(moment, "id", ""))},
        )
        return None


def _state(reader: Any, start: float, end: float) -> State:
    """The five lanes across one stretch, as medians."""
    if end <= start:
        return State()
    return State(
        intensity=_median(reader, "intensity", start, end),
        tension=_median(reader, "tension", start, end),
        motion=_median(reader, "motion", start, end),
        audio=_median(reader, "audio", start, end),
        speech=_median(reader, "speech", start, end),
    )


def _median(reader: Any, lane: str, start: float, end: float) -> float:
    import statistics

    try:
        window = list(reader.window(lane, max(0.0, start), end))
    except Exception:
        return 0.0
    return float(statistics.median(window)) if window else 0.0


def _names(evidence: Any) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for event in evidence.named_events:
        name = str(getattr(getattr(event, "event_type", None), "value", "") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return tuple(sorted(counts, key=lambda name: (-counts[name], name)))


def _phase(phases: Any) -> tuple[str, float]:
    """V2-P2's answer, or an honest blank."""
    if phases is None:
        return "", 0.0
    name = str(getattr(phases, "phase", "") or getattr(phases, "name", "") or "")
    if name in ("", "unknown"):
        return "", 0.0
    return name, float(getattr(phases, "confidence", 0.0) or 0.0)


def _cuts(evidence: Any, span: Span) -> CutPoints:
    """Seams the footage already has, and the speech a cut must not land in."""
    seams = tuple(
        sorted(
            float(getattr(cut, "start_seconds", 0.0))
            for cut in evidence.cuts
            if getattr(cut, "start_seconds", None) is not None
        )
    )
    spoken = tuple(
        (float(getattr(said, "start", 0.0)), float(getattr(said, "end", 0.0)))
        for said in evidence.said
        if getattr(said, "end", None) is not None
    )
    return CutPoints(
        into=tuple(point for point in seams if point <= span.end_seconds),
        out_of=tuple(point for point in seams if point >= span.start_seconds),
        forbidden=spoken,
    )


__all__ = [
    "CONTEXT_SECONDS",
    "PRESENT",
    "CutPoints",
    "EditorialEvidence",
    "State",
    "read",
]
