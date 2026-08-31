"""Scoring an edit before it is rendered (V2-P6).

Deterministic, and that is the design rather than a limitation. A judge that
cannot be reproduced is not a judge, it is a mood: the same three plans would
win in different orders on different days, and no regression in the optimiser
would ever be visible through it. Every axis here is arithmetic over things
already measured -- the session's lanes, the moments' own types and scores,
the shape the plan produces.

Eight axes, because they are the eight things a person actually complains
about, and each is normalised 0..1 so the weights are readable as opinions
rather than as units.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.semantic.reader import SemanticReader

logger = get_logger("narrative.judge", LogChannel.PIPELINE)

#: What each axis is worth. Coherence and structure lead because a video that
#: does not hold together cannot be rescued by density, and ending strength is
#: here at all because "it stops rather than ends" was a real complaint about
#: a real render.
AXIS_WEIGHTS: Final[dict[str, float]] = {
    "coherence": 1.00,
    "structure": 0.95,
    "pacing": 0.90,
    "intensity": 0.85,
    "variety": 0.75,
    "ending": 0.70,
    "effect_density": 0.45,
    "audio_density": 0.40,
}

#: A clip this far from its neighbour is a jump the viewer feels.
NEIGHBOUR_GAP_SECONDS: Final[float] = 240.0

#: How many shots of unchanging length in a row stop being a rhythm.
#:
#: Four. Three shots at one length is a pattern a viewer reads as deliberate;
#: the fourth is when it starts reading as nothing having been decided. This
#: is the *other* way shot length fails to be a decision, and the axis needs
#: both -- a definition that only punished arbitrary variation would score a
#: perfect metronome at 1.00.
METRONOME_RUN: Final[int] = 4

#: What "enough" looks like on each density axis, per minute.
IDEAL_EFFECTS_PER_MINUTE: Final[float] = 3.0
IDEAL_SPEECH_SHARE: Final[float] = 0.35


@dataclass(frozen=True, slots=True)
class PlanScore:
    """How good an edit looks before anyone has rendered it."""

    coherence: float = 0.0
    pacing: float = 0.0
    variety: float = 0.0
    intensity: float = 0.0
    structure: float = 0.0
    effect_density: float = 0.0
    audio_density: float = 0.0
    ending: float = 0.0
    total: float = 0.0
    why: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "coherence": round(self.coherence, 3),
            "pacing": round(self.pacing, 3),
            "variety": round(self.variety, 3),
            "intensity": round(self.intensity, 3),
            "structure": round(self.structure, 3),
            "effect_density": round(self.effect_density, 3),
            "audio_density": round(self.audio_density, 3),
            "ending": round(self.ending, 3),
            "total": round(self.total, 3),
            "why": list(self.why),
        }


def judge(
    plan: Any,
    *,
    reader: SemanticReader | None,
    config: Any,
    style: Any = None,
    editorial: Any = None,
) -> PlanScore:
    """Score one plan on eight axes, with a sentence for each (§80).

    `editorial` is the reading `backend.editorial.reading` already made for
    this project. Without it the sequence axis falls back to moment type
    alone, and measured on this machine that is not a small loss: reading
    blind, the pacing axis saw 11 to 13 arbitrary cuts on plans where the
    reading saw 2 to 5, because two shots logged as the same type are very
    often a payoff followed by a reaction. A judge working from the weaker
    signal ranks plans by it.
    """
    moments = list(plan.moments)
    if not moments:
        return PlanScore(why=("the plan is empty",))

    why: list[str] = []
    # V2-P8: what this style considers a good amount of each.
    taste = getattr(style, "judgement", None)
    axes = {
        "coherence": _coherence(moments, why),
        "structure": _structure(plan, moments, why),
        "pacing": _pacing(moments, plan, config, why, taste, editorial),
        "intensity": _intensity(moments, reader, why),
        "variety": _variety(moments, why),
        "ending": _ending(moments, reader, why),
        "effect_density": _effect_density(moments, plan, why, taste),
        "audio_density": _audio_density(moments, reader, why, taste),
    }
    total = sum(AXIS_WEIGHTS[name] * value for name, value in axes.items())
    total /= sum(AXIS_WEIGHTS.values())
    return PlanScore(**axes, total=total, why=tuple(why))


def _coherence(moments: Sequence[Any], why: list[str]) -> float:
    """How well the edit hangs together in time.

    A clip four minutes from its neighbour is a jump the viewer feels even
    when the order is perfectly chronological -- chronology guarantees the
    direction of travel, not the distance.
    """
    if len(moments) < 2:
        return 1.0
    jumps = 0
    for before, after in pairwise(moments):
        if before.media_id != after.media_id:
            jumps += 1
            continue
        if after.context_start - before.context_end > NEIGHBOUR_GAP_SECONDS:
            jumps += 1
    value = max(0.0, 1.0 - jumps / max(len(moments) - 1, 1))
    why.append(f"coherence {value:.2f}: {jumps} long jump(s) between clips")
    return value


def _structure(plan: Any, moments: Sequence[Any], why: list[str]) -> float:
    """Whether the edit has a recognisable shape rather than a list."""
    beats = [beat for beat in getattr(plan, "beats", ()) if beat]
    named = len(set(beats))
    value = min(1.0, named / 5.0) if beats else 0.35
    why.append(f"structure {value:.2f}: {named} distinct narrative beat(s)")
    return value


def _pacing(
    moments: Sequence[Any],
    plan: Any,
    config: Any,
    why: list[str],
    style: Any = None,
    editorial: Any = None,
) -> float:
    """Whether the shot lengths are doing work.

    **This axis was measuring the wrong thing, and V2-P0's numbers proved it.**

    It used to score ``(longest - shortest) / mean`` against an ideal of 1.2 --
    an authored constant no edit this system makes has ever approached, the
    house style's own spread being 1.888. So every edit was marked down for
    being what every edit is, and when styles were finally given a say in how
    shots are cut, the three that trim were marked down *further* for
    succeeding: shortening shots lowers the mean, which inflates a spread
    measured against it, even though absolute variance fell four seconds.

    Retuning the constant would have moved the number without fixing anything.
    The real defect is not unevenness, because unevenness is what a cinematic
    edit is made of. It is unevenness that **tracks nothing**: a cut from a
    held shot to a brief one is editing when the brief shot is doing something
    different, and noise when the next shot is more of the same at another
    length.

    So the axis now counts the two ways shot length can fail to be a decision,
    and neither of them is "uneven":

    * **arbitrary variation** -- the length changes and nothing else does, so
      the change is not tracking the material;
    * **a metronome** -- a long run of shots that never change length at all,
      which is the same absence of a decision expressed the other way.

    Between those, a deliberately steady edit and a deliberately uneven one
    both score well, which is the point. There is no ideal spread left in the
    axis, only two named defects.
    """
    from backend.editorial import sequence as sequencing

    lengths = [moment.context_duration for moment in moments]
    if not lengths:
        return 0.0

    reading = sequencing.read(moments, editorial)
    if reading.is_empty:
        # One shot has no seams and therefore no pacing to be wrong about.
        why.append("pacing 1.00: a single shot has no rhythm to judge")
        return 1.0

    arbitrary = sum(1 for seam in reading.seams if seam.arbitrary)
    value = 1.0 - arbitrary / len(reading.seams)

    metronome = max(0, reading.longest_flat_run - METRONOME_RUN + 1)
    if metronome:
        value -= metronome / len(moments)
    value = max(0.0, value)

    why.append(
        f"pacing {value:.2f}: {arbitrary} of {len(reading.seams)} cuts change "
        f"the shot length without changing what the shot is doing"
        + (
            f"; {reading.longest_flat_run} shots in a row never change length"
            if metronome
            else ""
        )
        + f" (clips run {min(lengths):.0f}-{max(lengths):.0f}s)"
    )
    return value


def _intensity(moments: Sequence[Any], reader: SemanticReader | None, why: list[str]) -> float:
    """How hot the selected footage is, on the session's own scale."""
    if reader is None:
        value = sum(moment.score for moment in moments) / len(moments)
        why.append(f"intensity {value:.2f}: from moment scores (no session lanes)")
        return min(1.0, value)
    readings = [
        reader.intensity_between(moment.context_start, moment.context_end)
        for moment in moments
    ]
    value = sum(readings) / len(readings)
    why.append(f"intensity {value:.2f}: mean heat of the selected footage")
    return min(1.0, value)


def _variety(moments: Sequence[Any], why: list[str]) -> float:
    """How many kinds of thing happen, and how evenly."""
    counts: dict[str, int] = {}
    for moment in moments:
        key = getattr(moment.moment_type, "value", str(moment.moment_type))
        counts[key] = counts.get(key, 0) + 1
    kinds = len(counts)
    dominant = max(counts.values()) / len(moments)
    value = min(1.0, kinds / 6.0) * (1.0 - max(0.0, dominant - 0.5))
    why.append(
        f"variety {value:.2f}: {kinds} kind(s), the commonest is {dominant:.0%} of the edit"
    )
    return value


def _ending(moments: Sequence[Any], reader: SemanticReader | None, why: list[str]) -> float:
    """Whether the video ends on something, or merely stops.

    "It stops rather than ends" was a real complaint about a real render, and
    it is the axis a length-optimising knapsack is least likely to serve: the
    last clip is whatever happened to fit.
    """
    last = moments[-1]
    if reader is None:
        value = min(1.0, last.score * 1.4)
        why.append(f"ending {value:.2f}: last clip scores {last.score:.2f}")
        return value
    ending = reader.intensity_between(last.context_start, last.context_end)
    rest = [
        reader.intensity_between(moment.context_start, moment.context_end)
        for moment in moments[:-1]
    ] or [ending]
    value = min(1.0, ending / max(sum(rest) / len(rest), 1e-6) * 0.6)
    why.append(f"ending {value:.2f}: the last clip against the edit's own average")
    return value


def _effect_density(
    moments: Sequence[Any], plan: Any, why: list[str], style: Any = None
) -> float:
    """How much emphasis the footage can carry, before any is planned.

    Measured from what the moments offer -- named events are what a
    composition anchors to -- rather than from effects nobody has placed yet.
    """
    seconds = sum(moment.context_duration for moment in moments)
    anchors = sum(
        1
        for moment in moments
        for event in getattr(moment, "events", ())
        if getattr(event.event_type, "value", "") != "unknown_event"
    )
    per_minute = anchors / max(seconds / 60.0, 1e-6)
    # V2-P8: a minimal style is not a worse edit for having no effects.
    ideal = float(getattr(style, "ideal_effects_per_minute", IDEAL_EFFECTS_PER_MINUTE))
    if ideal <= 0.0:
        value = 1.0 if per_minute <= 0.05 else max(0.0, 1.0 - per_minute)
    else:
        value = max(0.0, 1.0 - abs(per_minute - ideal) / ideal)
    why.append(f"effect density {value:.2f}: {per_minute:.1f} nameable beat(s) per minute")
    return min(1.0, value)


def _audio_density(
    moments: Sequence[Any],
    reader: SemanticReader | None,
    why: list[str],
    style: Any = None,
) -> float:
    """How much of the edit somebody is talking over.

    All speech is a podcast; none is a silent film. Neither is wrong, and the
    axis only says how far this plan sits from the middle.
    """
    if reader is None:
        why.append("audio density 0.50: no session lanes to read speech from")
        return 0.5
    share: list[float] = []
    for moment in moments:
        window = reader.window("speech", moment.context_start, moment.context_end)
        share.append(sum(window) / len(window) if window else 0.0)
    spoken = sum(share) / len(share)
    ideal = float(getattr(style, "ideal_speech_share", IDEAL_SPEECH_SHARE))
    value = max(0.0, 1.0 - abs(spoken - ideal) / max(ideal, 1e-6))
    why.append(f"audio density {value:.2f}: somebody is talking over {spoken:.0%} of it")
    return min(1.0, value)


def best(
    scored: Sequence[tuple[Any, Any, PlanScore]],
) -> tuple[Any, Any, PlanScore] | None:
    """The winner, chosen reproducibly.

    Totals are compared at three decimals. Two plans that differ in the
    seventh place are the same plan as far as any viewer is concerned, and
    letting that decide meant the same session picked A on one run and C on
    the next -- a judge whose answer moves is not a judge. Genuine ties fall
    to the profile order, which is fixed.
    """
    if not scored:
        return None
    return min(
        scored,
        key=lambda item: (-round(item[2].total, 3), item[0].id),
    )


__all__ = ["AXIS_WEIGHTS", "METRONOME_RUN", "PlanScore", "best", "judge"]
