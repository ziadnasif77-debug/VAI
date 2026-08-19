"""What the editor has learned about the person using it (Phase F).

The lesson this exists to generalise is written down in `EditingIntent`, next
to the chronological flag:

    As a per-project preference the hook came back on every new project and
    read as "the same errors" -- a default the user has to re-defeat per
    project is not a default.

That one was settled by changing the shipped default, which works exactly once
and only for a preference the author happens to share. Everything else a person
re-types every project -- "make it faster", "fewer effects", "no fail clips" --
is still re-typed every project.

So: a preference is a change the person has made **in several separate
projects**. One instruction is a mood. Three across three projects is how they
want their videos, and the editor should start there.

Two rules shape every type here.

**Evidence, not inference.** A preference names the projects it was learned
from and how many times it was seen. Nothing is derived from a single project,
and nothing is derived from something the person did not actually do.

**It is a default, not a decision.** A learned preference sits between the
preset and the instructions: it beats what shipped in the box, and anything
typed for the current project beats it. A person who says "keep the effects
this time" gets the effects this time, and the preference is untouched.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Preference(_Model):
    """One dimension of the intent the person keeps setting the same way."""

    #: The `EditingIntent` field this is about, e.g. ``pacing``.
    dimension: str = Field(min_length=1, max_length=48)
    #: The value they keep choosing, in the JSON form the delta uses.
    value: Any
    #: How many separate projects showed it. The whole basis of the claim.
    projects: int = Field(ge=1)
    #: Which ones, newest first. Kept so "why is this fast?" has an answer
    #: that names evidence rather than restating the conclusion (§80).
    seen_in: tuple[str, ...] = ()
    #: What the person actually typed, where they typed something. Shown
    #: verbatim; a preference explained in the editor's words rather than the
    #: person's is a preference they will not recognise.
    examples: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "projects": self.projects,
        }

    def sentence(self) -> str:
        """One line, for the person who asks why the edit starts like this."""
        return (
            f"{self.dimension} is {self.value} because you asked for it in {self.projects} projects"
        )


class Preferences(_Model):
    """Everything learned, and what it adds up to."""

    learned: tuple[Preference, ...] = ()
    #: How many finished projects were read to get here. Carried because
    #: "nothing was learned" and "there was nothing to learn from" are
    #: different statements and only one of them is worth showing.
    considered: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.learned

    def by_dimension(self) -> dict[str, Preference]:
        return {preference.dimension: preference for preference in self.learned}

    def summary(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "learned": [preference.summary() for preference in self.learned],
        }

    def sentences(self) -> list[str]:
        return [preference.sentence() for preference in self.learned]


__all__ = ["Preference", "Preferences"]
