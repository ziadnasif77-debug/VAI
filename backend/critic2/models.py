"""What a critic that has actually watched the video may say (V2-P7).

The Critic this project already had reviews the EDL *before* anything is
rendered -- a numbered list of clips built from source-side analysis, judged by
a text model, permitted to trim and to drop. That placement was deliberate and
right for what it does: a criticism of a timeline costs a database write, and
the same criticism of a finished MP4 costs a re-render.

It also means nothing in this pipeline has ever looked at the thing a viewer
meets. The defects that live in the assembly -- three similar shots in a row, a
gesture that fires twice in ten seconds, an ending that stops -- are invisible
by construction to every stage that reads the source.

This module is the vocabulary for the one that does. A correction is a closed
verb over a named target with the evidence that produced it, because a critic
that can say anything is a critic nobody can check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal

#: What a correction may ask for. Closed on purpose: every verb here maps to
#: an operation §42 already guarantees, and a verb with no operation behind it
#: is a promise the pipeline cannot keep -- ``weaken_effect`` was listed here
#: with nothing able to emit it and nothing able to perform it, which is the
#: same sin one level up from the map below. Reordering is absent and will stay
#: absent -- the constitution is not a defect to be corrected.
Action = Literal[
    "trim_start",
    "trim_end",
    "drop",
    "remove_effect",
]
ACTIONS: Final[tuple[Action, ...]] = (
    "trim_start",
    "trim_end",
    "drop",
    "remove_effect",
)

#: What the critic is allowed to have found. Free text would make the report
#: readable and the system unmeasurable: these are the names the acceptance
#: gate counts, the analytics layer will group by, and the owner argues with.
DefectCode = Literal[
    "low_intensity_tail",
    "repetition",
    "context_loss",
    "bad_cut",
    "weak_hook",
    "weak_climax",
    "weak_ending",
    "effect_overuse",
    "audio_fatigue",
    "visual_fatigue",
    "style_violation",
]
DEFECTS: Final[tuple[DefectCode, ...]] = (
    "low_intensity_tail",
    "repetition",
    "context_loss",
    "bad_cut",
    "weak_hook",
    "weak_climax",
    "weak_ending",
    "effect_overuse",
    "audio_fatigue",
    "visual_fatigue",
    "style_violation",
)

#: Which verb answers which defect. A defect with no answer is still worth
#: reporting -- the owner may act on it -- but it produces no correction, and
#: saying so is more honest than inventing a trim that does not address it.
ANSWERS: Final[dict[str, tuple[Action, ...]]] = {
    "low_intensity_tail": ("trim_end",),
    "repetition": ("drop",),
    "effect_overuse": ("remove_effect",),
    # The rest are reported and not acted on, each for a reason:
    #
    # A weak hook is a selection problem -- trimming the first shot's head
    # does not make a quiet opening louder, and choosing a different opening
    # is what P6's profiles are for. A weak climax and a weak ending are the
    # same: the footage that would fix them was not selected.
    # Context loss and audio fatigue have no verb in §42's vocabulary at all.
    # Visual fatigue could drop a shot, but a long flat stretch is usually
    # every shot in it, and dropping them one at a time makes a different
    # video rather than a better one.
    "context_loss": (),
    "bad_cut": (),
    "weak_hook": (),
    "weak_climax": (),
    "weak_ending": (),
    "audio_fatigue": (),
    "visual_fatigue": (),
    # A style violation is a selection or a pacing decision that did not match
    # the doctrine the edit was cut under. Neither is correctable after the
    # render: trimming a shot does not make the style right, and re-selecting
    # is a different video. It is reported so the owner can change the style or
    # the doctrine, which is the only thing that would actually answer it.
    "style_violation": (),
}


@dataclass(frozen=True, slots=True)
class Evidence:
    """What was seen, so a correction can be checked rather than believed."""

    #: Where in the finished video, in seconds.
    at_seconds: float
    #: Frames that were looked at, if any.
    frames: tuple[str, ...] = field(default=())
    #: The measurement that triggered it, named and valued.
    measured: dict[str, Any] = field(default_factory=dict)
    #: One sentence a person can read.
    note: str = ""


@dataclass(frozen=True, slots=True)
class EditCorrection:
    """One change, one target, one reason."""

    action: Action
    #: A clip id or an effect id. Checked against the timeline before it is
    #: applied: a target that does not exist is a rejection, never a
    #: nearest-neighbour repair.
    target: str
    reason: DefectCode
    evidence: Evidence
    #: Seconds for a trim, or the fraction to weaken an effect by.
    amount: float = 0.0
    confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "amount": round(self.amount, 3),
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "evidence": {
                "at_seconds": round(self.evidence.at_seconds, 2),
                "frames": list(self.evidence.frames),
                "measured": self.evidence.measured,
                "note": self.evidence.note,
            },
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """Something the critic saw, whether or not anything can be done about it."""

    code: DefectCode
    at_seconds: float
    detail: str
    confidence: float = 0.0
    measured: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "at_seconds": round(self.at_seconds, 2),
            "detail": self.detail,
            "confidence": round(self.confidence, 3),
            "measured": self.measured,
        }


__all__ = [
    "ACTIONS",
    "ANSWERS",
    "DEFECTS",
    "Action",
    "DefectCode",
    "EditCorrection",
    "Evidence",
    "Finding",
]
