"""Three edits from the same moments, so one can be chosen (V2-P6).

The optimiser has always produced exactly one plan: a weighted knapsack over
the selected moments, optimal by its own objective, and unfalsifiable because
nothing else was ever built to compare it with. When a video came out flat,
there was no way to ask whether a different balance would have been better --
only to change the weights and re-run everything, once, and hope.

This is the cheapest large improvement available in the whole design, and it
is cheap for a reason the architecture already paid for: §127 keeps selection
separate from the EDL, so re-planning reads stored moments and touches no
video. Three plans cost milliseconds and no analysis at all.

The profiles are not three sets of magic numbers. Each one is a different
answer to a question an editor actually argues about -- how much to favour the
strongest single moments over a shape, how much variety is worth -- and every
one of them is chronological, because the constitution is not a preference.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.editorial.policy import NEUTRAL, SelectionPolicy
from backend.moments.formation import Moment
from backend.narrative.story import NarrativePlan, build_plan

logger = get_logger("narrative.plans", LogChannel.PIPELINE)


@dataclass(frozen=True, slots=True)
class PlanProfile:
    """One way of weighing the same footage."""

    id: str
    name: str
    #: Multipliers on the optimiser's own objective weights. 1.0 leaves the
    #: configured value alone, so the balanced profile IS the shipped
    #: behaviour and any regression shows up as a difference from it.
    #:
    #: Kept as plain fields rather than a :class:`SelectionPolicy` so the three
    #: profiles read as one table; :meth:`policy` is the conversion, and it is
    #: the only place the two vocabularies meet.
    entertainment: float = 1.0
    narrative: float = 1.0
    variety: float = 1.0
    #: Multipliers on the optimiser's penalties. The variety *bonus* turned
    #: out to be inert -- raising it 1.6x selected the identical twelve clips
    #: -- because in a knapsack the way to get more kinds of thing is to make
    #: sameness expensive, not to make difference slightly cheaper.
    repetition_penalty: float = 1.0
    dead_time_penalty: float = 1.0

    def policy(self) -> SelectionPolicy:
        """This profile's balance, as the type the optimiser's seam speaks."""
        return SelectionPolicy(
            entertainment=self.entertainment,
            narrative=self.narrative,
            variety=self.variety,
            repetition_penalty=self.repetition_penalty,
            dead_time_penalty=self.dead_time_penalty,
        )
    why: str = ""


PROFILES: Final[tuple[PlanProfile, ...]] = (
    PlanProfile(
        id="A",
        name="balanced",
        why="the configured objective, unchanged -- the edit this system has always made",
    ),
    PlanProfile(
        id="B",
        name="tension_forward",
        entertainment=1.35,
        narrative=1.15,
        variety=0.55,
        why="the strongest moments and the arc between them, at the cost of breadth",
    ),
    PlanProfile(
        id="C",
        name="variety_forward",
        entertainment=0.80,
        narrative=0.85,
        variety=1.60,
        repetition_penalty=2.20,
        why="more kinds of thing happening, at the cost of the very best minutes",
    ),
)


def propose(
    moments: Sequence[Moment],
    *,
    mode: Any,
    target_seconds: float,
    config: Any,
    policy: Any,
    chronological: bool = False,
    speech: Mapping[str, Any] | None = None,
    media_durations: Mapping[str, float] | None = None,
    director: Callable[[Sequence[Moment]], Any] | None = None,
    profiles: Sequence[PlanProfile] = PROFILES,
    # Not `policy`: that name is taken by the duration band, and two meanings
    # of one word in one signature is a trap for whoever reads it next.
    selection: SelectionPolicy = NEUTRAL,
    exclusions: Mapping[str, Sequence[tuple[float, float]]] | None = None,
) -> list[tuple[PlanProfile, NarrativePlan]]:
    """One plan per profile, over the same moments.

    The Director is offered to the first profile only. It is a model call, it
    is the slowest thing in the stage by three orders of magnitude, and asking
    it the same question three times would produce three answers whose
    differences say more about sampling than about editing.
    """
    proposed: list[tuple[PlanProfile, NarrativePlan]] = []
    for index, profile in enumerate(profiles):
        plan = build_plan(
            moments,
            mode=mode,
            target_seconds=target_seconds,
            config=_weighted(config, profile, selection),
            policy=policy,
            chronological=chronological,
            speech=speech,
            media_durations=media_durations,
            director=director if index == 0 else None,
            exclusions=exclusions,
        )
        if plan.is_empty:
            continue
        proposed.append((profile, plan))
    logger.info(
        "Proposed edits",
        extra={
            "profiles": [profile.id for profile, _ in proposed],
            "clips": [len(plan.moments) for _, plan in proposed],
        },
    )
    return proposed


def _weighted(config: Any, profile: PlanProfile, selection: SelectionPolicy) -> Any:
    """The narrative config with this profile's balance and the style's, applied.

    Both at once and neither winning outright: a profile asks for a different
    balance *of the same edit*, a style asks for a different edit, and they are
    both true. The composition is bounded by :class:`SelectionPolicy`, so two
    legal asks cannot make an illegal objective.

    This function used to build the modified config itself. It now says the
    same thing in one typed place, which is what lets a style reach the
    optimiser without the optimiser ever reading a style.
    """
    return selection.then(profile.policy()).applied_to(config)


__all__ = ["PROFILES", "PlanProfile", "propose"]
