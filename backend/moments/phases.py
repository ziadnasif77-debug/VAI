"""What a moment is doing, second by second (V2-P2).

A moment has always been a *story fragment* rather than an instant -- setup,
enemy appears, combat, kill, reaction -- and the code has always treated it as
one span with a type. Everything downstream then had to guess where inside it
the interesting part was: the effects planner placed by a fixed fraction of
the length, context expansion added a constant pre-roll per type, and the
Critic could only trim from the ends.

This names the parts. Not with new labels invented over the ones the detectors
produced -- 59% of the events on the gate session are already ``unknown_event``
and stacking taxonomy on that would be inventing understanding -- but from the
**shape of the session's own lanes**, which is a measurement:

    the peak                  -> payoff  (climax where the level says so)
    rising, before the peak   -> anticipation
    flat and low, at the head -> setup
    falling, after the peak   -> reaction
    the dead-zone lane        -> dead
    lanes too flat to tell    -> unknown, and nothing else

Every phase carries the confidence its own evidence supports, and a moment
whose lanes are flat gets one ``unknown`` phase covering it rather than a
plausible-looking arc. The system says what it measured or says it does not
know; it does not narrate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from backend.core.logging import LogChannel, get_logger
from backend.semantic.reader import SemanticReader

logger = get_logger("moments.phases", LogChannel.PIPELINE)

PhaseName = Literal[
    "setup", "anticipation", "escalation", "payoff", "reaction", "dead", "unknown"
]

#: Below this spread between a moment's quietest and loudest half-second, the
#: lanes cannot separate a build from a payoff. §23's threshold, reused: the
#: same number the level grader uses before it stops trusting quantiles.
FLAT_SPREAD: Final[float] = 0.15

#: A phase shorter than this is a sample, not a beat, and is merged into its
#: neighbour. The first version used one second -- two bins at 2Hz -- and
#: produced twenty-two alternating phases for a single moment, which is a
#: measurement of noise wearing the vocabulary of a story.
MIN_PHASE_SECONDS: Final[float] = 2.5

#: The moving average applied before any shaping. The lanes are half-second
#: samples of a noisy world; a rise reads as a rise over seconds, not bins.
SMOOTH_SECONDS: Final[float] = 3.0

#: The spread at which a shape is beyond argument. Confidence runs from zero
#: at FLAT_SPREAD -- where the classifier itself refuses to name anything --
#: to one here. Anchoring it to 1.0 instead, as the first version did, meant a
#: moment whose peak stood four times its floor reported 0.36: an honest
#: number on the wrong scale, and every consumer reading it as a probability
#: would have discarded a shape that was perfectly clear.
CERTAIN_SPREAD: Final[float] = 0.50

#: The payoff is a plateau, not an instant: every bin within this fraction of
#: the peak, contiguous with it, belongs to the same beat.
PLATEAU_OF_PEAK: Final[float] = 0.85

#: A reaction lasts while the moment is still coming down. Past this height
#: it has landed, and whatever follows is footage rather than a beat.
REACTION_FLOOR: Final[float] = 0.35


@dataclass(frozen=True, slots=True)
class MomentPhase:
    """One stretch of a moment, and how sure we are of it."""

    name: PhaseName
    start_seconds: float
    end_seconds: float
    confidence: float

    @property
    def seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start_seconds": round(self.start_seconds, 3),
            "end_seconds": round(self.end_seconds, 3),
            "confidence": round(self.confidence, 3),
        }


def classify_phases(
    reader: SemanticReader | None,
    *,
    start_seconds: float,
    end_seconds: float,
) -> tuple[MomentPhase, ...]:
    """The shape of ``[start, end)``, as named phases.

    Returns a single ``unknown`` phase -- never an empty tuple -- when there
    is no reader, no span, or no measurable shape. A caller that gets phases
    can trust them; a caller that gets ``unknown`` knows precisely that it
    learned nothing, which is a different statement from "this was calm".
    """
    span = end_seconds - start_seconds
    if reader is None or span <= 0:
        return (MomentPhase("unknown", start_seconds, end_seconds, 0.0),)

    raw = list(reader.window("intensity", start_seconds, end_seconds))
    dead = list(reader.window("dead_zones", start_seconds, end_seconds))
    if not raw:
        return (MomentPhase("unknown", start_seconds, end_seconds, 0.0),)

    step = span / len(raw)
    intensity = _smoothed(raw, window=max(1, round(SMOOTH_SECONDS / step)))
    floor, ceiling = min(intensity), max(intensity)
    spread = ceiling - floor
    if spread < FLAT_SPREAD:
        # Nothing here rises or falls. Naming a payoff would be inventing one.
        return (
            MomentPhase(
                "dead" if sum(dead) > len(dead) / 2 else "unknown",
                start_seconds,
                end_seconds,
                0.0,
            ),
        )

    peak = max(range(len(intensity)), key=lambda i: intensity[i])
    plateau = _plateau(intensity, peak, floor + PLATEAU_OF_PEAK * spread)
    level = reader.level_for(
        start_seconds + plateau[0] * step, start_seconds + (plateau[1] + 1) * step
    )
    reaction_ends = _reaction_end(intensity, plateau[1], floor + REACTION_FLOOR * spread)

    rising = _rising_bands(intensity[: plateau[0]], floor, ceiling)

    named: list[PhaseName] = []
    for index in range(len(intensity)):
        if dead[index] >= 0.5:
            named.append("dead")
        elif plateau[0] <= index <= plateau[1]:
            named.append("payoff")
        elif index < plateau[0]:
            named.append(rising[index])
        elif index <= reaction_ends:
            named.append("reaction")
        else:
            # Past the come-down. Calling ninety seconds of flat footage a
            # "reaction" would dress a badly-formed moment as a story beat --
            # and the Critic reads these to decide what to trim.
            named.append("unknown")

    phases = _runs(named, start_seconds, step, len(intensity))
    phases = _merge_slivers(phases)
    phases = _coalesce(phases)
    certainty = min(
        1.0, max(0.0, (spread - FLAT_SPREAD) / (CERTAIN_SPREAD - FLAT_SPREAD))
    )
    return tuple(_confidence(phases, peak_confidence=certainty, level=level))


def _smoothed(values: list[float], *, window: int) -> list[float]:
    """A centred moving average, edges included rather than trimmed."""
    if window <= 1 or len(values) <= window:
        return list(values)
    half = window // 2
    out: list[float] = []
    for index in range(len(values)):
        lo = max(0, index - half)
        hi = min(len(values), index + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _plateau(intensity: list[float], peak: int, threshold: float) -> tuple[int, int]:
    """The contiguous run around ``peak`` that stays near it.

    A payoff measured as one half-second bin is a sample of a payoff. The beat
    is the stretch that stays up there.
    """
    lo = hi = peak
    while lo > 0 and intensity[lo - 1] >= threshold:
        lo -= 1
    while hi + 1 < len(intensity) and intensity[hi + 1] >= threshold:
        hi += 1
    return lo, hi


def _reaction_end(intensity: list[float], after: int, threshold: float) -> int:
    """The last bin still coming down from the payoff."""
    index = after
    while index + 1 < len(intensity) and intensity[index + 1] >= threshold:
        index += 1
    return index


#: Where the climb stops being setup and starts being a build, and where the
#: build stops being anticipation and starts being escalation. Fractions of
#: the moment's own range.
SETUP_CEILING: Final[float] = 0.25
ESCALATION_FLOOR: Final[float] = 0.60


def _rising_bands(
    values: list[float], floor: float, ceiling: float
) -> list[PhaseName]:
    """Name the run-up to the payoff: setup, then the build into it.

    Classifying every bin by its own height was the first version, and on a
    real ninety-second moment it produced nine alternating names -- a truthful
    report that the moment contains several arcs, and a useless one, because
    no consumer wants the wiggles. What a consumer wants is where the *final*
    climb begins, so a riser can sit under it and a cut can quicken through
    it. So: everything up to the last time the curve was down in the setup
    band is setup, and the single climb after that is named once.

    The distinction earns its keep downstream: setup is footage a viewer needs
    and an editor may trim, anticipation is where a riser belongs, escalation
    is where the cutting should already be quick.
    """
    if not values:
        return []
    if ceiling <= floor:
        return ["setup"] * len(values)

    heights = [(value - floor) / (ceiling - floor) for value in values]
    # The build begins after the last moment the curve was properly low.
    begins = 0
    for index, height in enumerate(heights):
        if height < SETUP_CEILING:
            begins = index + 1
    begins = min(begins, len(heights))

    names: list[PhaseName] = ["setup"] * begins
    climb = heights[begins:]
    if not climb:
        return names
    # One boundary, crossed once: the first bin at escalation height and
    # everything after it is escalation, whatever the curve does in between.
    hard = next(
        (index for index, height in enumerate(climb) if height >= ESCALATION_FLOOR),
        len(climb),
    )
    names.extend(["anticipation"] * hard)
    names.extend(["escalation"] * (len(climb) - hard))
    return names


def _runs(
    named: list[PhaseName], start: float, step: float, count: int
) -> list[MomentPhase]:
    phases: list[MomentPhase] = []
    current = named[0]
    first = 0
    for index in range(1, count):
        if named[index] != current:
            phases.append(
                MomentPhase(current, start + first * step, start + index * step, 0.0)
            )
            current, first = named[index], index
    phases.append(MomentPhase(current, start + first * step, start + count * step, 0.0))
    return phases


def _merge_slivers(phases: list[MomentPhase]) -> list[MomentPhase]:
    """Absorb sub-second runs, keeping the span covered exactly."""
    if len(phases) <= 1:
        return phases
    merged: list[MomentPhase] = []
    for phase in phases:
        if merged and phase.seconds < MIN_PHASE_SECONDS and phase.name != "payoff":
            previous = merged[-1]
            merged[-1] = MomentPhase(
                previous.name, previous.start_seconds, phase.end_seconds, 0.0
            )
            continue
        merged.append(phase)
    if len(merged) >= 2 and merged[0].seconds < MIN_PHASE_SECONDS:
        merged[1] = MomentPhase(
            merged[1].name, merged[0].start_seconds, merged[1].end_seconds, 0.0
        )
        merged = merged[1:]
    return merged


def _coalesce(phases: list[MomentPhase]) -> list[MomentPhase]:
    """One run per name. Absorbing a sliver leaves its neighbours adjacent and
    identically named, which reads as two beats where there is one."""
    joined: list[MomentPhase] = []
    for phase in phases:
        if joined and joined[-1].name == phase.name:
            joined[-1] = MomentPhase(
                phase.name, joined[-1].start_seconds, phase.end_seconds, 0.0
            )
            continue
        joined.append(phase)
    return joined


def _confidence(
    phases: list[MomentPhase], *, peak_confidence: float, level: str
) -> list[MomentPhase]:
    """How much each phase's own evidence supports its name.

    The payoff carries the peak's certainty, raised when the level grader
    agrees it is hot. Everything else is read off the payoff: a build-up is
    only as certain as the thing it builds to.
    """
    agrees = 1.0 if level in ("high", "climax") else 0.8
    return [
        MomentPhase(
            phase.name,
            phase.start_seconds,
            phase.end_seconds,
            0.0
            if phase.name in ("unknown", "dead")
            else min(1.0, peak_confidence * agrees * (1.0 if phase.name == "payoff" else 0.9)),
        )
        for phase in phases
    ]


def phase_named(phases: tuple[MomentPhase, ...], name: PhaseName) -> MomentPhase | None:
    """The first phase called ``name``, or ``None``."""
    return next((phase for phase in phases if phase.name == name), None)


__all__ = [
    "FLAT_SPREAD",
    "MIN_PHASE_SECONDS",
    "MomentPhase",
    "PhaseName",
    "classify_phases",
    "phase_named",
]
