"""Why a moment scored what it did, in one place (SPEC §80, §63).

§80 requires every decision to be explainable, and the explanation is written
once at scoring time and stored. That was fine while the editor only spoke
English. It is not fine now: the stored sentences are English, so an Arabic
"لماذا اخترت هذه اللقطة؟" came back as an Arabic frame wrapped around three
English lines -- the half-translated reply that reads as broken software.

Translating the stored prose is not an option worth taking. An explanation is
evidence, and paraphrasing evidence with a pattern match is how evidence stops
being trustworthy.

So the **rules** live here rather than the sentences, and both sides call them:
the scorer renders them in English to store alongside the moment, and the
question answerer renders them again in the reader's language from the same
facts. One rule set, two renderings, no drift -- which matters more here than
it looks, because a second copy of these rules in the reader would diverge from
the writer's silently and nobody would notice until the two disagreed about a
clip someone was arguing with.

Nothing here reads a database or a model. It takes facts and returns lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.interaction.phrases import Phrasebook

#: A dimension has to reach this to be called a strength. Below it, naming it
#: would explain a weak score with a weak reason.
STRENGTH_FLOOR: float = 0.5

#: Penalties below this are noise rather than an explanation.
PENALTY_FLOOR: float = 0.2


@dataclass(frozen=True, slots=True)
class ReasonFacts:
    """Everything the rules below need, and nothing else.

    A deliberately flat shape: the scorer builds it from a :class:`Moment`, the
    question answerer from stored rows, and neither has to know about the
    other's types for the rules to apply identically.
    """

    moment_type: str
    dimensions: dict[str, float]
    confidence: float
    #: Detectors that agreed on this moment (§27). Empty when unknown.
    sources: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    event_count: int = 0
    dead_time: float = 0.0
    repetition: float = 0.0
    saturation: float = 0.0
    #: Below this, the moment is flagged for a human (§79).
    review_threshold: float = 0.0
    context_notes: tuple[str, ...] = field(default=())


def build_reasons(facts: ReasonFacts, phrases: Phrasebook) -> list[str]:
    """The §80 explanation, as plain sentences in ``phrases``'s language.

    Ordered strongest-claim-first: what made the score, who agreed, what is in
    it, and only then what was held against it. Someone asking "why this clip?"
    wants the answer before the caveats.
    """
    reasons: list[str] = []

    ranked = sorted(facts.dimensions.items(), key=lambda item: item[1], reverse=True)
    strongest = [name for name, value in ranked[:3] if value >= STRENGTH_FLOOR]
    if strongest:
        reasons.append(
            phrases.say(
                "reason_strongest",
                dimensions=phrases.join([phrases.dimension(name) for name in strongest]),
            )
        )

    if len(facts.sources) > 1:
        reasons.append(
            phrases.say(
                "reason_agreement",
                count=len(facts.sources),
                sources=phrases.join(list(facts.sources)),
            )
        )
    elif facts.sources:
        reasons.append(phrases.say("reason_single_source", source=facts.sources[0]))

    if facts.event_count or facts.event_types:
        reasons.append(
            phrases.say(
                "reason_events",
                count=facts.event_count,
                types=phrases.join([phrases.event_type(name) for name in facts.event_types]),
            )
        )

    if facts.dead_time > PENALTY_FLOOR:
        reasons.append(phrases.say("reason_dead_time", percent=f"{facts.dead_time:.0%}"))
    if facts.repetition > PENALTY_FLOOR:
        reasons.append(phrases.say("reason_repetition"))
    if facts.saturation > 0.0:
        reasons.append(
            phrases.say(
                "reason_saturation", moment_type=phrases.moment_type(facts.moment_type)
            )
        )
    if facts.confidence < facts.review_threshold:
        reasons.append(
            phrases.say("reason_needs_review", confidence=f"{facts.confidence:.2f}")
        )
    if facts.context_notes:
        reasons.append(
            phrases.say("reason_boundaries", notes="; ".join(facts.context_notes))
        )
    return reasons


__all__ = ["PENALTY_FLOOR", "STRENGTH_FLOOR", "ReasonFacts", "build_reasons"]
