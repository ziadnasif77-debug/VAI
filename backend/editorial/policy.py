"""What a style is allowed to ask of the selection, as one typed thing.

The optimiser is the most delicate code in this project -- a knapsack over
value in context, then twelve rounds of repair against the duration band -- and
it is deterministic, which is why every plan it produces can be argued with.
Nothing here changes it. This module is the seam that lets a *taste* reach it
without a taste ever being read inside it.

The mechanism already existed in a narrower form. V2-P6's counterfactual
profiles bend the objective by building a modified ``DurationOptimizerConfig``
and handing that to an untouched ``optimise()``; :func:`SelectionPolicy.applied_to`
is that function, given a name and a type. So a profile and a style now say the
same kind of thing in the same language, and the optimiser still receives what
it always received.

Two properties are load-bearing, and both have tests:

* **Neutral is exact.** A policy that asks for nothing returns the caller's own
  config object, so the house style selects the identical moments it selected
  before this file existed -- not nearly identical.
* **Bounded.** Every field is a multiplier inside a declared range, and one
  policy cannot compose with another into something outside it. A style that
  wants twice the variety is expressible; a style that wants a hundred times is
  not, and saying so here is cheaper than discovering it in an edit.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Final

#: The widest a multiplier may be. Not a preference: it is the range within
#: which the objective still behaves like the objective. Below the floor a term
#: is effectively deleted and the optimiser is solving a different problem;
#: above the ceiling one term dominates and the others stop mattering, which is
#: a sort wearing a knapsack's clothes.
MULTIPLIER_FLOOR: Final[float] = 0.25
MULTIPLIER_CEILING: Final[float] = 3.0


def _bounded(value: float) -> float:
    return min(MULTIPLIER_CEILING, max(MULTIPLIER_FLOOR, float(value)))


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Multipliers on the optimiser's own objective. Nothing else.

    Deliberately not "what this style likes". The optimiser understands
    entertainment, narrative, variety, repetition and dead time; a policy that
    spoke of reactions or payoffs would either be ignored or would need the
    optimiser to learn a new vocabulary. Translating a doctrine into these five
    numbers is :mod:`backend.editorial.doctrine`'s job, and keeping that
    translation outside the optimiser is the whole point of this type.
    """

    entertainment: float = 1.0
    narrative: float = 1.0
    variety: float = 1.0
    repetition_penalty: float = 1.0
    dead_time_penalty: float = 1.0

    def __post_init__(self) -> None:
        for field in fields(self):
            object.__setattr__(self, field.name, _bounded(getattr(self, field.name)))

    @property
    def is_neutral(self) -> bool:
        """Whether this asks for nothing at all.

        The exact-equality guarantee rests on this: a neutral policy must be
        detectable so the caller's own config object can be returned unchanged,
        rather than a copy that is equal in every field and might one day not be.
        """
        return all(getattr(self, field.name) == 1.0 for field in fields(self))

    def then(self, other: SelectionPolicy) -> SelectionPolicy:
        """Both policies, composed -- a style's taste and a profile's balance.

        Multiplicative, then bounded, so composing two legal policies can never
        produce an illegal one. The counterfactual profiles ask for a different
        balance *of the same edit*, and a style asks for a different edit; both
        are true at once and neither wins outright.
        """
        return SelectionPolicy(
            entertainment=self.entertainment * other.entertainment,
            narrative=self.narrative * other.narrative,
            variety=self.variety * other.variety,
            repetition_penalty=self.repetition_penalty * other.repetition_penalty,
            dead_time_penalty=self.dead_time_penalty * other.dead_time_penalty,
        )

    def applied_to(self, config: Any) -> Any:
        """The narrative config with this policy's balance in it.

        Returns the caller's own object when the policy is neutral. That is not
        an optimisation -- it is the exact-equality guarantee, made structural
        instead of trusted.
        """
        if self.is_neutral:
            return config
        weights = config.optimizer.objective_weights
        penalties = config.optimizer.objective_penalties
        return config.model_copy(
            update={
                "optimizer": config.optimizer.model_copy(
                    update={
                        "objective_weights": weights.model_copy(
                            update={
                                "entertainment": weights.entertainment * self.entertainment,
                                "narrative": weights.narrative * self.narrative,
                                "variety": weights.variety * self.variety,
                            }
                        ),
                        "objective_penalties": penalties.model_copy(
                            update={
                                "repetition": penalties.repetition * self.repetition_penalty,
                                "dead_time": penalties.dead_time * self.dead_time_penalty,
                            }
                        ),
                    }
                )
            }
        )

    def as_dict(self) -> dict[str, float]:
        return {field.name: round(getattr(self, field.name), 4) for field in fields(self)}

    def describe(self) -> str:
        """What this asks for, in words, or that it asks for nothing."""
        if self.is_neutral:
            return "the configured objective, unchanged"
        parts = [
            f"{name.replace('_', ' ')} x{value:g}"
            for name, value in self.as_dict().items()
            if value != 1.0
        ]
        return ", ".join(parts)


#: What every caller gets until a style says otherwise.
NEUTRAL: Final[SelectionPolicy] = SelectionPolicy()


__all__ = [
    "MULTIPLIER_CEILING",
    "MULTIPLIER_FLOOR",
    "NEUTRAL",
    "SelectionPolicy",
]
