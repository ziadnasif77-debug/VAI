"""Dead time (SPEC section 30).

    Detect walking, waiting, loading, menus, inventory, travel, AFK, long
    silence, repetitive farming, repeated failed attempts. Each segment gets a
    ``dead_time_score``. **Removed only when removal does not damage context.**

The last sentence is the module. Finding boring stretches is easy — a long
silence with no events in it is dead time by any measure. Knowing when *not* to
cut one is the part that decides whether the output looks edited or looks
chopped.

Two rules keep it honest:

* **Dead time is scored, not deleted.** This module marks; the narrative stage
  (§37) decides. A stretch that is 90 % dead time may still be the only bridge
  between two moments the story needs.
* **A segment adjacent to a kept moment is protected.** The walk *up to* the
  ambush is what makes the ambush land. Cutting straight from a kill to the
  next kill is exactly the "highlight reel with no breathing room" §37 warns
  about, and it is what removing dead time naively produces.

Scoring is evidence-based: silence, absence of events, absence of speech, and
absence of visual change. The **category** is a weaker claim than the score, so
an unrecognisable stretch is `WAITING` rather than a guess at `REPETITIVE_FARMING`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ai.providers.base import StoredObservation, TranscriptSegment
from backend.analysis.audio_events import AudioEvent
from backend.config.schema import DeadTimeConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import AudioEventType, DeadTimeCategory
from backend.moments.formation import Moment

logger = get_logger("moments.dead_time", LogChannel.PIPELINE)

#: Vision labels that name a dead-time category outright. The model reporting
#: "loading" is a far stronger claim than an absence of evidence.
_LABEL_CATEGORIES: Final[dict[str, DeadTimeCategory]] = {
    "loading": DeadTimeCategory.LOADING,
    "menu": DeadTimeCategory.MENU,
    "inventory": DeadTimeCategory.INVENTORY,
    "traversal": DeadTimeCategory.WALKING,
    "driving": DeadTimeCategory.TRAVEL,
    "map": DeadTimeCategory.MENU,
}


@dataclass(frozen=True, slots=True)
class DeadSegment:
    """A stretch of recording with little in it."""

    start_seconds: float
    end_seconds: float
    category: DeadTimeCategory
    #: 0-1. How dead this stretch is, not how confident we are that it exists.
    score: float
    #: True when a kept moment needs this stretch and it must survive (§30).
    protected: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds

    def overlaps(self, start: float, end: float) -> bool:
        return self.start_seconds < end and start < self.end_seconds

    @property
    def removable(self) -> bool:
        """Whether cutting this would not damage context."""
        return not self.protected


def detect_dead_time(
    duration_seconds: float,
    config: DeadTimeConfig,
    *,
    moments: Sequence[Moment] = (),
    audio_events: Sequence[AudioEvent] = (),
    transcript: Sequence[TranscriptSegment] = (),
    vision: Sequence[StoredObservation] = (),
) -> list[DeadSegment]:
    """Find and score the stretches worth removing (§30).

    Args:
        duration_seconds: source length. The gaps *between* moments are the
            search space, so this bounds the last one.
        moments: what the analysis found worth keeping. Everything between them
            is a candidate, and everything adjacent to them is protected.

    Returns segments in chronological order, each scored and each marked with
    whether removing it would damage context.
    """
    if not config.enabled or duration_seconds <= 0:
        return []

    gaps = _gaps_between(moments, duration_seconds, config.min_segment_seconds)
    segments: list[DeadSegment] = []
    for start, end in gaps:
        category, score, evidence = _classify(
            start, end, config, audio_events, transcript, vision
        )
        if score <= 0.0:
            continue
        segments.append(
            DeadSegment(
                start_seconds=start,
                end_seconds=end,
                category=category,
                score=score,
                evidence=evidence,
            )
        )

    protected = _protect(segments, moments, config)
    logger.info(
        "Detected dead time",
        extra={
            "segments": len(protected),
            "protected": sum(1 for item in protected if item.protected),
            "removable_seconds": round(
                sum(item.duration for item in protected if item.removable), 2
            ),
            "duration_seconds": round(duration_seconds, 2),
        },
    )
    return protected


def _gaps_between(
    moments: Sequence[Moment], duration: float, minimum: float
) -> list[tuple[float, float]]:
    """The stretches no moment occupies.

    Measured against each moment's **context** span, not its core: the pre-roll
    is already part of a clip, and treating it as dead time would mean the same
    seconds were both kept and cut.
    """
    if not moments:
        return [(0.0, duration)] if duration >= minimum else []

    spans = sorted(
        (moment.context_start, moment.context_end) for moment in moments
    )
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start - cursor >= minimum:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= minimum:
        gaps.append((cursor, duration))
    return gaps


def _classify(
    start: float,
    end: float,
    config: DeadTimeConfig,
    audio_events: Sequence[AudioEvent],
    transcript: Sequence[TranscriptSegment],
    vision: Sequence[StoredObservation],
) -> tuple[DeadTimeCategory, float, dict[str, Any]]:
    """Score a stretch and name it as specifically as the evidence allows."""
    span = max(end - start, 1e-6)

    silence = sum(
        _overlap(event.start_seconds, event.end_seconds, start, end)
        for event in audio_events
        if event.event_type is AudioEventType.SILENCE
    )
    speech = sum(
        _overlap(segment.start, segment.end, start, end) for segment in transcript
    )
    activity = sum(
        1
        for event in audio_events
        if event.event_type in {AudioEventType.SPIKE, AudioEventType.TRANSIENT}
        and start <= event.start_seconds < end
    )
    labels = [
        label
        for item in vision
        if start <= item.timestamp < end
        for label in item.labels
    ]

    silence_ratio = min(silence / span, 1.0)
    speech_ratio = min(speech / span, 1.0)

    # Dead means: quiet, nothing happening, nobody talking. Each is weak alone
    # and together they are the definition.
    score = (
        0.45 * silence_ratio
        + 0.35 * (1.0 if activity == 0 else max(0.0, 1.0 - activity / 4.0))
        + 0.20 * (1.0 - speech_ratio)
    )

    category = DeadTimeCategory.WAITING
    for label in labels:
        named = _LABEL_CATEGORIES.get(label)
        if named is not None:
            category = named
            # A model reporting "loading" is a much stronger claim than an
            # absence of evidence, so it raises the score as well as naming it.
            score = max(score, 0.8)
            break
    else:
        if silence >= config.silence_seconds_for_dead_time:
            category = DeadTimeCategory.LONG_SILENCE
        elif speech_ratio > 0.3:
            # Talking through a quiet stretch is commentary, not dead air, and
            # the §37 pacing pass may well want it.
            score *= 0.6

    return (
        category,
        round(min(max(score, 0.0), 1.0), 4),
        {
            "silence_ratio": round(silence_ratio, 3),
            "speech_ratio": round(speech_ratio, 3),
            "audio_events": activity,
            "labels": sorted(set(labels))[:6],
        },
    )


def _protect(
    segments: Sequence[DeadSegment], moments: Sequence[Moment], config: DeadTimeConfig
) -> list[DeadSegment]:
    """Mark the dead time a kept moment depends on (§30).

    The seconds immediately before a moment are its run-up. Cutting them makes
    the moment arrive with no preparation, which is the "no breathing room"
    failure §37 names — so a bounded amount adjacent to a moment survives even
    though it is, by measurement, dead.
    """
    if not config.protect_context or not moments:
        return list(segments)

    limit = config.max_protected_seconds
    starts = sorted(moment.context_start for moment in moments)
    ends = sorted(moment.context_end for moment in moments)

    protected: list[DeadSegment] = []
    for segment in segments:
        touches_lead_in = any(
            0.0 <= start - segment.end_seconds <= limit for start in starts
        )
        touches_tail = any(0.0 <= segment.start_seconds - end <= limit for end in ends)
        protected.append(
            DeadSegment(
                start_seconds=segment.start_seconds,
                end_seconds=segment.end_seconds,
                category=segment.category,
                score=segment.score,
                protected=touches_lead_in or touches_tail,
                evidence={**segment.evidence, "protects": _protects(touches_lead_in, touches_tail)},
            )
        )
    return protected


def _protects(lead_in: bool, tail: bool) -> str | None:
    """Which side of a moment this segment is protecting, if any."""
    if lead_in:
        return "lead-in"
    return "tail" if tail else None


def dead_time_ratio(
    moment: Moment, segments: Sequence[DeadSegment]
) -> float:
    """How much of a moment's viewing span is dead (§32's penalty input).

    A moment whose context is mostly dead air is a moment that will feel slow,
    and §32 penalises it — but the moment is not discarded, because the events
    inside it are still real.
    """
    if moment.context_duration <= 0:
        return 0.0
    dead = sum(
        _overlap(item.start_seconds, item.end_seconds, moment.context_start, moment.context_end)
        * item.score
        for item in segments
    )
    return round(min(dead / moment.context_duration, 1.0), 4)


def _overlap(start: float, end: float, window_start: float, window_end: float) -> float:
    return max(0.0, min(end, window_end) - max(start, window_start))


__all__ = ["DeadSegment", "dead_time_ratio", "detect_dead_time"]
