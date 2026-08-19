"""What the Critic may say about a finished edit, and what it may not (Phase E).

Everything upstream of here judges the *source*. The scorer read events, the
optimiser read durations, the Director read a list of moments. Nothing has ever
looked at the thing that was actually assembled and asked whether it is any
good -- which is the one question a viewer asks.

The Critic asks it, and is held to the same rule as every other model in this
pipeline:

    A note must name a clip that exists and ask for a change the timeline can
    already make.

So a note carries a clip index into the list the Critic was shown, one action
from a closed set, and a reason written for the person who will read it. There
is no free-text instruction, because a free-text instruction cannot be checked
and an unchecked instruction from a 7B model is how an edit acquires a cut
nobody asked for.

The actions are deliberately few and all conservative:

``keep``
    Nothing to do. Kept rather than dropped, because "this clip was reviewed
    and is fine" and "nobody looked at this clip" are different statements
    (§76's reasoning applied to the edit).
``trim_start`` / ``trim_end``
    Take seconds off one end. The most common real defect in a generated edit
    -- a clip that begins on a loading screen or runs on after the moment is
    over -- and the safest fix, because it changes nothing about which footage
    is in the video, only how much.
``drop``
    Take the clip out. The strongest action, and the one §39 gets a veto over:
    a video of the wrong length is a worse defect than the one being removed.

Nothing here re-cuts, reorders or invents. The Director already chose the
clips and time already chose the order.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Action(str, Enum):
    """What a note asks for. A closed set, on purpose."""

    KEEP = "keep"
    TRIM_START = "trim_start"
    TRIM_END = "trim_end"
    DROP = "drop"

    @property
    def changes_the_edit(self) -> bool:
        return self is not Action.KEEP


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Note(_Model):
    """One criticism of one clip."""

    #: Index into the list of clips the Critic was shown, in the order it was
    #: shown them. An index rather than a description, for the same reason the
    #: Director uses one: matching a description back to a clip means guessing.
    clip: int = Field(ge=0)
    action: Action = Action.KEEP
    #: Seconds to take off, for the trims. Ignored by the other actions.
    seconds: float = Field(default=0.0, ge=0.0)
    #: Why, in the Critic's words. Never parsed; shown to the person who asks
    #: what changed and why (§80).
    reason: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def _trims_need_a_length(self) -> Note:
        if self.action in {Action.TRIM_START, Action.TRIM_END} and self.seconds <= 0:
            raise ValueError("a trim has to say how many seconds to take off")
        return self

    @property
    def acts(self) -> bool:
        return self.action.changes_the_edit


class Critique(_Model):
    """One reading of one edit."""

    #: One line on how the video plays as a whole. Displayed, never parsed.
    verdict: str = Field(default="", max_length=300)
    notes: tuple[Note, ...] = ()

    @model_validator(mode="after")
    def _one_note_per_clip(self) -> Critique:
        seen = [note.clip for note in self.notes]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "A critique gave the same clip two notes. Which one applies is "
                "not something the code should be guessing."
            )
        return self

    @property
    def is_empty(self) -> bool:
        return not self.notes

    @property
    def actionable(self) -> tuple[Note, ...]:
        return tuple(note for note in self.notes if note.acts)

    def summary(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict[:120],
            "notes": len(self.notes),
            "actions": [note.action.value for note in self.actionable],
        }


class CritiqueRejection(_Model):
    """Why a reading was not used, kept so the outcome is explainable (§80)."""

    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)


__all__ = ["Action", "Critique", "CritiqueRejection", "Note"]
