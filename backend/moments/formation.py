"""Moment formation (SPEC sections 28, 34).

§28 describes the shape of a moment, and the description is the specification:

    setup → enemy appears → combat → kill → reaction → victory

One moment, six events. That is the whole idea — a moment is a **story
fragment**, not an instant, and the events inside it are its beats. §27 already
merged the detectors that saw the *same* instant; this merges the instants that
belong to the same *thing happening*.

The two failure modes are opposite and both ruinous. Group too eagerly and a
whole firefight becomes one 3-minute "moment" nobody would cut; group too
timidly and a kill and the shout that followed it become two clips of the same
event, one of which is two seconds of somebody reacting to nothing.

The gap that separates them is configurable (``formation.max_event_gap_seconds``)
because it is genuinely game-dependent: a round-based shooter breathes
differently from an open-world game.

The moment's **type** comes from its events, ranked by how much a type says.
A moment containing a multi-kill and three audio spikes is a multi-kill; the
spikes are what it sounded like.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.analysis import frame_state
from backend.analysis.frame_state import StateSpan
from backend.config.schema import FormationConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import GameEventType, MomentType
from backend.gaming.correlation import GameEvent

logger = get_logger("moments.formation", LogChannel.PIPELINE)

#: Which §34 moment type an event implies. Ordered by specificity when several
#: apply: see :data:`_TYPE_PRIORITY`.
EVENT_TO_MOMENT: Final[dict[GameEventType, MomentType]] = {
    GameEventType.MULTI_KILL: MomentType.EPIC,
    GameEventType.CLUTCH: MomentType.CLUTCH,
    GameEventType.COMEBACK: MomentType.COMEBACK,
    GameEventType.OUTPLAY: MomentType.OUTPLAY,
    GameEventType.BOSS_DEFEAT: MomentType.BOSS,
    GameEventType.BOSS_FIGHT: MomentType.BOSS,
    GameEventType.VICTORY: MomentType.VICTORY,
    GameEventType.DEFEAT: MomentType.DEFEAT,
    GameEventType.FUNNY_MOMENT: MomentType.FUNNY,
    GameEventType.FAIL: MomentType.FAIL,
    GameEventType.RARE_LOOT: MomentType.RARE,
    GameEventType.RARE_EVENT: MomentType.RARE,
    GameEventType.KILL: MomentType.SKILL,
    GameEventType.NEAR_DEATH: MomentType.TENSION,
    GameEventType.LOW_HEALTH: MomentType.TENSION,
    GameEventType.ESCAPE: MomentType.CLUTCH,
    GameEventType.CHASE: MomentType.TENSION,
    GameEventType.DEATH: MomentType.FAIL,
    GameEventType.OBJECTIVE: MomentType.SKILL,
    GameEventType.OBJECTIVE_FAILURE: MomentType.FAIL,
    GameEventType.HIGH_DAMAGE: MomentType.SKILL,
    GameEventType.COMBAT: MomentType.CHAOS,
    GameEventType.COLLISION: MomentType.CHAOS,
    GameEventType.UNEXPECTED_EVENT: MomentType.SURPRISE,
}

#: How much a type *claims*. When a moment contains several kinds of event, the
#: most specific wins — a multi-kill with an unnamed audio spike beside it is a
#: multi-kill, not a surprise. ``SURPRISE`` sits last precisely because it is
#: what the pipeline says when it cannot say more (§23).
_TYPE_PRIORITY: Final[tuple[MomentType, ...]] = (
    MomentType.CLUTCH,
    MomentType.EPIC,
    MomentType.COMEBACK,
    MomentType.OUTPLAY,
    MomentType.BOSS,
    MomentType.VICTORY,
    MomentType.DEFEAT,
    MomentType.FUNNY,
    MomentType.RARE,
    MomentType.SKILL,
    MomentType.FAIL,
    MomentType.TENSION,
    MomentType.REACTION,
    MomentType.CHAOS,
    MomentType.DISCOVERY,
    MomentType.RAGE,
    MomentType.SURPRISE,
)


@dataclass(frozen=True, slots=True)
class Moment:
    """A candidate clip: a span, the events inside it, and why it is one.

    ``start``/``end`` are the events themselves; ``context_start``/
    ``context_end`` are what a viewer would actually be shown after §29
    expansion. Both are carried because they answer different questions —
    "when did it happen" and "where should the clip begin".
    """

    media_id: str
    moment_type: MomentType
    start_seconds: float
    end_seconds: float
    events: tuple[GameEvent, ...]

    #: Set by :mod:`backend.moments.context`. Equal to the core span until then.
    context_start: float = 0.0
    context_end: float = 0.0

    #: Set by :mod:`backend.moments.scoring`.
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    explanation: tuple[str, ...] = ()

    #: Set by the dead-time and repetition passes (§30, §31).
    dead_time_score: float = 0.0
    repetition_score: float = 0.0

    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def context_duration(self) -> float:
        return self.context_end - self.context_start

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0

    @property
    def confidence(self) -> float:
        """The moment is as confident as its most confident event.

        Not an average: a certain kill next to a doubtful audio spike is a
        certain kill that happened to be noisy, and averaging would punish it
        for the corroboration.
        """
        return max((event.confidence for event in self.events), default=0.0)

    @property
    def importance(self) -> float:
        return max((event.importance for event in self.events), default=0.0)

    @property
    def sources(self) -> tuple[str, ...]:
        """Every detector behind any of this moment's events."""
        return tuple(sorted({source for event in self.events for source in event.sources}))

    @property
    def event_types(self) -> tuple[GameEventType, ...]:
        return tuple(event.event_type for event in self.events)

    def overlaps(self, other: Moment) -> bool:
        return (
            self.context_start < other.context_end
            and other.context_start < self.context_end
        )

    def with_context(self, start: float, end: float) -> Moment:
        return replace_moment(self, context_start=start, context_end=end)

    def with_score(
        self,
        score: float,
        breakdown: dict[str, float],
        explanation: Sequence[str],
        *,
        dead_time: float = 0.0,
        repetition: float = 0.0,
    ) -> Moment:
        return replace_moment(
            self,
            score=score,
            score_breakdown=dict(breakdown),
            explanation=tuple(explanation),
            dead_time_score=dead_time,
            repetition_score=repetition,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "type": self.moment_type.value,
            "start": round(self.start_seconds, 3),
            "end": round(self.end_seconds, 3),
            "context": [round(self.context_start, 3), round(self.context_end, 3)],
            "events": len(self.events),
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
        }


def replace_moment(moment: Moment, **updates: Any) -> Moment:
    """Rebuild a frozen moment with some fields changed."""
    values = {
        "media_id": moment.media_id,
        "moment_type": moment.moment_type,
        "start_seconds": moment.start_seconds,
        "end_seconds": moment.end_seconds,
        "events": moment.events,
        "context_start": moment.context_start,
        "context_end": moment.context_end,
        "score": moment.score,
        "score_breakdown": moment.score_breakdown,
        "explanation": moment.explanation,
        "dead_time_score": moment.dead_time_score,
        "repetition_score": moment.repetition_score,
        "metadata": moment.metadata,
    }
    values.update(updates)
    return Moment(**values)


def form_moments(
    events: Iterable[GameEvent],
    config: FormationConfig,
    *,
    media_id: str,
    non_gameplay: Sequence[StateSpan] = (),
) -> list[Moment]:
    """Group correlated events into moments (§28).

    Events closer than ``max_event_gap_seconds`` belong to the same thing
    happening. A group longer than ``max_moment_seconds`` is split at its widest
    internal gap rather than truncated: a three-minute firefight is several
    moments, and cutting it at an arbitrary two minutes would end a clip
    mid-action.

    Groups shorter than ``min_moment_seconds`` are kept and widened by the
    context pass, not dropped — a half-second kill is a real moment that needs
    surrounding, and dropping it here would lose exactly the clips a highlight
    video is made of.

    ``non_gameplay`` are the stretches the vision model said were menus,
    loading screens or cutscenes (Phase 0.6). A moment whose **core** is mostly
    one of those is dropped here: an audio spike inside a pause menu is a real
    spike and not a moment, and the finished video carried 40 of them before
    this argument existed. Context may still touch a menu — a loading screen on
    either side of a mission start is honest — so only the core is measured.
    """
    ordered = sorted(events, key=lambda event: event.start_seconds)
    if not ordered:
        return []

    groups: list[list[GameEvent]] = []
    for event in ordered:
        if groups and event.start_seconds - _group_end(groups[-1]) <= config.max_event_gap_seconds:
            groups[-1].append(event)
        else:
            groups.append([event])

    moments: list[Moment] = []
    for group in groups:
        moments.extend(_build(group, config, media_id))

    kept, rejected = _drop_non_gameplay(moments, non_gameplay, config)

    logger.info(
        "Formed moments",
        extra={
            "media_id": media_id,
            "events": len(ordered),
            "groups": len(groups),
            "moments": len(kept),
            "rejected_non_gameplay": rejected,
            "by_type": _tally(kept),
        },
    )
    return kept


def _drop_non_gameplay(
    moments: Sequence[Moment],
    spans: Sequence[StateSpan],
    config: FormationConfig,
) -> tuple[list[Moment], int]:
    """Drop moments whose core sits inside a menu, loading or cutscene run.

    Measured as the longest *unbroken* non-gameplay stretch, not the total:
    scattered single frames reading ``menu`` inside continuous action are a
    transient overlay or a misread, while an unbroken block is the loading
    screen this guard exists to keep out.

    The overlap is recorded on every survivor, not only on the rejected: a
    moment that is a quarter menu is worth knowing about in review (§80), and
    the number is what makes the threshold arguable rather than magic.
    """
    if not spans or config.max_non_gameplay_core_overlap >= 1.0:
        return list(moments), 0

    kept: list[Moment] = []
    rejected = 0
    for moment in moments:
        ratio = frame_state.longest_overlap_ratio(
            moment.start_seconds, moment.end_seconds, spans
        )
        if ratio > config.max_non_gameplay_core_overlap:
            rejected += 1
            logger.debug(
                "Dropped a moment showing a menu or loading screen",
                extra={
                    "start": round(moment.start_seconds, 2),
                    "end": round(moment.end_seconds, 2),
                    "non_gameplay_ratio": round(ratio, 3),
                },
            )
            continue
        kept.append(
            moment
            if ratio <= 0
            else replace_moment(
                moment, metadata={**moment.metadata, "non_gameplay_ratio": round(ratio, 3)}
            )
        )
    return kept, rejected


def _build(group: list[GameEvent], config: FormationConfig, media_id: str) -> list[Moment]:
    """Turn one group of events into one or more moments."""
    start = min(event.start_seconds for event in group)
    end = max(event.end_seconds for event in group)

    if end - start > config.max_moment_seconds and len(group) > 1:
        left, right = _split(group)
        return [
            *_build(left, config, media_id),
            *_build(right, config, media_id),
        ]

    return [
        Moment(
            media_id=media_id,
            moment_type=moment_type_for(group),
            start_seconds=start,
            end_seconds=max(end, start),
            events=tuple(group),
            context_start=start,
            context_end=max(end, start),
            metadata={"event_gap_seconds": round(_widest_gap(group), 3)},
        )
    ]


def _split(group: list[GameEvent]) -> tuple[list[GameEvent], list[GameEvent]]:
    """Split at the widest internal gap — the natural seam in the action."""
    widest_index = 1
    widest = -1.0
    for index in range(1, len(group)):
        gap = group[index].start_seconds - group[index - 1].end_seconds
        if gap > widest:
            widest, widest_index = gap, index
    return group[:widest_index], group[widest_index:]


def moment_type_for(events: Sequence[GameEvent]) -> MomentType:
    """Choose the §34 type a group of events supports.

    The most specific type present wins. A group of nothing but unnamed events
    is a SURPRISE, which is the taxonomy's way of saying "something happened
    here" -- and is exactly what §23 lets the pipeline claim about a game it
    knows nothing about.
    """
    candidates = {
        EVENT_TO_MOMENT[event.event_type]
        for event in events
        if event.event_type in EVENT_TO_MOMENT
    }
    if not candidates:
        return MomentType.SURPRISE
    return next(
        (moment_type for moment_type in _TYPE_PRIORITY if moment_type in candidates),
        MomentType.SURPRISE,
    )


def _group_end(group: list[GameEvent]) -> float:
    return max(event.end_seconds for event in group)


def _widest_gap(group: Sequence[GameEvent]) -> float:
    if len(group) < 2:
        return 0.0
    return max(
        group[index].start_seconds - group[index - 1].end_seconds
        for index in range(1, len(group))
    )


def _tally(moments: Sequence[Moment]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for moment in moments:
        counts[moment.moment_type.value] = counts.get(moment.moment_type.value, 0) + 1
    return counts


__all__ = [
    "EVENT_TO_MOMENT",
    "Moment",
    "form_moments",
    "moment_type_for",
    "replace_moment",
]
