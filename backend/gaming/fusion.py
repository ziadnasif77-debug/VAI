"""Naming what no single detector could name (Phase 0.2).

§27's fusion already exists in :mod:`backend.gaming.correlation`, and it does
one thing well: when several detectors describe the same instant, the most
specific *name any of them offered* wins. What it cannot do is name an instant
none of them named — and that is where two thirds of this pipeline's events end
up.

Measured on two real recordings: 61% and 70% of correlated events are
``unexpected_event``, and on one of them **63 of 116 events are
``["audio", "scene"]`` clusters** — an audio spike and a shot change, neither of
which is allowed to claim anything, and correctly so. A waveform transient is
not a kill.

But a waveform transient *while the vision model is reporting ``combat``* is
something a person would name without hesitation. The evidence was always
there; nothing was allowed to read it together.

So this module reads the **bundle**: every observation in a cluster, their
labels, their sources, their detail. A rule matches the bundle and names it.
Three properties keep that honest:

* **A rule never overrides a detector that could see.** Fusion runs only when
  correlation resolved a generic type — a profile's kill-feed reading always
  wins over an inference from a label and a spike.
* **A rule's confidence is its own.** It does not inherit the cluster's, which
  was computed for a different claim.
* **A rule names its evidence.** Every fused event records which rule fired and
  what it read (§21 provenance), so "why is this a collision?" is answerable
  from the row rather than from trust.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from backend.core.models.enums import GameEventType
from backend.gaming.events import EventObservation


@dataclass(frozen=True, slots=True)
class FusionRule:
    """Evidence that together names an event no detector could name alone.

    Every requirement is a conjunction: all of them must hold. A rule with no
    requirements at all would match everything, so :meth:`matches` refuses one.
    """

    event_type: GameEventType
    name: str
    #: Vision labels that must appear among the cluster's observations. Any one
    #: of them satisfies the requirement -- ``("driving", "vehicle")`` means
    #: "the model called it either".
    labels: tuple[str, ...] = ()
    #: Detector sources that must have contributed, e.g. ``("audio",)``.
    sources: tuple[str, ...] = ()
    #: Observation types that must be present, for rules keyed off a weaker
    #: named event rather than a raw signal.
    types: tuple[GameEventType, ...] = ()
    #: The weakest observation that may satisfy ``labels``. A label reported at
    #: 0.2 confidence is the model saying it does not know.
    min_label_confidence: float = 0.45
    #: What this rule alone is worth. Below any profile OCR rule on purpose.
    confidence: float = 0.65
    #: Extra seconds either side of the cluster the named event covers.
    pad_seconds: float = 0.0

    def matches(self, bundle: EvidenceBundle) -> bool:
        """Whether this rule's evidence is all present."""
        if not (self.labels or self.sources or self.types):
            return False
        if self.labels and not bundle.has_label(self.labels, self.min_label_confidence):
            return False
        if self.sources and not bundle.sources.issuperset(self.sources):
            return False
        return not (self.types and not bundle.types.issuperset(self.types))


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Everything a cluster of observations collectively saw.

    Built once per cluster rather than per rule: the label scan is the only
    non-trivial part, and running it once per rule would re-read the same
    dozen observations for every entry in the table.
    """

    sources: frozenset[str]
    types: frozenset[GameEventType]
    #: Vision labels with the best confidence each was reported at.
    labels: dict[str, float] = field(default_factory=dict)

    def has_label(self, wanted: Sequence[str], min_confidence: float) -> bool:
        return any(self.labels.get(label, -1.0) >= min_confidence for label in wanted)


def bundle_of(cluster: Sequence[EventObservation]) -> EvidenceBundle:
    """Collect what a cluster saw, for the rules to read."""
    labels: dict[str, float] = {}
    for item in cluster:
        label = item.detail.get("label")
        if isinstance(label, str) and label:
            key = label.strip().lower()
            labels[key] = max(labels.get(key, 0.0), item.confidence)
    return EvidenceBundle(
        sources=frozenset(item.source for item in cluster),
        types=frozenset(item.event_type for item in cluster),
        labels=labels,
    )


#: The generic table, applied to every game. Deliberately short and deliberately
#: cautious: each entry names something a person watching the same evidence
#: would name without knowing the game, which is exactly the §23 line. A profile
#: adds rules that need to know the game (Phase 0.3).
GENERIC_RULES: Final[tuple[FusionRule, ...]] = (
    # A fight on screen while something was heard. The single most common
    # unnamed instant on this footage -- the vision model reported `combat` 53
    # times on one recording and every one of them was discarded.
    FusionRule(
        event_type=GameEventType.COMBAT,
        name="combat_seen_and_heard",
        labels=("combat",),
        sources=("audio",),
        confidence=0.68,
    ),
    # Driving, a transient, and a shot change together: the picture of a
    # vehicle hitting something. Without the scene change it is just engine
    # noise over driving, which is most of an open-world recording.
    FusionRule(
        event_type=GameEventType.COLLISION,
        name="driving_impact",
        labels=("driving",),
        sources=("audio", "scene"),
        confidence=0.6,
    ),
    # Low health is already named by the vision label alone; this is the
    # stronger reading, where something was heard at the same instant.
    FusionRule(
        event_type=GameEventType.NEAR_DEATH,
        name="low_health_under_fire",
        labels=("low_health",),
        sources=("audio",),
        types=(GameEventType.LOW_HEALTH,),
        confidence=0.66,
    ),
)


@dataclass(frozen=True, slots=True)
class Fused:
    """A rule's verdict on a cluster."""

    event_type: GameEventType
    confidence: float
    rule: str
    evidence: dict[str, float]


def classify(
    cluster: Sequence[EventObservation],
    *,
    rules: Sequence[FusionRule] = GENERIC_RULES,
) -> Fused | None:
    """Name this cluster from its combined evidence, or return ``None``.

    ``None`` is the honest and common answer: an audio spike beside a shot
    change with nothing looking at the screen stays ``unexpected_event``, which
    is what the taxonomy has always meant by it. This module exists to stop
    that being the answer two thirds of the time, not to stop it being an
    answer.
    """
    if not cluster:
        return None
    bundle = bundle_of(cluster)
    for rule in rules:
        if rule.matches(bundle):
            return Fused(
                event_type=rule.event_type,
                confidence=rule.confidence,
                rule=rule.name,
                evidence={
                    label: round(bundle.labels[label], 3)
                    for label in rule.labels
                    if label in bundle.labels
                },
            )
    return None


__all__ = [
    "GENERIC_RULES",
    "EvidenceBundle",
    "Fused",
    "FusionRule",
    "bundle_of",
    "classify",
]
