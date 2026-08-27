"""Event correlation (SPEC sections 26, 27, 49).

§27 in one line, and it is the line that matters:

    Kill-feed change + weapon sound + "NO WAY" becomes **one** high-confidence
    gameplay moment.

Not three events. That distinction is the whole module. Detectors that agree
must **raise confidence**, not multiply the count — because everything
downstream treats an event as a thing that happened, and three records of one
explosion make it look like three explosions to the moment detector, the
scorer, the pacing pass and the finished video.

Two rules follow from it:

* **Agreement raises confidence, with diminishing returns.** Two independent
  sources are much better than one; a fourth adds little. Confidence rises
  towards certainty and never reaches it, because no amount of detector
  agreement makes an inference a fact.
* **The type comes from the source that could actually know it.** Audio can say
  *something happened*; only OCR against a profile's kill feed can say *kill*.
  When a specific type and a generic one describe the same instant, the
  specific one wins and the generic becomes corroboration.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

from backend.analysis.frame_state import StateSpan
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import FrameState, GameEventType
from backend.gaming.events import EventObservation
from backend.gaming.fusion import GENERIC_RULES, Fused, FusionRule, classify

logger = get_logger("gaming.correlation", LogChannel.PIPELINE)

#: How close two observations must be to be about the same instant. Detectors
#: disagree by more than they seem to: a kill-feed line appears a beat after the
#: sound, and a player reacts a beat after that.
DEFAULT_WINDOW_SECONDS: Final[float] = 2.5

#: The widest stretch of *claiming* evidence one cluster may cover. The window
#: above is 2.5 seconds because a cluster means "the same instant" -- but
#: chaining is transitive, and on the Grounded golden window it built clusters
#: of 59 to 96 seconds: audio transients every few seconds kept the chain
#: alive, one narration observation named the whole thing "outplay", and the
#: evaluator scored an invented minute-long event. Fifteen seconds keeps every
#: †real §27 shape (kill + sound + reaction) and sits under the episode
#: layer's measured 20-second knee, which is where situations -- as opposed to
#: instants -- are joined on purpose.
MAX_CLUSTER_CLAIM_SECONDS: Final[float] = 15.0

#: Confidence gained per additional agreeing source, before diminishing
#: returns. Applied as ``1 - (1 - c) * decay**(n - 1)``, which approaches
#: certainty without reaching it.
AGREEMENT_DECAY: Final[float] = 0.55

#: What a source that saw the instant but did not name the event is worth,
#: relative to one that agreed on the type. Half: it rules out a misreading
#: without supporting the specific claim.
CORROBORATION_WEIGHT: Final[float] = 0.5

#: Types that describe *something happened* without naming it. A specific type
#: always outranks these when both describe the same instant (§23).
GENERIC_TYPES: Final[frozenset[GameEventType]] = frozenset(
    {GameEventType.UNEXPECTED_EVENT, GameEventType.RARE_EVENT}
)


@dataclass(frozen=True, slots=True)
class GameEvent:
    """One thing that happened, as §26 defines it."""

    event_type: GameEventType
    start_seconds: float
    end_seconds: float
    confidence: float
    importance: float
    #: Every detector that saw it. §26's ``sources``, and the reason a reader
    #: can tell a four-source event from a lone audio spike.
    sources: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    game_profile: str | None = None

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0

    @property
    def agreement(self) -> int:
        return len(self.sources)

    @property
    def is_named(self) -> bool:
        """Whether the event has a specific type rather than a generic one."""
        return self.event_type not in GENERIC_TYPES

    def as_dict(self) -> dict[str, Any]:
        """The §26 shape, for logs and the API."""
        return {
            "type": self.event_type.value,
            "start": round(self.start_seconds, 3),
            "end": round(self.end_seconds, 3),
            "confidence": round(self.confidence, 4),
            "importance": round(self.importance, 4),
            "sources": list(self.sources),
            "metadata": self.metadata,
        }


def correlate(
    observations: Iterable[EventObservation],
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    game_profile: str | None = None,
    min_confidence: float = 0.0,
    fusion_rules: Sequence[FusionRule] = GENERIC_RULES,
    screen_states: Sequence[StateSpan] = (),
) -> list[GameEvent]:
    """Merge agreeing observations into events (§27).

    Args:
        observations: what the detectors reported.
        window_seconds: how close two observations must be to be the same
            instant.
        game_profile: recorded on every event. "Detected with the generic
            profile" and "detected with the Valorant profile" are different
            claims about the same event (§49).
        min_confidence: events below this are dropped. An event nobody is
            confident about costs the moment detector time and adds noise.
        fusion_rules: Phase 0.2's evidence table, consulted only when no
            detector could name a cluster. A profile passes its own rules
            ahead of the generic ones.
        screen_states: what the vision pass saw *between* the instants, from
            :func:`backend.analysis.frame_state.spans`. Used for one thing
            only: an instant the game was not being played at cannot be an
            event nobody could name, because it is not an event.

    Returns events in chronological order.
    """
    ordered = sorted(observations, key=lambda item: item.start_seconds)
    if not ordered:
        return []

    # A cluster of nothing but screen descriptions is not an event. The vision
    # detector reports what it saw at every analysed frame, and a run of "the
    # player is riding across a field" observations must not become a hundred
    # events -- it is context for the instants where something was also heard.
    clusters = [
        cluster
        for cluster in _cluster(ordered, window_seconds)
        if any(not item.context_only for item in cluster)
    ]
    events = [
        event
        for cluster in clusters
        if (event := _to_event(cluster, game_profile, fusion_rules)).confidence
        >= min_confidence
    ]
    events, off_screen = _on_screen(events, screen_states)
    events.sort(key=lambda event: event.start_seconds)

    named = sum(1 for event in events if event.is_named)
    logger.info(
        "Correlated observations into events",
        extra={
            "observations": len(ordered),
            "clusters": len(clusters),
            "events": len(events),
            "named": named,
            # Phase 0.5's headline metric: what fraction of what happened the
            # pipeline could put a name to. Measured at 0.30-0.39 before fusion.
            "named_event_ratio": round(named / len(events), 4) if events else 0.0,
            "fused": sum(
                1
                for event in events
                if str(event.metadata.get("named_by", "")).startswith("fusion:")
            ),
            "multi_source": sum(1 for event in events if event.agreement > 1),
            # Phase 0's criterion 8. A scene cut and an audio spike while the
            # player is reading a menu is the interface making noise, not the
            # game doing something.
            "dropped_off_screen": off_screen,
        },
    )
    return events


def _on_screen(
    events: Sequence[GameEvent], screen_states: Sequence[StateSpan]
) -> tuple[list[GameEvent], int]:
    """Drop the unnamed events that happened while the game was not on screen.

    Measured across ten real projects: of 389 events nobody could name, 104 had
    a frame within two seconds of them, and of what those frames reported,
    ``menu``, ``inventory`` and ``loading`` outnumbered every gameplay label
    together. Those are not naming failures. A scene boundary and an audio
    spike are exactly what a menu opening produces, and calling the result an
    event nobody could identify is the detector describing the interface.

    **Only unnamed events, and only screens.** A named event keeps its name
    wherever it was read -- ``defeat`` is read off a defeat screen, and a rule
    that dropped it would delete the clearest evidence this pipeline has. And
    ``HUD_ONLY`` is not a screen: the vision model calls a health bar over a
    firefight ``inventory``, which is the reason :mod:`frame_state` separates
    the two at all.
    """
    screens = [
        span
        for span in screen_states
        if span.state in {FrameState.MENU, FrameState.LOADING, FrameState.PAUSE}
    ]
    if not screens:
        return list(events), 0

    kept: list[GameEvent] = []
    dropped = 0
    for event in events:
        if event.is_named or not any(
            span.overlaps(event.start_seconds, event.end_seconds) > 0 for span in screens
        ):
            kept.append(event)
        else:
            dropped += 1
    return kept, dropped


def _cluster(
    observations: Sequence[EventObservation], window: float
) -> list[list[EventObservation]]:
    """Group observations that describe the same instant.

    Grouped by proximity to the cluster's own span rather than to its first
    member, so a chain of observations a second apart stays one event instead
    of fragmenting — which is precisely the "three explosions" failure §27
    exists to prevent.

    Two limits keep "the same instant" meaning what it says, both measured on
    the Grounded golden window where their absence built 59-to-96-second
    clusters:

    * **Context does not bridge.** A screen description arrives with every
      analysed frame, so at a five-second vision cadence a chain of them can
      connect anything to anything. A claiming observation joins by its
      distance from the last *claiming* one; context attaches to whatever
      cluster it lands beside but never extends the reach.
    * **A cluster's claiming evidence is capped** at
      :data:`MAX_CLUSTER_CLAIM_SECONDS`. Past that, this layer's answer is
      two events -- and whether they are one *situation* is the episode
      layer's question, answered at its own measured threshold.
    """
    clusters: list[list[EventObservation]] = []
    claim_start: float | None = None
    claim_end: float | None = None
    for observation in observations:
        if clusters:
            current = clusters[-1]
            anchor = (
                claim_end
                if claim_end is not None
                else max(item.end_seconds for item in current)
            )
            near = observation.start_seconds - anchor <= window
            if near and observation.context_only:
                current.append(observation)
                continue
            if near and not observation.context_only:
                start = claim_start if claim_start is not None else observation.start_seconds
                if max(claim_end or 0.0, observation.end_seconds) - start <= (
                    MAX_CLUSTER_CLAIM_SECONDS
                ):
                    current.append(observation)
                    claim_start = start
                    claim_end = max(claim_end or 0.0, observation.end_seconds)
                    continue
        clusters.append([observation])
        if observation.context_only:
            claim_start = claim_end = None
        else:
            claim_start = observation.start_seconds
            claim_end = observation.end_seconds
    return clusters


def _to_event(
    cluster: Sequence[EventObservation],
    game_profile: str | None,
    rules: Sequence[FusionRule] = GENERIC_RULES,
) -> GameEvent:
    """Turn one cluster into a single event."""
    sources = tuple(sorted({item.source for item in cluster}))
    event_type = _resolve_type(cluster)

    # A state read by one kind of sensor is context, not an event. Measured on
    # the Grounded window: the vision model reads the game's always-on
    # hunger-and-thirst dials as "low health" while the player walks -- four
    # frames in a row, on footage a person marked boring. Demoting the type to
    # generic hands the cluster to fusion, where `hurt_and_heard` still names
    # the real thing when audio corroborates it, and a HUD or profile reading
    # (a source that can actually see the bar) is never demoted.
    if event_type is GameEventType.LOW_HEALTH:
        claiming = {item.source for item in cluster if item.event_type is event_type}
        if claiming == {"vision"}:
            event_type = GameEventType.UNEXPECTED_EVENT

    # Phase 0.2: no detector could name this instant, but the evidence together
    # might. Only reached when the resolved type is generic, so a source that
    # could actually see -- a profile's kill feed, a victory banner -- is never
    # overridden by an inference.
    fused = classify(cluster, rules=rules) if event_type in GENERIC_TYPES else None
    if fused is not None:
        return _fused_event(cluster, fused, sources, game_profile)

    supporting = [item for item in cluster if item.event_type is event_type]

    # Two kinds of agreement, worth different amounts. A source that agrees on
    # the *type* corroborates the claim. A source reporting a generic type at
    # the same instant corroborates that something real happened there -- which
    # is weaker evidence for "it was a kill", but not none: a kill-feed reading
    # with a weapon sound under it is likelier to be a kill than one over
    # silence.
    agreeing = {item.source for item in supporting}
    corroborating = {
        item.source
        for item in cluster
        if item.event_type in GENERIC_TYPES
        and item.source not in agreeing
        # A screen description corroborates nothing by itself: the vision model
        # reports one at every analysed frame, so counting them would raise the
        # confidence of every event that happened to sit near a keyframe.
        and not item.context_only
    }
    confidence = _combine(
        [item.confidence for item in supporting],
        agreeing_sources=len(agreeing),
        corroborating_sources=len(corroborating),
    )
    start, end = _span_of(cluster)

    return GameEvent(
        event_type=event_type,
        start_seconds=start,
        end_seconds=max(end, start),
        confidence=confidence,
        importance=_importance(event_type, confidence, len(sources)),
        sources=sources,
        game_profile=game_profile,
        metadata={
            "observations": len(cluster),
            "agreeing_observations": len(supporting),
            "detail": [
                {
                    "source": item.source,
                    "type": item.event_type.value,
                    "confidence": round(item.confidence, 3),
                    **item.detail,
                }
                for item in cluster
            ],
            "correlated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _fused_event(
    cluster: Sequence[EventObservation],
    fused: Fused,
    sources: tuple[str, ...],
    game_profile: str | None,
) -> GameEvent:
    """Build the event a fusion rule named.

    Confidence is the rule's own, raised by how many sources were in the
    bundle -- the same diminishing-returns curve every other event uses, so a
    fused event and a detected one can be compared without knowing which is
    which. The rule and what it read are recorded, because an event nobody
    detected has to be able to explain itself (§21, §80).
    """
    start, end = _span_of(cluster)
    confidence = _combine(
        [fused.confidence], agreeing_sources=len(sources), corroborating_sources=0
    )
    return GameEvent(
        event_type=fused.event_type,
        start_seconds=start,
        end_seconds=max(end, start),
        confidence=confidence,
        importance=_importance(fused.event_type, confidence, len(sources)),
        sources=sources,
        game_profile=game_profile,
        metadata={
            "observations": len(cluster),
            "named_by": f"fusion:{fused.rule}",
            "fusion_evidence": fused.evidence,
            "detail": [
                {
                    "source": item.source,
                    "type": item.event_type.value,
                    "confidence": round(item.confidence, 3),
                    **item.detail,
                }
                for item in cluster
            ],
            "correlated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _span_of(cluster: Sequence[EventObservation]) -> tuple[float, float]:
    """When the event happened, from the observations that claim it happened.

    A screen description explains an event; it does not lengthen one. Letting
    context set the bounds stretched every event towards the nearest analysed
    keyframe, which merged neighbouring events into longer groups and cost a
    third of one recording's candidate clips in a measurement.
    """
    claiming = [item for item in cluster if not item.context_only] or list(cluster)
    start = min(item.start_seconds for item in claiming)
    end = max(item.end_seconds for item in claiming)
    return start, max(end, start)


def _resolve_type(cluster: Sequence[EventObservation]) -> GameEventType:
    """Choose the event type a cluster supports.

    A specific type beats a generic one outright: audio can report that
    something happened, but only a source that read the screen can say what.
    Among specific types, the one with the strongest total support wins, and
    ties break on the single most confident observation.
    """
    named = [item for item in cluster if item.event_type not in GENERIC_TYPES]
    candidates = named or list(cluster)

    totals: dict[GameEventType, float] = {}
    peaks: dict[GameEventType, float] = {}
    for item in candidates:
        totals[item.event_type] = totals.get(item.event_type, 0.0) + item.confidence
        peaks[item.event_type] = max(peaks.get(item.event_type, 0.0), item.confidence)

    return max(totals, key=lambda key: (totals[key], peaks[key], key.value))


def _combine(
    confidences: Sequence[float],
    *,
    agreeing_sources: int,
    corroborating_sources: int = 0,
) -> float:
    """Combine agreeing confidences into one, with diminishing returns.

    Starts from the strongest single piece of evidence and closes the remaining
    gap to certainty once per additional **independent source** — not once per
    observation, or three OCR lines from one frame would look like three
    detectors agreeing.

    A corroborating source, which saw the instant but did not name it, counts
    for :data:`CORROBORATION_WEIGHT` of a full one.

    Never reaches 1.0. No amount of agreement between inferring detectors turns
    an inference into a fact, and a stored 1.0 would tell every later stage that
    this one is beyond question.
    """
    if not confidences:
        return 0.0
    best = max(min(max(value, 0.0), 1.0) for value in confidences)
    extra = max(agreeing_sources - 1, 0) + CORROBORATION_WEIGHT * max(
        corroborating_sources, 0
    )
    if extra <= 0:
        return round(best, 6)
    combined = 1.0 - (1.0 - best) * math.pow(AGREEMENT_DECAY, extra)
    return round(min(combined, 0.99), 6)


def _importance(event_type: GameEventType, confidence: float, sources: int) -> float:
    """How much this event should weigh before moments are scored (§32).

    Deliberately crude, and deliberately not a quality judgement: §33 is
    explicit that the highest score is not the best clip, and the real decision
    belongs to the moment scorer with its configurable weights. What this
    carries is only "how much did the detectors think was going on here".
    """
    base = _BASE_IMPORTANCE.get(event_type, 0.5)
    agreement_bonus = min(sources - 1, 3) * 0.05
    return round(min(base * confidence + agreement_bonus, 1.0), 6)


#: Rough prior weight per event type. A victory matters more than a scene
#: change; both are refined by §32's configurable scoring.
_BASE_IMPORTANCE: Final[dict[GameEventType, float]] = {
    GameEventType.VICTORY: 0.9,
    GameEventType.DEFEAT: 0.8,
    GameEventType.BOSS_DEFEAT: 0.9,
    GameEventType.MULTI_KILL: 0.85,
    GameEventType.CLUTCH: 0.9,
    GameEventType.KILL: 0.7,
    GameEventType.DEATH: 0.6,
    GameEventType.FUNNY_MOMENT: 0.75,
    GameEventType.OBJECTIVE: 0.7,
    GameEventType.OBJECTIVE_FAILURE: 0.6,
    GameEventType.LOW_HEALTH: 0.55,
    GameEventType.BOSS_FIGHT: 0.7,
    GameEventType.RARE_EVENT: 0.6,
    # Named by fusion rather than by a detector that could see, so weighted
    # below the read-from-screen types and above the unnamed ones.
    GameEventType.COMBAT: 0.6,
    GameEventType.COLLISION: 0.55,
    GameEventType.CHASE: 0.65,
    GameEventType.UNEXPECTED_EVENT: 0.4,
}


__all__ = [
    "AGREEMENT_DECAY",
    "CORROBORATION_WEIGHT",
    "DEFAULT_WINDOW_SECONDS",
    "GENERIC_TYPES",
    "GameEvent",
    "correlate",
]
