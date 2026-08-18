"""What the Director may say, and what it may not (Phase C).

The blueprint is **structure, not content**. It names moments the pipeline
already found and says what to do with them: which one opens, which one closes,
how the middle should feel, what to avoid. It carries no timestamps it invented,
no clips it wishes existed, and no instructions for a renderer.

The rule that shapes every field here is one line:

    The Director may not produce an event that is not in the evidence.

A blueprint asking for a boss fight in a recording with no boss fight is not
repaired into something plausible. It is rejected, with the reason kept, and
the pipeline falls back to the deterministic order it has always used. A
repaired hallucination is a hallucination nobody can see.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Beat(_Model):
    """One step of the arc, tied to a moment that exists."""

    #: Index into the candidate list the Director was shown. An index rather
    #: than a description, because a description would have to be matched back
    #: to a moment by guessing, and guessing is what this design removes.
    moment: int = Field(ge=0)
    #: What this moment is doing in the story: ``hook``, ``setup``,
    #: ``escalation``, ``climax``, ``cooldown``, ``payoff``, ``outro``.
    role: str = Field(min_length=1, max_length=24)
    #: Why, in the Director's words. Never parsed -- shown to a person who
    #: asks why the video opens where it does (§80).
    reason: str = Field(default="", max_length=240)


class Blueprint(_Model):
    """The shape of one edit, proposed by the model and checked by code."""

    #: One line naming what the session was about. Displayed, never parsed.
    theme: str = Field(default="", max_length=200)
    #: The arc, in the order the viewer should meet it.
    beats: tuple[Beat, ...] = ()
    #: Patterns the Director wants avoided, e.g. "three deaths in a row".
    #: Advisory: the deterministic passes already enforce variety (§33), and
    #: this is a hint to them rather than a new rule.
    avoid: tuple[str, ...] = ()

    @field_validator("beats")
    @classmethod
    def _no_repeated_moment(cls, value: tuple[Beat, ...]) -> tuple[Beat, ...]:
        seen = [beat.moment for beat in value]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "A blueprint used the same moment twice. One stretch of footage "
                "shown at two points in the video is a mistake the viewer sees."
            )
        return value

    @property
    def is_empty(self) -> bool:
        return not self.beats

    def summary(self) -> dict[str, object]:
        return {
            "theme": self.theme[:80],
            "beats": len(self.beats),
            "roles": [beat.role for beat in self.beats],
            "avoid": list(self.avoid),
        }


class BlueprintRejection(_Model):
    """Why a proposal was not used, kept so the fallback is explainable.

    §80's rule applied to the model layer: "the deterministic order was used"
    and "the Director proposed a moment that does not exist" are different
    statements about the same video, and only a recorded reason tells them
    apart.
    """

    reason: str
    detail: dict[str, object] = Field(default_factory=dict)


__all__ = ["Beat", "Blueprint", "BlueprintRejection"]
