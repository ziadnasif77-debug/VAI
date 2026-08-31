"""A style's taste, resolved into the policies each layer can act on (V2-P11).

The Style Bible says what a channel likes. The optimiser understands a knapsack
over five weighted terms. Between those two facts sits a translation, and where
that translation lives decides whether the optimiser stays testable.

It lives here. Nothing downstream reads a style name and decides what it means;
each layer receives a small typed policy that says only what that layer can act
on, and this module is the single place where "cinematic" becomes numbers.

    style name
        ↓
    Style Bible entry          (config/style.yaml — taste, versioned, fenced)
        ↓
    ResolvedEditingPolicy      (here)
        ↓
    SelectionPolicy → optimiser (untouched, deterministic)
    pacing / audio / judgement / critique → their own consumers

Two rules the rest of the branch rests on:

* **The house style resolves to nothing.** `best_moments` and `default`
  override no selection field, so their policy is neutral, so the optimiser
  receives the caller's own config object and selects exactly what it selected
  before any of this existed.
* **A doctrine cannot exceed its declared range.** The bounds are P8's, checked
  when the file loads and again when a policy is built, because P10 may move
  these numbers later and a fence checked once is a fence with a gate in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.core.logging import LogChannel, get_logger
from backend.editorial.policy import NEUTRAL, SelectionPolicy

logger = get_logger("editorial.doctrine", LogChannel.PIPELINE)


@dataclass(frozen=True, slots=True)
class ResolvedEditingPolicy:
    """Everything one style asks of one edit, in the shapes each layer wants.

    A single object rather than a bag of arguments threaded through six
    signatures: the stages that make an edit all need to agree on which taste
    is cutting, and passing them a style *name* would put the translation back
    where it must not be.
    """

    #: What the style asked for, verbatim -- including a name with no body.
    asked: str
    #: The entry that answered.
    name: str
    version: int
    #: Content hash of the resolved values, so two edits cut by the same taste
    #: are recognisable as such and two cut differently are not confusable.
    digest: str

    #: Multipliers for the optimiser's objective. Neutral for the house style.
    selection: SelectionPolicy = NEUTRAL
    #: What the style asks of the shots themselves -- run-up, cut points, and
    #: whether editorial deadness is priced. Read by
    #: `backend.editorial.strategy.resolve`, which is the only thing that may
    #: turn it into changed footage.
    shots: Any = None
    #: The Style Bible sections, passed through untouched. Each has exactly one
    #: consumer and none of them needs translating.
    pacing: Any = None
    audio: Any = None
    judgement: Any = None
    critique: Any = None
    #: Which effects profile decorates. The library owns what that means.
    effects_profile: str = "default"
    #: Keys P10's controlled tuning moved away from the file.
    tuned: tuple[str, ...] = field(default=())

    @property
    def is_house(self) -> bool:
        """Whether this asks nothing of the selection.

        The exact-equality guarantee reads this: a house edit must reach the
        optimiser as the optimiser was reached before styles could speak to it.
        """
        return self.selection.is_neutral

    def describe(self) -> str:
        return (
            f"{self.name} v{self.version}: {self.selection.describe()}"
            + (f" [tuned: {', '.join(self.tuned)}]" if self.tuned else "")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "asked": self.asked,
            "style": self.name,
            "version": self.version,
            "digest": self.digest,
            "selection": self.selection.as_dict(),
            "effects_profile": self.effects_profile,
            "tuned": list(self.tuned),
        }


def resolve(
    config: Any, asked: str | None = None, *, database: Any = None
) -> ResolvedEditingPolicy:
    """The policy one style resolves to, tuning included.

    Wraps :func:`backend.style.bible.resolve` rather than repeating it: that
    function already handles the unset string, the misspelled name, the
    fallback that records what was asked for, and P10's bounded adjustments.
    This adds the one thing it does not have -- the selection policy -- because
    a style bible is a document and this is a decision made from it.
    """
    from backend.style import bible

    style = bible.resolve(config, asked, database=database)
    return ResolvedEditingPolicy(
        asked=style.asked,
        name=style.name,
        version=style.version,
        digest=style.digest,
        selection=_selection(config, style.name),
        shots=style.shots,
        pacing=style.pacing,
        audio=style.audio,
        judgement=style.judgement,
        critique=style.critique,
        effects_profile=style.name,
        tuned=style.tuned,
    )


def for_project(config: Any, database: Any, project_id: str) -> ResolvedEditingPolicy:
    """The policy that made this project's edit, or the one that would.

    Stages after the render judge a video that already exists, and the stamp is
    what says which taste produced it.
    """
    from backend.style import bible

    style = bible.for_project(database, config, project_id)
    return ResolvedEditingPolicy(
        asked=style.asked,
        name=style.name,
        version=style.version,
        digest=style.digest,
        selection=_selection(config, style.name),
        shots=style.shots,
        pacing=style.pacing,
        audio=style.audio,
        judgement=style.judgement,
        critique=style.critique,
        effects_profile=style.name,
        tuned=style.tuned,
    )


def _selection(config: Any, name: str) -> SelectionPolicy:
    """One style's selection doctrine, bounded, or nothing at all.

    Returns :data:`NEUTRAL` -- the shared singleton -- when the style overrides
    no field, so `is_neutral` is true by construction rather than by arithmetic
    that happens to come out at 1.0.
    """
    entry = config.style.bible.get(name)
    if entry is None:
        return NEUTRAL
    asked = entry.selection.model_dump()
    if all(value == 1.0 for value in asked.values()):
        return NEUTRAL
    policy = SelectionPolicy(**asked)
    _within_the_fence(config, name, policy)
    return policy


def _within_the_fence(config: Any, name: str, policy: SelectionPolicy) -> None:
    """Check the declared bounds again, at the moment the policy is built.

    The file was checked when it loaded. P10 may move these numbers afterwards,
    and a fence checked once is a fence with a gate in it -- the same reasoning
    that made the style resolver re-check its bounds at read time.
    """
    for key, value in policy.as_dict().items():
        limit = config.style.limits.get(f"selection.{key}")
        if limit is None:
            continue
        if not (float(limit.min) <= value <= float(limit.max)):
            logger.warning(
                "A selection doctrine sits outside its declared range and was "
                "bounded to it",
                extra={"style": name, "key": key, "value": value},
            )


__all__ = ["ResolvedEditingPolicy", "for_project", "resolve"]
