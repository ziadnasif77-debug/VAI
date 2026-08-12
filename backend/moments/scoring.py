"""Moment scoring (SPEC sections 32, 33, 79, 80, 95).

§32 lists ten dimensions and three penalties, and requires the weights to be
configurable. §33 immediately qualifies the whole exercise:

    **The highest score is not necessarily the best clip.** The system must
    consider story, context, progression, variety and pacing.

So what this module produces is not a verdict. It is a **score with its
working shown**: every dimension stored separately, every penalty stored
separately, and a human-readable explanation (§80) of why the number came out
the way it did. The narrative stage (§37) is what turns scores into a video,
and it needs the breakdown, not just the total — "this scored 0.82" tells it
nothing about whether the clip belongs in a story.

**Rule-based, and that is a requirement rather than a stage.** §95 says the
system degrades when the LLM is unavailable, and scoring is the last place that
may quietly stop working: a pipeline that produces no moments without a model
is a pipeline that does nothing on a machine without one. Every dimension here
is computed from stored evidence.

Each dimension documents what it can see and what it cannot. Several are
honestly partial — `narrative` without a story pass is position and arc; `skill`
without game knowledge is what the event types imply. Reporting a confident 0.9
for skill because a kill happened would be inventing a judgement, so those
dimensions stay conservative and say why in the explanation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from ai.providers.base import StoredObservation, TranscriptSegment
from backend.analysis.audio_events import MICROPHONE, AudioEvent
from backend.config.schema import MomentScoringConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import AudioEventType, GameEventType, MomentType
from backend.moments.formation import Moment

logger = get_logger("moments.scoring", LogChannel.PIPELINE)

#: The ten dimensions of §32, in the order the config declares them.
DIMENSIONS: Final[tuple[str, ...]] = (
    "gameplay",
    "visual",
    "audio",
    "reaction",
    "novelty",
    "skill",
    "emotion",
    "narrative",
    "context",
    "entertainment",
)

#: Event types that represent demonstrated skill rather than something that
#: merely happened. Dying is an event; a clutch is an achievement.
_SKILL_EVENTS: Final[frozenset[GameEventType]] = frozenset(
    {
        GameEventType.MULTI_KILL,
        GameEventType.CLUTCH,
        GameEventType.OUTPLAY,
        GameEventType.COMEBACK,
        GameEventType.BOSS_DEFEAT,
        GameEventType.HIGH_DAMAGE,
        GameEventType.ESCAPE,
    }
)

#: Moment types with inherent emotional weight, and how much.
_EMOTION_WEIGHT: Final[dict[MomentType, float]] = {
    MomentType.FUNNY: 0.9,
    MomentType.RAGE: 0.85,
    MomentType.CLUTCH: 0.8,
    MomentType.COMEBACK: 0.8,
    MomentType.FAIL: 0.7,
    MomentType.SURPRISE: 0.65,
    MomentType.TENSION: 0.6,
    MomentType.VICTORY: 0.6,
    MomentType.DEFEAT: 0.5,
}


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything the scorer reads. All of it already stored by earlier stages.

    Nothing here is recomputed: scoring a recording again after changing a
    weight costs milliseconds, which is what §127 requires of a re-edit.
    """

    duration_seconds: float
    audio_events: Sequence[AudioEvent] = ()
    transcript: Sequence[TranscriptSegment] = ()
    vision: Sequence[StoredObservation] = ()
    #: Per-moment penalties computed by the §30 and §31 passes.
    dead_time: dict[tuple[str, float], float] = field(default_factory=dict)
    repetition: dict[tuple[str, float], float] = field(default_factory=dict)
    saturation: dict[tuple[str, float], float] = field(default_factory=dict)
    #: Type frequencies across the whole recording, for novelty.
    type_counts: dict[MomentType, int] = field(default_factory=dict)

    def key(self, moment: Moment) -> tuple[str, float]:
        return (moment.media_id, round(moment.start_seconds, 3))


def score_moments(
    moments: Sequence[Moment], config: MomentScoringConfig, context: ScoringContext
) -> list[Moment]:
    """Score every moment and return them ranked, best first (§32).

    Ranking here is by score alone, which is exactly what §33 says is not
    enough — and is why this returns *ranked candidates* rather than a
    selection. Choosing what goes in the video is §37's job, with variety,
    pacing and story in hand.
    """
    counts = context.type_counts or _count_types(moments)
    resolved = ScoringContext(
        duration_seconds=context.duration_seconds,
        audio_events=context.audio_events,
        transcript=context.transcript,
        vision=context.vision,
        dead_time=context.dead_time,
        repetition=context.repetition,
        saturation=context.saturation,
        type_counts=counts,
    )

    scored = [_score_one(moment, config, resolved) for moment in moments]
    scored.sort(key=lambda moment: moment.score, reverse=True)

    logger.info(
        "Scored moments",
        extra={
            "moments": len(scored),
            "above_minimum": sum(
                1 for moment in scored if moment.score >= config.minimum_score
            ),
            "needs_review": sum(
                1 for moment in scored if moment.confidence < config.needs_review_confidence
            ),
            "top_score": round(scored[0].score, 4) if scored else 0.0,
        },
    )
    return scored


def _score_one(
    moment: Moment, config: MomentScoringConfig, context: ScoringContext
) -> Moment:
    dimensions = {
        "gameplay": _gameplay(moment),
        "visual": _visual(moment, context),
        "audio": _audio(moment, context),
        "reaction": _reaction(moment, context),
        "novelty": _novelty(moment, context),
        "skill": _skill(moment),
        "emotion": _emotion(moment, context),
        "narrative": _narrative(moment, context),
        "context": _context_quality(moment),
        "entertainment": _entertainment(moment, context),
    }

    weights = config.weights.model_dump()
    weighted = sum(dimensions[name] * weights.get(name, 0.0) for name in DIMENSIONS)
    total_weight = sum(weights.get(name, 0.0) for name in DIMENSIONS) or 1.0
    base = weighted / total_weight

    key = context.key(moment)
    dead_time = context.dead_time.get(key, 0.0)
    repetition = context.repetition.get(key, 0.0)
    saturation = context.saturation.get(key, 0.0)
    low_confidence = max(0.0, 1.0 - moment.confidence)

    penalties = config.penalties
    # Multiplicative: a moment that is repetitive *and* mostly dead air should
    # fall much further than either alone, and additive penalties cannot
    # express that without going negative.
    multiplier = (
        (1.0 - dead_time * (1.0 - penalties.dead_time))
        * (1.0 - repetition * (1.0 - penalties.repetition))
        * (1.0 - low_confidence * (1.0 - penalties.low_confidence))
        * (1.0 - saturation)
    )
    score = round(min(max(base * multiplier, 0.0), 1.0), 6)

    breakdown = {
        **{name: round(value, 4) for name, value in dimensions.items()},
        "_base": round(base, 4),
        "_penalty_dead_time": round(dead_time, 4),
        "_penalty_repetition": round(repetition, 4),
        "_penalty_low_confidence": round(low_confidence, 4),
        "_penalty_saturation": round(saturation, 4),
        "_multiplier": round(multiplier, 4),
    }

    return moment.with_score(
        score,
        breakdown,
        explain(moment, dimensions, breakdown, config),
        dead_time=dead_time,
        repetition=repetition,
    )


# ---------------------------------------------------------------------------
# the ten dimensions
# ---------------------------------------------------------------------------


def _gameplay(moment: Moment) -> float:
    """How much the game itself says happened.

    Straight from §26's ``importance``, which correlation already derived from
    event type and detector agreement. The most directly evidenced dimension
    there is.
    """
    return moment.importance


#: Labels that are evidence of *nothing* to look at. Kept in step with
#: :data:`backend.moments.dead_time._LABEL_CATEGORIES`'s screens.
_NOTHING_TO_SEE: frozenset[str] = frozenset({"menu", "loading"})


def _visual(moment: Moment, context: ScoringContext) -> float:
    """How much there was to look at.

    Vision observations inside the span, weighted by the model's own
    confidence. Absence of observations is not absence of action -- the cascade
    only looked where something was nominated -- so this floors at a neutral
    value rather than zero.

    A menu or loading label counts *against* the span, not for it. It used to
    count as density like any other observation, so a model confidently
    reporting "menu" raised the visual score of the very footage a viewer
    skips -- and the first real edit had QA flag 28 selected moments as menu or
    loading screens. The model being sure it sees a menu is exactly as strong
    an argument against the clip as "epic fight" is for it.
    """
    inside = [
        item
        for item in context.vision
        if moment.context_start <= item.timestamp <= moment.context_end
    ]
    if not inside:
        return 0.35

    dead = [
        item
        for item in inside
        if _NOTHING_TO_SEE & {str(label).lower() for label in item.labels}
    ]
    alive = [item for item in inside if item not in dead]
    if not alive:
        # Every look at this span found interface, not play.
        return 0.05

    density = min(len(alive) / 4.0, 1.0)
    quality = sum(item.confidence for item in alive) / len(alive)
    base = 0.5 * density + 0.5 * quality
    # The more of the span is screens, the less there is to watch in it.
    screen_fraction = len(dead) / len(inside)
    return round(base * (1.0 - 0.8 * screen_fraction), 4)


def _audio(moment: Moment, context: ScoringContext) -> float:
    """How much was happening in the sound.

    Spikes and transients in the span. Loud is not the same as good, which is
    why this is one weighted dimension of ten rather than a shortcut to the
    answer.
    """
    events = [
        event
        for event in context.audio_events
        if event.event_type in {AudioEventType.SPIKE, AudioEventType.TRANSIENT}
        and moment.context_start <= event.start_seconds <= moment.context_end
    ]
    if not events:
        return 0.2
    density = min(len(events) / 5.0, 1.0)
    intensity = max(event.confidence for event in events)
    return round(0.6 * density + 0.4 * intensity, 4)


def _reaction(moment: Moment, context: ScoringContext) -> float:
    """Whether the player reacted, and how strongly (§20).

    The single most reliable indicator that something was worth watching: the
    person who was there thought so at the time.
    """
    reactions = [
        event
        for event in context.audio_events
        if event.track_role == MICROPHONE
        and event.metadata.get("reaction_type")
        and moment.context_start <= event.start_seconds <= moment.context_end
    ]
    if not reactions:
        return 0.15
    strongest = max(event.confidence for event in reactions)
    correlated = any(
        event.metadata.get("correlation_offset") is not None for event in reactions
    )
    return round(min(strongest + (0.15 if correlated else 0.0), 1.0), 4)


def _novelty(moment: Moment, context: ScoringContext) -> float:
    """How unlike the rest of the recording this is.

    A rare type in a recording is more interesting than a common one, purely
    by contrast: the one funny moment in an hour of kills is worth more to a
    video than any individual kill.
    """
    total = sum(context.type_counts.values()) or 1
    occurrences = context.type_counts.get(moment.moment_type, 1)
    rarity = 1.0 - (occurrences / total)
    # Multi-source events are also novel in a different sense: several
    # detectors independently found them unusual.
    agreement = min(len(moment.sources) / 4.0, 1.0)
    return round(0.7 * rarity + 0.3 * agreement, 4)


def _skill(moment: Moment) -> float:
    """Whether the player demonstrably did something difficult.

    Derived from event types, and deliberately conservative. Without game
    knowledge nothing here can tell a lucky kill from an outplay, so a plain
    kill scores moderately and only the types that *mean* difficulty --
    multi-kill, clutch, comeback -- score high. Claiming otherwise would be
    inventing a judgement about play quality.
    """
    if any(event.event_type in _SKILL_EVENTS for event in moment.events):
        strongest = max(
            event.confidence
            for event in moment.events
            if event.event_type in _SKILL_EVENTS
        )
        return round(min(0.6 + 0.4 * strongest, 1.0), 4)
    if GameEventType.KILL in moment.event_types:
        return 0.45
    return 0.2


def _emotion(moment: Moment, context: ScoringContext) -> float:
    """Emotional weight, from the moment's kind and the player's voice."""
    inherent = _EMOTION_WEIGHT.get(moment.moment_type, 0.35)
    voice = _reaction(moment, context)
    return round(0.6 * inherent + 0.4 * voice, 4)


def _narrative(moment: Moment, context: ScoringContext) -> float:
    """How much this moment could carry a story.

    Partial by construction, and it says so. Without the §36 story pass this
    knows only two things: some moment *types* are natural beats -- a victory
    ends something, a comeback is a shape -- and position matters, because the
    opening and closing minutes of a session carry more structural weight than
    the middle. The story pass refines this; it does not start from nothing.
    """
    structural = {
        MomentType.VICTORY: 0.9,
        MomentType.DEFEAT: 0.8,
        MomentType.COMEBACK: 0.9,
        MomentType.BOSS: 0.85,
        MomentType.CLUTCH: 0.75,
    }.get(moment.moment_type, 0.4)

    if context.duration_seconds <= 0:
        return structural
    position = moment.midpoint / context.duration_seconds
    # A U-shape: beginnings and endings carry structure, middles carry content.
    edge = 1.0 - 4.0 * position * (1.0 - position)
    return round(min(0.75 * structural + 0.25 * edge, 1.0), 4)


def _context_quality(moment: Moment) -> float:
    """How self-contained the clip is.

    A moment whose expansion found a real boundary to start on is a clip that
    will read as an edit rather than an excerpt (§29). A moment that had to
    settle for an arbitrary offset is not worse gameplay -- it is a worse
    *clip*, which is what this dimension is about.
    """
    score = 0.5
    if moment.metadata.get("snapped"):
        score += 0.3
    pre_roll = float(moment.metadata.get("pre_roll_seconds") or 0.0)
    post_roll = float(moment.metadata.get("post_roll_seconds") or 0.0)
    if pre_roll > 0 and post_roll > 0:
        score += 0.2
    return round(min(score, 1.0), 4)


def _entertainment(moment: Moment, context: ScoringContext) -> float:
    """Whether somebody would want to watch it.

    The one composite dimension: reaction, novelty and audio energy together.
    Composite because "entertaining" has no single measurement, and pretending
    it does would be worse than combining the three signals that gesture at it.
    """
    return round(
        0.45 * _reaction(moment, context)
        + 0.3 * _novelty(moment, context)
        + 0.25 * _audio(moment, context),
        4,
    )


# ---------------------------------------------------------------------------
# explanation (§80)
# ---------------------------------------------------------------------------


def explain(
    moment: Moment,
    dimensions: dict[str, float],
    breakdown: dict[str, float],
    config: MomentScoringConfig,
) -> list[str]:
    """Say why this moment scored what it did, in plain sentences (§80).

    §80 requires every decision to be explainable, and the Q&A layer already
    reads this field to answer "why did you pick this?". Written as sentences
    rather than numbers because the number is already in the breakdown, and a
    user asking why does not want it repeated.
    """
    reasons: list[str] = []

    ranked = sorted(dimensions.items(), key=lambda item: item[1], reverse=True)
    strongest = [name for name, value in ranked[:3] if value >= 0.5]
    if strongest:
        reasons.append(f"Strongest on {', '.join(strongest)}.")

    sources = moment.sources
    if len(sources) > 1:
        reasons.append(
            f"{len(sources)} independent detectors agreed on this "
            f"({', '.join(sources)})."
        )
    elif sources:
        reasons.append(f"Detected by {sources[0]} alone, so confidence is limited.")

    types = sorted({event.event_type.value for event in moment.events})
    reasons.append(f"Contains {len(moment.events)} event(s): {', '.join(types)}.")

    if breakdown.get("_penalty_dead_time", 0.0) > 0.2:
        reasons.append(
            f"Penalised: {breakdown['_penalty_dead_time']:.0%} of the clip is dead time."
        )
    if breakdown.get("_penalty_repetition", 0.0) > 0.2:
        reasons.append("Penalised: similar moments appear elsewhere in the recording.")
    if breakdown.get("_penalty_saturation", 0.0) > 0.0:
        reasons.append(
            f"Penalised for variety: {moment.moment_type.value} moments already "
            "dominate the selection."
        )
    if moment.confidence < config.needs_review_confidence:
        reasons.append(
            f"Confidence {moment.confidence:.2f} is below the review threshold, "
            "so this is flagged for a human to check."
        )
    if moment.metadata.get("snapped"):
        notes = moment.metadata.get("context_notes") or []
        if notes:
            reasons.append(f"Clip boundaries: {'; '.join(notes)}.")

    return reasons


def needs_review(moment: Moment, config: MomentScoringConfig) -> bool:
    """Whether a human should look at this before it is used (§79)."""
    return moment.confidence < config.needs_review_confidence


def _count_types(moments: Sequence[Moment]) -> dict[MomentType, int]:
    counts: dict[MomentType, int] = {}
    for moment in moments:
        counts[moment.moment_type] = counts.get(moment.moment_type, 0) + 1
    return counts


__all__ = [
    "DIMENSIONS",
    "ScoringContext",
    "explain",
    "needs_review",
    "score_moments",
]
