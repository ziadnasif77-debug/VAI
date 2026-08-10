"""Pacing (SPEC section 38).

    Pacing depends on event frequency, clip duration, dead time, audio
    intensity, visual intensity, reaction frequency, narrative position.

Pacing is what separates an edit from a playlist. The same clips in the same
order can be watchable or exhausting depending on how they are spaced, and the
failures are specific and recognisable:

* **Monotony** — four clips of the same kind in a row. The viewer stops
  distinguishing them and starts skipping.
* **Flatness** — a long run of low-intensity clips with no relief. This is where
  people leave.
* **No arc** — the best moment first and everything downhill after it. The
  configured ``intensity_curve: rising`` says the opposite: build.

This module orders a selection and then **reports** on it. It reorders within
the constraints it can satisfy and does not silently drop clips to make a curve
work — the selection came from §39's optimiser, which balanced duration against
value, and quietly discarding part of it here would break that guarantee.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Final

from backend.config.schema import PacingConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import MomentType
from backend.moments.formation import Moment

logger = get_logger("narrative.pacing", LogChannel.PIPELINE)

#: How intense a moment type feels, 0-1. Used for the arc and for detecting
#: long flat stretches. A judgement about *kind*, refined per moment by the
#: audio and reaction dimensions the scorer already measured.
TYPE_INTENSITY: Final[dict[MomentType, float]] = {
    MomentType.EPIC: 1.0,
    MomentType.CHAOS: 0.95,
    MomentType.CLUTCH: 0.9,
    MomentType.OUTPLAY: 0.85,
    MomentType.COMEBACK: 0.85,
    MomentType.BOSS: 0.8,
    MomentType.RAGE: 0.8,
    MomentType.VICTORY: 0.75,
    MomentType.SURPRISE: 0.7,
    MomentType.FUNNY: 0.65,
    MomentType.SKILL: 0.6,
    MomentType.FAIL: 0.55,
    MomentType.DEFEAT: 0.5,
    MomentType.REACTION: 0.5,
    MomentType.TENSION: 0.45,
    MomentType.DISCOVERY: 0.4,
    MomentType.RARE: 0.6,
}


@dataclass(frozen=True, slots=True)
class PacingReport:
    """How the ordered selection actually paces."""

    clips: int
    total_seconds: float
    clips_per_minute: float
    mean_intensity: float
    #: Longest run of clips of the same type.
    longest_same_type_run: int
    #: Longest stretch of low-intensity clips, in seconds.
    longest_flat_seconds: float
    #: Whether intensity trends upward across the video.
    rises: bool
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return not self.warnings

    def summary(self) -> dict[str, Any]:
        return {
            "clips": self.clips,
            "total_seconds": round(self.total_seconds, 2),
            "clips_per_minute": round(self.clips_per_minute, 2),
            "mean_intensity": round(self.mean_intensity, 3),
            "longest_same_type_run": self.longest_same_type_run,
            "longest_flat_seconds": round(self.longest_flat_seconds, 2),
            "rises": self.rises,
            "warnings": list(self.warnings),
        }


def order(moments: Sequence[Moment], config: PacingConfig) -> list[Moment]:
    """Break up runs of the same clip type, preserving the given order (§38).

    The **caller's order is the base**, not chronology. Each §35 mode has
    already decided its sequence for a reason -- story follows an arc, best
    moments runs strongest-first, compilation groups by type -- and re-sorting
    here would silently make all three modes identical. (It did, once.)

    So this only does local swaps: when a run of one type exceeds the limit, the
    nearest differently-typed clip within a few positions takes the slot.
    Anything wider would be a reordering, which is the mode's decision to make.
    """
    ordered = list(moments)
    if len(ordered) < 3 or config.max_consecutive_same_type <= 0:
        return ordered

    limit = config.max_consecutive_same_type
    result: list[Moment] = []
    run_type: MomentType | None = None
    run = 0

    pending = list(ordered)
    while pending:
        candidate = pending.pop(0)
        if candidate.moment_type is run_type and run >= limit:
            # Look ahead for something different that can take this slot, and
            # only a short way ahead so the timeline stays broadly in order.
            swap = next(
                (
                    index
                    for index, item in enumerate(pending[:3])
                    if item.moment_type is not run_type
                ),
                None,
            )
            if swap is not None:
                pending.insert(0, candidate)
                candidate = pending.pop(swap + 1)

        result.append(candidate)
        if candidate.moment_type is run_type:
            run += 1
        else:
            run_type, run = candidate.moment_type, 1
    return result


def report(moments: Sequence[Moment], config: PacingConfig) -> PacingReport:
    """Measure the pacing of an ordered selection (§38).

    Warnings, not corrections. The narrative stage records them and the review
    screen shows them, because "this section is flat" is a judgement a human may
    reasonably disagree with -- and §78 gives them the last word.
    """
    if not moments:
        return PacingReport(
            clips=0,
            total_seconds=0.0,
            clips_per_minute=0.0,
            mean_intensity=0.0,
            longest_same_type_run=0,
            longest_flat_seconds=0.0,
            rises=False,
            warnings=("no clips to pace",),
        )

    total = sum(moment.context_duration for moment in moments)
    intensities = [intensity_of(moment) for moment in moments]
    per_minute = len(moments) / max(total / 60.0, 1e-6)

    warnings: list[str] = []
    longest_run = _longest_run(moments)
    if longest_run > config.max_consecutive_same_type:
        warnings.append(
            f"{longest_run} clips of the same type run consecutively "
            f"(limit {config.max_consecutive_same_type})"
        )

    flat = _longest_flat(moments, intensities)
    if flat > config.max_consecutive_low_intensity_seconds:
        warnings.append(
            f"{flat:.0f}s of low-intensity clips in a row "
            f"(limit {config.max_consecutive_low_intensity_seconds:.0f}s)"
        )

    short = [m for m in moments if m.context_duration < config.min_clip_seconds]
    if short:
        warnings.append(f"{len(short)} clip(s) shorter than the minimum")
    long_clips = [m for m in moments if m.context_duration > config.max_clip_seconds]
    if long_clips:
        warnings.append(f"{len(long_clips)} clip(s) longer than the maximum")

    rises = _rises(intensities)
    if config.intensity_curve == "rising" and not rises:
        warnings.append("intensity does not build across the video")

    if per_minute < config.target_clips_per_minute * 0.5:
        warnings.append(f"only {per_minute:.1f} clips per minute; the edit may drag")

    result = PacingReport(
        clips=len(moments),
        total_seconds=total,
        clips_per_minute=per_minute,
        mean_intensity=sum(intensities) / len(intensities),
        longest_same_type_run=longest_run,
        longest_flat_seconds=flat,
        rises=rises,
        warnings=tuple(warnings),
        metadata={"target_clips_per_minute": config.target_clips_per_minute},
    )
    (logger.warning if warnings else logger.info)("Measured pacing", extra=result.summary())
    return result


def intensity_of(moment: Moment) -> float:
    """How intense one clip feels, 0-1 (§38).

    The type's inherent intensity, adjusted by what the scorer measured about
    this particular clip -- its audio energy and whether the player reacted.
    A quiet epic moment and a loud one are not the same clip to watch.
    """
    base = TYPE_INTENSITY.get(moment.moment_type, 0.5)
    breakdown = moment.score_breakdown
    audio = float(breakdown.get("audio", 0.5))
    reaction = float(breakdown.get("reaction", 0.5))
    return round(min(0.6 * base + 0.25 * audio + 0.15 * reaction, 1.0), 4)


def _longest_run(moments: Sequence[Moment]) -> int:
    longest = current = 1
    for previous, moment in pairwise(moments):
        current = current + 1 if moment.moment_type is previous.moment_type else 1
        longest = max(longest, current)
    return longest


def _longest_flat(moments: Sequence[Moment], intensities: Sequence[float]) -> float:
    """Longest continuous stretch of below-average-intensity clips."""
    threshold = sum(intensities) / len(intensities) * 0.75
    longest = current = 0.0
    for moment, intensity in zip(moments, intensities, strict=True):
        if intensity < threshold:
            current += moment.context_duration
            longest = max(longest, current)
        else:
            current = 0.0
    return longest


def _rises(intensities: Sequence[float]) -> bool:
    """Whether the second half is more intense than the first.

    A coarse test on purpose. A strict monotonic climb would be a worse video
    than one that breathes -- §38 asks for a curve, not a ramp.
    """
    if len(intensities) < 4:
        return True
    midpoint = len(intensities) // 2
    first = sum(intensities[:midpoint]) / midpoint
    second = sum(intensities[midpoint:]) / (len(intensities) - midpoint)
    return second >= first


__all__ = ["TYPE_INTENSITY", "PacingReport", "intensity_of", "order", "report"]
