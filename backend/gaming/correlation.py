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

from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import GameEventType
from backend.gaming.events import EventObservation

logger = get_logger("gaming.correlation", LogChannel.PIPELINE)

#: How close two observations must be to be about the same instant. Detectors
#: disagree by more than they seem to: a kill-feed line appears a beat after the
#: sound, and a player reacts a beat after that.
DEFAULT_WINDOW_SECONDS: Final[float] = 2.5

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

    Returns events in chronological order.
    """
    ordered = sorted(observations, key=lambda item: item.start_seconds)
    if not ordered:
        return []

    clusters = _cluster(ordered, window_seconds)
    events = [
        event
        for cluster in clusters
        if (event := _to_event(cluster, game_profile)).confidence >= min_confidence
    ]
    events.sort(key=lambda event: event.start_seconds)

    logger.info(
        "Correlated observations into events",
        extra={
            "observations": len(ordered),
            "clusters": len(clusters),
            "events": len(events),
            "named": sum(1 for event in events if event.is_named),
            "multi_source": sum(1 for event in events if event.agreement > 1),
        },
    )
    return events


def _cluster(
    observations: Sequence[EventObservation], window: float
) -> list[list[EventObservation]]:
    """Group observations that describe the same instant.

    Grouped by proximity to the cluster's own span rather than to its first
    member, so a chain of observations a second apart stays one event instead
    of fragmenting — which is precisely the "three explosions" failure §27
    exists to prevent.
    """
    clusters: list[list[EventObservation]] = []
    for observation in observations:
        if clusters:
            current = clusters[-1]
            latest_end = max(item.end_seconds for item in current)
            if observation.start_seconds - latest_end <= window:
                current.append(observation)
                continue
        clusters.append([observation])
    return clusters


def _to_event(
    cluster: Sequence[EventObservation], game_profile: str | None
) -> GameEvent:
    """Turn one cluster into a single event."""
    sources = tuple(sorted({item.source for item in cluster}))
    event_type = _resolve_type(cluster)
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
        if item.event_type in GENERIC_TYPES and item.source not in agreeing
    }
    confidence = _combine(
        [item.confidence for item in supporting],
        agreeing_sources=len(agreeing),
        corroborating_sources=len(corroborating),
    )
    start = min(item.start_seconds for item in cluster)
    end = max(item.end_seconds for item in cluster)

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
