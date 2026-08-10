"""Repetition and variety (SPEC sections 31, 33).

§31: *detect repeated kills, deaths, attempts, jokes, situations, reactions.
Keep the strongest representative examples.*

The word that matters is **strongest**, not *first*. Chronological order is the
obvious way to thin duplicates and it is the wrong one: the fifth attempt at a
boss is usually the one that worked, and the first is somebody dying to a
mechanic they had not learned yet. Keeping the earliest occurrence of each kind
produces a video of first drafts.

§33 is the companion rule, and it is a warning about scoring:

    The highest score is not necessarily the best clip. The system must
    consider story, context, progression, variety and pacing.

So variety here is not a filter that deletes moments. It is a **saturation
penalty** fed back into the score: once a type dominates what has been picked,
the next moment of that type is worth less *to this video* than its own merits
suggest. Twelve excellent kills in a row is a worse video than eight kills, two
fails and a funny moment — and no per-moment score can express that, because
the cost only exists relative to the selection.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.config.schema import RepetitionConfig, VarietyConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import MomentType
from backend.moments.formation import Moment

logger = get_logger("moments.repetition", LogChannel.PIPELINE)


@dataclass(frozen=True, slots=True)
class RepetitionGroup:
    """Moments that are the same thing happening again."""

    signature: str
    members: tuple[Moment, ...]
    kept: tuple[Moment, ...]
    dropped: tuple[Moment, ...]

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(frozen=True, slots=True)
class RepetitionResult:
    """What the repetition pass concluded."""

    groups: tuple[RepetitionGroup, ...] = ()
    #: Moment identity (media, start) to its repetition penalty, 0-1.
    scores: dict[tuple[str, float], float] = field(default_factory=dict)

    def score_for(self, moment: Moment) -> float:
        return self.scores.get((moment.media_id, round(moment.start_seconds, 3)), 0.0)

    @property
    def repeated_moments(self) -> int:
        return sum(group.size for group in self.groups)


def detect_repetition(
    moments: Sequence[Moment], config: RepetitionConfig
) -> RepetitionResult:
    """Group moments that repeat, and score how redundant each one is (§31).

    Nothing is deleted. Each moment gets a ``repetition_score`` that §32 uses
    as a penalty, and the group records which members are the strongest — so
    the narrative stage can drop the weakest with the reasoning available
    rather than inferring it.
    """
    if not config.enabled or len(moments) < 2:
        return RepetitionResult()

    groups: list[RepetitionGroup] = []
    scores: dict[tuple[str, float], float] = {}
    remaining = list(moments)

    while remaining:
        candidate = remaining.pop(0)
        similar = [
            other
            for other in remaining
            if _similarity(candidate, other, config) >= config.similarity_threshold
        ]
        if not similar:
            continue
        for member in similar:
            remaining.remove(member)

        members = (candidate, *similar)
        ranked = _rank(members, config)
        kept = ranked[: max(config.max_repeats_kept, 1)]
        dropped = ranked[max(config.max_repeats_kept, 1) :]

        groups.append(
            RepetitionGroup(
                signature=_signature(candidate),
                members=members,
                kept=tuple(kept),
                dropped=tuple(dropped),
            )
        )
        # The penalty grows with position: the strongest representative is
        # barely penalised, the fifth near-identical clip heavily.
        for position, moment in enumerate(ranked):
            key = (moment.media_id, round(moment.start_seconds, 3))
            scores[key] = round(min(position / max(len(ranked) - 1, 1), 1.0), 4)

    result = RepetitionResult(groups=tuple(groups), scores=scores)
    logger.info(
        "Detected repetition",
        extra={
            "moments": len(moments),
            "groups": len(groups),
            "repeated_moments": result.repeated_moments,
            "dropped_candidates": sum(len(group.dropped) for group in groups),
        },
    )
    return result


def _rank(members: Sequence[Moment], config: RepetitionConfig) -> list[Moment]:
    """Order a repetition group best-first.

    ``highest_score`` is the configured default and the right one: §31 says to
    keep the strongest examples, and the fifth attempt at a boss is usually the
    one worth watching.
    """
    if config.keep_strategy == "first":
        return sorted(members, key=lambda moment: moment.start_seconds)
    if config.keep_strategy == "last":
        return sorted(members, key=lambda moment: -moment.start_seconds)
    return sorted(
        members,
        key=lambda moment: (moment.score, moment.confidence, -moment.start_seconds),
        reverse=True,
    )


def _similarity(first: Moment, second: Moment, config: RepetitionConfig) -> float:
    """How alike two moments are, 0-1.

    Compared on the axes ``repetition.compare`` names, averaged. Each is a weak
    signal on its own -- two kills are always "the same event type" -- so
    agreement across axes is what makes a pair a repeat.
    """
    axes = set(config.compare)
    parts: list[float] = []

    if "event_type" in axes:
        parts.append(_type_similarity(first, second))
    if "visual_similarity" in axes:
        parts.append(_label_similarity(first, second))
    if "audio_similarity" in axes:
        parts.append(_source_similarity(first, second))
    if "transcript_similarity" in axes:
        parts.append(_text_similarity(first, second))

    if not parts:
        return _type_similarity(first, second)
    return sum(parts) / len(parts)


def _type_similarity(first: Moment, second: Moment) -> float:
    if first.moment_type is not second.moment_type:
        return 0.0
    left = set(first.event_types)
    right = set(second.event_types)
    if not left or not right:
        return 1.0
    return len(left & right) / len(left | right)


def _label_similarity(first: Moment, second: Moment) -> float:
    left = _labels(first)
    right = _labels(second)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _source_similarity(first: Moment, second: Moment) -> float:
    left, right = set(first.sources), set(second.sources)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _text_similarity(first: Moment, second: Moment) -> float:
    left, right = _text(first), _text(second)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _labels(moment: Moment) -> set[str]:
    return {
        str(entry.get("label", ""))
        for event in moment.events
        for entry in event.metadata.get("detail", [])
        if isinstance(entry, dict) and entry.get("label")
    }


def _text(moment: Moment) -> str:
    return " ".join(
        str(entry.get("text", ""))
        for event in moment.events
        for entry in event.metadata.get("detail", [])
        if isinstance(entry, dict) and entry.get("text")
    ).lower()


def _signature(moment: Moment) -> str:
    return f"{moment.moment_type.value}:{'+'.join(sorted({e.value for e in moment.event_types}))}"


# ---------------------------------------------------------------------------
# variety (§33)
# ---------------------------------------------------------------------------


def saturation_penalties(
    moments: Sequence[Moment], config: VarietyConfig
) -> dict[tuple[str, float], float]:
    """How much each moment is devalued by its type already dominating (§33).

    Walks the moments best-first, tracking what has been picked. Once a type
    exceeds ``max_same_type_ratio`` of the selection so far, further moments of
    that type carry ``saturation_penalty``.

    A *penalty*, never a rejection. The twelfth kill may still be the best clip
    in the recording, and §33's point is that the selector should have to
    justify picking it -- not that it is forbidden.
    """
    if not config.enabled or not moments:
        return {}

    ranked = sorted(moments, key=lambda moment: moment.score, reverse=True)
    counts: dict[MomentType, int] = {}
    penalties: dict[tuple[str, float], float] = {}

    for position, moment in enumerate(ranked, start=1):
        share = counts.get(moment.moment_type, 0) / position
        key = (moment.media_id, round(moment.start_seconds, 3))
        penalties[key] = config.saturation_penalty if share > config.max_same_type_ratio else 0.0
        counts[moment.moment_type] = counts.get(moment.moment_type, 0) + 1

    return penalties


def variety_report(moments: Sequence[Moment], config: VarietyConfig) -> dict[str, Any]:
    """Whether a selection is varied enough to watch (§33).

    Reported rather than enforced. The narrative stage owns selection, and this
    is the evidence it needs to know when a shortlist has become monotonous.
    """
    counts: dict[str, int] = {}
    for moment in moments:
        counts[moment.moment_type.value] = counts.get(moment.moment_type.value, 0) + 1

    total = max(len(moments), 1)
    dominant = max(counts.values(), default=0) / total
    return {
        "moments": len(moments),
        "distinct_types": len(counts),
        "by_type": counts,
        "dominant_share": round(dominant, 3),
        "meets_minimum_types": len(counts) >= config.min_distinct_types,
        "within_type_ratio": dominant <= config.max_same_type_ratio,
    }


__all__ = [
    "RepetitionGroup",
    "RepetitionResult",
    "detect_repetition",
    "saturation_penalties",
    "variety_report",
]
