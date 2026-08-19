"""Asking the model for a shape, and checking the answer (Phase C).

The flow is short on purpose:

    describe the moments -> render a versioned prompt -> validated JSON
        -> check every beat against the evidence -> accept or reject

Every step that could let a hallucination through is a step that refuses
instead. The model names moments **by index into a list it was just shown**,
so there is no fuzzy matching from a description back to footage; an index
outside the list is a rejection, not a nearest-neighbour guess.

A rejection is not a failure. The pipeline has ordered videos deterministically
since Phase 7 and still does; the Director is an improvement on that order when
it earns it, and a recorded reason when it does not.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.core.duration import format_duration
from backend.core.errors import GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.core.prompts import load_prompt
from backend.director.models import Beat, Blueprint, BlueprintRejection
from backend.moments.formation import Moment

logger = get_logger("director.service", LogChannel.AI)

#: How many moments the Director is shown. A local 7B model given two hundred
#: numbered lines answers about the first twenty and invents the rest; the
#: selection it is shown is already the optimiser's best subset, so the cap
#: costs nothing a viewer would see.
MAX_MOMENTS_SHOWN: int = 40

#: §93's shape, enforced by Ollama as a grammar rather than checked afterwards.
#:
#: **The lengths are load-bearing**, and the Critic found out how. Ollama
#: compiles this into the grammar it decodes with, so a string with no bound is
#: an invitation to fill the output budget -- and when the budget runs out
#: mid-string the JSON is truncated and unparseable. Measured there: two runs
#: in three lost all three attempts that way. Every cap matches the Pydantic
#: model's exactly, so the grammar and the type agree about what fits.
_SCHEMA: dict = {
    "type": "object",
    "required": ["theme", "beats"],
    "properties": {
        "theme": {"type": "string", "maxLength": 200},
        "beats": {
            "type": "array",
            "maxItems": MAX_MOMENTS_SHOWN,
            "items": {
                "type": "object",
                "required": ["moment", "role"],
                "properties": {
                    "moment": {"type": "integer", "minimum": 0},
                    "role": {
                        "type": "string",
                        "enum": [
                            "hook",
                            "setup",
                            "escalation",
                            "climax",
                            "cooldown",
                            "payoff",
                            "outro",
                        ],
                    },
                    "reason": {"type": "string", "maxLength": 240},
                },
            },
        },
        "avoid": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 80},
        },
    },
}


def build_blueprint(
    moments: Sequence[Moment],
    *,
    provider,
    intent_text: str,
    target_seconds: float,
    style: str = "story",
) -> Blueprint | BlueprintRejection:
    """Ask the Director for the shape of this edit.

    Returns a :class:`Blueprint` only when every beat names a moment that
    exists. Anything else -- no provider, an unreachable model, an invalid
    answer, an index nobody can honour -- comes back as a
    :class:`BlueprintRejection` carrying why, and the caller keeps the order it
    already had.
    """
    if provider is None:
        return BlueprintRejection(reason="no reasoning model is configured")
    if not moments:
        return BlueprintRejection(reason="there are no moments to arrange")

    shown = list(moments[:MAX_MOMENTS_SHOWN])
    prompt = load_prompt("narrative.blueprint")
    rendered = prompt.render(
        moments=_describe(shown),
        intent=intent_text or "a highlights video of this session",
        target=format_duration(target_seconds),
        style=style,
    )

    try:
        payload = provider.complete_json(
            rendered, schema=_SCHEMA, prompt_id=prompt.id, temperature=0.2
        )
    except GamingEditorError as error:
        # §95: the model is an improvement on a working default, never a
        # dependency of it. A video is still made without one.
        logger.warning("The Director did not answer", extra={"error": str(error)[:160]})
        return BlueprintRejection(
            reason="the reasoning model did not answer", detail={"error": str(error)[:200]}
        )

    return _validated(payload, len(shown))


def _describe(moments: Sequence[Moment]) -> str:
    """The moments as numbered lines: what happened, when, and how sure.

    Deliberately terse and entirely factual. The model is being asked to order
    evidence, and prose about how exciting a clip is would be the scorer's
    opinion arriving as if it were an observation.
    """
    lines = []
    for index, moment in enumerate(moments):
        events = sorted({event.event_type.value for event in moment.events})
        named = ", ".join(event for event in events if event != "unexpected_event")
        lines.append(
            f"{index}. [{format_duration(moment.context_start)}] "
            f"{moment.moment_type.value} "
            f"({format_duration(moment.context_duration)}, score {moment.score:.2f})"
            + (f" -- {named}" if named else "")
        )
    return "\n".join(lines)


def _validated(payload: dict, available: int) -> Blueprint | BlueprintRejection:
    """Turn the model's answer into a blueprint, or say why it is not one."""
    raw_beats = payload.get("beats") or []
    beats: list[Beat] = []
    for entry in raw_beats:
        if not isinstance(entry, dict):
            continue
        index = entry.get("moment")
        if not isinstance(index, int) or not 0 <= index < available:
            # The one check the whole design exists for. A moment number
            # outside the list was not chosen from the evidence, so there is
            # nothing to repair it into.
            return BlueprintRejection(
                reason="the Director named a moment that does not exist",
                detail={"moment": index, "available": available},
            )
        beats.append(
            Beat(
                moment=index,
                role=str(entry.get("role") or "body")[:24],
                reason=str(entry.get("reason") or "")[:240],
            )
        )

    try:
        blueprint = Blueprint(
            theme=str(payload.get("theme") or "")[:200],
            beats=tuple(beats),
            avoid=tuple(str(item)[:80] for item in (payload.get("avoid") or [])[:8]),
        )
    except ValueError as error:
        return BlueprintRejection(
            reason="the Director's plan did not hold together",
            detail={"error": str(error)[:200]},
        )

    if blueprint.is_empty:
        # The prompt asks for this explicitly when the request cannot be met
        # from the session: an empty plan with a theme saying why.
        return BlueprintRejection(
            reason="the Director found nothing in this session to build the request from",
            detail={"theme": blueprint.theme},
        )

    logger.info("The Director proposed a shape", extra=blueprint.summary())
    return blueprint


__all__ = ["MAX_MOMENTS_SHOWN", "build_blueprint"]
