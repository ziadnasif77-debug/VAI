"""The hook (SPEC section 37).

    The first seconds must give a reason to keep watching: epic moment, clutch,
    funny reaction, unexpected event, or an outcome preview. **The system must
    not invent narration.**

The last sentence is a hard constraint on what this module is allowed to do. It
**selects** — it never writes a voice-over, never generates a title card, never
fabricates footage. The hook is a moment that already exists in the recording,
moved to the front.

That constraint is also what makes the feature honest. A generated "you won't
believe what happens next" is a promise the video did not make and may not keep;
a clip of the thing actually happening is the same promise, kept in advance.

The moment used as a hook can appear again in its chronological place
(``hook.allow_replay_in_body``). That is a deliberate editing convention — the
cold open, then the story that leads back to it — not an accident, and it is
configurable because it is a stylistic choice rather than a rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from backend.config.schema import HookConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import MomentType
from backend.moments.formation import Moment, replace_moment

logger = get_logger("narrative.hook", LogChannel.PIPELINE)

#: Moment types that can open a video, and how well. §37 lists epic, clutch,
#: funny reaction and unexpected event; the weights rank them when several are
#: available.
HOOK_STRENGTH: Final[dict[MomentType, float]] = {
    MomentType.EPIC: 1.0,
    MomentType.CLUTCH: 0.95,
    MomentType.FUNNY: 0.9,
    MomentType.SURPRISE: 0.8,
    MomentType.OUTPLAY: 0.85,
    MomentType.COMEBACK: 0.85,
    MomentType.RARE: 0.75,
    MomentType.CHAOS: 0.7,
    MomentType.VICTORY: 0.7,
    MomentType.FAIL: 0.65,
}

#: §37's "outcome preview": opening on the result, then showing how it happened.
#: Only ever a moment that exists -- previewing an outcome means showing the
#: real one early, not describing it.
OUTCOME_TYPES: Final[frozenset[MomentType]] = frozenset(
    {MomentType.VICTORY, MomentType.DEFEAT, MomentType.BOSS}
)


@dataclass(frozen=True, slots=True)
class HookSelection:
    """The chosen opening, and why it was chosen."""

    moment: Moment | None
    reason: str
    #: True when the same moment also appears in the body, in its own place.
    replayed_in_body: bool = False
    considered: int = 0

    @property
    def exists(self) -> bool:
        return self.moment is not None

    def summary(self) -> dict[str, Any]:
        if self.moment is None:
            return {"hook": None, "reason": self.reason, "considered": self.considered}
        return {
            "hook": self.moment.moment_type.value,
            "start_seconds": round(self.moment.context_start, 2),
            "seconds": round(self.moment.context_duration, 2),
            "reason": self.reason,
            "replayed_in_body": self.replayed_in_body,
            "considered": self.considered,
        }


def choose_hook(
    moments: Sequence[Moment], config: HookConfig
) -> HookSelection:
    """Pick the moment that opens the video (§37).

    Args:
        moments: the selected moments, in any order.
        config: ``narrative.hook``.

    Returns a selection that may be empty. A video with no moment strong enough
    to open on simply starts at its beginning -- which is better than promoting
    a weak clip into the position that decides whether anyone keeps watching.
    """
    if not config.enabled or not moments:
        return HookSelection(moment=None, reason="hook disabled or nothing to choose from")

    allowed = _allowed_types(config)
    candidates = [
        moment
        for moment in moments
        if moment.moment_type in allowed and moment.context_duration >= config.min_seconds
    ]
    if not candidates:
        return HookSelection(
            moment=None,
            reason="no moment of a type strong enough to open on",
            considered=len(moments),
        )

    best = max(candidates, key=lambda moment: _hook_value(moment))
    trimmed, was_trimmed = _fit(best, config)
    reason = (
        f"strongest {best.moment_type.value} moment "
        f"(score {best.score:.2f}, {len(best.sources)} detector(s) agreed)"
    )
    if was_trimmed:
        reason += f"; trimmed to {config.max_seconds:.0f}s for the opening"

    selection = HookSelection(
        moment=trimmed,
        reason=reason,
        replayed_in_body=config.allow_replay_in_body,
        considered=len(candidates),
    )
    logger.info("Chose the hook", extra=selection.summary())
    return selection


def _allowed_types(config: HookConfig) -> set[MomentType]:
    """Resolve ``hook.sources`` into moment types.

    ``outcome_preview`` is not a moment type -- it is §37's name for opening on
    the result. It expands to the types that *are* outcomes.
    """
    allowed: set[MomentType] = set()
    for name in config.sources:
        if name == "outcome_preview":
            allowed |= OUTCOME_TYPES
            continue
        try:
            allowed.add(MomentType(name))
        except ValueError:
            logger.warning("Unknown hook source in configuration", extra={"source": name})
    return allowed or set(HOOK_STRENGTH)


def _hook_value(moment: Moment) -> float:
    """How well a moment would open the video.

    Its own score, weighted by how well its *kind* works as an opening. The
    best clip in a video is not always the best first clip: a tense build-up
    scores well and opens badly, because a hook has to land immediately.
    """
    return moment.score * HOOK_STRENGTH.get(moment.moment_type, 0.5)


def _fit(moment: Moment, config: HookConfig) -> tuple[Moment, bool]:
    """Trim a hook to its maximum length, from the front.

    Trimmed from the front rather than the end: the payoff is at the end of the
    moment, and an opening that stops before it is an opening that promises
    nothing.
    """
    if moment.context_duration <= config.max_seconds:
        return moment, False
    return (
        replace_moment(
            moment,
            context_start=max(moment.context_end - config.max_seconds, 0.0),
            metadata={**moment.metadata, "role": "hook", "trimmed_for_hook": True},
        ),
        True,
    )


__all__ = ["HOOK_STRENGTH", "OUTCOME_TYPES", "HookSelection", "choose_hook"]
