"""Asking a model what is wrong with the edit, and checking the answer (Phase E).

    describe the edit -> render a versioned prompt -> validated JSON
        -> check every note against the clips that exist -> accept or reject

The shape is the Director's, and deliberately so: the two are the same kind of
component. A model reads evidence the pipeline produced, answers by index into
a list it was just shown, and code decides what -- if anything -- happens next.
What differs is when they run and what they are looking at. The Director sees
moments and proposes a video; the Critic sees the video and proposes cuts.

A rejection is not a failure. The pipeline has rendered its edit unreviewed
since Phase 8 and still can; the Critic is an improvement on that when it earns
one, and a recorded reason when it does not (§95).
"""

from __future__ import annotations

from backend.core.duration import format_duration
from backend.core.errors import GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.core.prompts import load_prompt
from backend.critic.evidence import EditEvidence
from backend.critic.models import Action, Critique, CritiqueRejection, Note

logger = get_logger("critic.service", LogChannel.AI)

#: How many clips the Critic is shown. A finished edit rarely has more; a
#: best-moments compilation can, and a local 7B model given eighty numbered
#: rows reviews the first fifteen and invents the rest.
MAX_CLIPS_SHOWN: int = 40

#: The most of a clip a trim may take. Past this the note is not a trim, it is
#: a disagreement about whether the clip belongs -- which is what `drop` is
#: for, and which §39 gets a veto over.
MAX_TRIM_FRACTION: float = 0.5

#: §93's shape, enforced by Ollama as a grammar rather than checked after.
_SCHEMA: dict = {
    "type": "object",
    "required": ["verdict", "notes"],
    "properties": {
        "verdict": {"type": "string"},
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["clip", "action"],
                "properties": {
                    "clip": {"type": "integer", "minimum": 0},
                    "action": {
                        "type": "string",
                        "enum": [action.value for action in Action],
                    },
                    "seconds": {"type": "number", "minimum": 0},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def review(
    evidence: EditEvidence,
    *,
    provider,
    intent_text: str = "",
) -> Critique | CritiqueRejection:
    """Ask the Critic what is wrong with this edit.

    Returns a :class:`Critique` only when every note names a clip that exists
    and asks for something the timeline can do. Anything else -- no provider,
    an unreachable model, an index nobody can honour -- comes back as a
    :class:`CritiqueRejection` carrying why, and the caller renders the edit it
    already had.
    """
    if provider is None:
        return CritiqueRejection(reason="no reasoning model is configured")
    if evidence.is_empty:
        return CritiqueRejection(reason="there is no edit to review")

    shown = evidence.clips[:MAX_CLIPS_SHOWN]
    prompt = load_prompt("critique.edit_review")
    rendered = prompt.render(
        clips="\n".join(clip.line() for clip in shown),
        total=format_duration(evidence.total_seconds),
        target=format_duration(evidence.target_seconds),
        intent=intent_text or "a highlights video of this session",
    )

    try:
        payload = provider.complete_json(
            rendered, schema=_SCHEMA, prompt_id=prompt.id, temperature=0.2
        )
    except GamingEditorError as error:
        logger.warning("The Critic did not answer", extra={"error": str(error)[:160]})
        return CritiqueRejection(
            reason="the reasoning model did not answer", detail={"error": str(error)[:200]}
        )

    return _validated(payload, shown)


def _validated(payload: dict, shown) -> Critique | CritiqueRejection:
    """Turn the model's answer into a critique, or say why it is not one."""
    notes: list[Note] = []
    for entry in payload.get("notes") or []:
        if not isinstance(entry, dict):
            continue
        index = entry.get("clip")
        if not isinstance(index, int) or not 0 <= index < len(shown):
            # The one check the whole design exists for. A clip number outside
            # the list was not read off the evidence, so there is nothing to
            # repair it into.
            return CritiqueRejection(
                reason="the Critic named a clip that does not exist",
                detail={"clip": index, "available": len(shown)},
            )
        note = _note(entry, index, shown[index].seconds)
        if note is not None:
            notes.append(note)

    try:
        critique = Critique(
            verdict=str(payload.get("verdict") or "")[:300],
            notes=tuple(notes),
        )
    except ValueError as error:
        return CritiqueRejection(
            reason="the Critic's review did not hold together",
            detail={"error": str(error)[:200]},
        )

    logger.info("The Critic reviewed the edit", extra=critique.summary())
    return critique


def _note(entry: dict, index: int, clip_seconds: float) -> Note | None:
    """One entry, made safe -- or dropped to ``keep`` where it cannot be.

    A malformed action or an over-long trim is not treated as a reason to throw
    the whole review away. The clip exists, the model had something to say
    about it, and the honest reduction is "reviewed, no change" rather than
    losing the other nineteen notes with it. Naming a clip that does not exist
    is different, and is handled above: that is the model reading a list it was
    not shown.
    """
    try:
        action = Action(str(entry.get("action") or Action.KEEP.value))
    except ValueError:
        action = Action.KEEP

    seconds = float(entry.get("seconds") or 0.0)
    reason = str(entry.get("reason") or "")[:240]

    if action in {Action.TRIM_START, Action.TRIM_END}:
        ceiling = clip_seconds * MAX_TRIM_FRACTION
        if seconds <= 0:
            action, seconds = Action.KEEP, 0.0
        elif seconds > ceiling:
            # Asked to remove most of a clip. The clip still has whatever put
            # it in the edit, so the trim is taken to the ceiling rather than
            # discarded -- and the reason records what was actually asked for.
            reason = f"{reason} (asked for {seconds:.1f}s; capped)".strip()
            seconds = ceiling

    try:
        return Note(clip=index, action=action, seconds=round(seconds, 3), reason=reason)
    except ValueError:
        return None


__all__ = ["MAX_CLIPS_SHOWN", "MAX_TRIM_FRACTION", "review"]
