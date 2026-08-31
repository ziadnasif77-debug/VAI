"""How the shots sit together (V2-P0b).

Every reading before this one is about a shot. `ShotSemantics` says what one
shot is for; the optimiser scores one moment at a time and adds a variety term
that is a share of the whole, blind to adjacency; the judge's coherence axis
counts long jumps and nothing else. None of them can see the thing an editor
sees first, which is not a shot but a **join** -- two shots and what happens
between them.

Five relations, each read at the seams:

    rhythm              do shot lengths change from one to the next
    contrast            do adjacent shots differ in kind
    continuity          do adjacent shots belong to each other
    repetition          how much the sequence says the same thing twice
    transition quality  do the cuts land where the footage already changes

## No ideals here

Every value is a raw measurement, 0..1, with no target baked in. That is a
deliberate correction: the judge's pacing axis scores shot spread against an
ideal of 1.2, and **no edit this system makes has ever come near it** -- the
house's own spread is 1.888 -- so the axis measures distance from an authored
guess rather than quality. A reading that ships its own opinion repeats that
mistake in five new places.

So this says what is true and a style says what it wants. What "good rhythm"
means is a taste, and taste lives in `config/style.yaml`.

## Derived, never stored

Read from the plan and the editorial reading, both of which the caller already
has. No table, no migration, nothing to invalidate -- the same rule
`backend.editorial.reading` follows, and for the same reason.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.editorial.semantics import ShotPurpose

logger = get_logger("editorial.sequence", LogChannel.PIPELINE)

#: How much two consecutive shots must differ in length before a viewer feels
#: the change.
#:
#: A fifth. Below that the two read as the same length -- a cut from 40s to 36s
#: is not a change of pace, it is the same pace measured twice.
FELT_LENGTH_CHANGE: Final[float] = 0.20

#: How far apart in the source two shots may be and still feel adjacent.
#:
#: Sixty seconds, against the judge's own 240. The judge is asking whether the
#: edit holds together at all; this is asking whether one shot *follows* the
#: other, which is a stricter question and a different number.
CONTINUOUS_GAP_SECONDS: Final[float] = 60.0

#: How close a cut must land to a seam the footage already has to count as
#: landing on it. A cut within half a second of a scene change reads as that
#: scene change; one further away reads as a cut.
SEAM_TOLERANCE: Final[float] = 0.5


@dataclass(frozen=True, slots=True)
class Seam:
    """One join between two shots -- what a viewer actually experiences.

    Named for the join rather than for either shot, because everything here is
    a property of the pair. A seam has no quality of its own to report: the
    five readings are shares across all the seams, and one seam in isolation is
    an anecdote.
    """

    #: The seam after ``moments[index]``.
    index: int
    #: Source seconds between the two, or ``inf`` across recordings.
    gap_seconds: float
    same_media: bool
    same_type: bool
    #: The later shot's length over the earlier one's.
    length_ratio: float
    purpose_before: ShotPurpose | None = None
    purpose_after: ShotPurpose | None = None
    #: Whether the cut lands on a boundary the analysis already found.
    on_seam: bool = False

    @property
    def length_changed(self) -> bool:
        return abs(self.length_ratio - 1.0) >= FELT_LENGTH_CHANGE

    @property
    def continuous(self) -> bool:
        """Whether the later shot reads as following the earlier one."""
        return self.same_media and self.gap_seconds <= CONTINUOUS_GAP_SECONDS

    @property
    def purposeful(self) -> bool:
        """Whether this seam's change of length goes with a change of kind.

        The distinction the judge's pacing axis could not make. A cut from a
        held shot to a brief one is *editing* when the brief shot is doing
        something different -- a reaction after a payoff, a beat after an
        action -- and it is **noise** when the next shot is more of the same
        at a different length.

        Unevenness is not a defect and evenness is not a virtue. What
        separates a cinematic edit from a badly cut one is whether the
        variation is doing work, and that is a property of the pair.
        """
        return self.length_changed and self.differs

    @property
    def arbitrary(self) -> bool:
        """A length change between two shots of the same kind."""
        return self.length_changed and not self.differs

    @property
    def differs(self) -> bool:
        """Whether the two shots are different in kind, by type or by purpose."""
        if not self.same_type:
            return True
        return (
            self.purpose_before is not None
            and self.purpose_after is not None
            and self.purpose_before is not self.purpose_after
        )


@dataclass(frozen=True, slots=True)
class SequenceReading:
    """The five relations, as shares across every seam in the edit.

    All raw. A high `repetition` is not a defect and a low `contrast` is not a
    failure -- a competitive edit wants both, and a variety-led edit wants
    neither. What they are is *true*.
    """

    seams: tuple[Seam, ...] = ()

    #: Share of seams where the shot length changed enough to be felt.
    rhythm: float = 0.0
    #: Of the seams that changed length, the share that also changed kind.
    #:
    #: The reading the judge's pacing axis was missing. An edit can be uneven
    #: because it is cut with intent -- a held payoff, then a brief reaction --
    #: or uneven because nothing decided the lengths. Spread alone cannot tell
    #: those apart, and scoring spread against an ideal marks the first one
    #: down for being the second.
    purposeful_rhythm: float = 0.0
    #: Share of seams joining two shots that differ in kind.
    contrast: float = 0.0
    #: Share of seams where the later shot reads as following the earlier one.
    continuity: float = 0.0
    #: Share of seams joining two shots of the same type. The adjacency the
    #: optimiser's variety term is blind to.
    repetition: float = 0.0
    #: Share of cuts landing on a boundary the footage already had.
    transition_quality: float = 0.0

    #: The longest run of one type, in shots. Adjacency again, as a count
    #: rather than a share, because "seven in a row" is the complaint and
    #: "0.3 of seams" is not.
    longest_same_type_run: int = 0
    #: The longest run of shots none of which changed length noticeably.
    longest_flat_run: int = 0

    #: One sentence per reading, for a person checking the numbers.
    why: tuple[str, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        return not self.seams

    def as_dict(self) -> dict[str, Any]:
        return {
            "seams": len(self.seams),
            "rhythm": round(self.rhythm, 3),
            "purposeful_rhythm": round(self.purposeful_rhythm, 3),
            "contrast": round(self.contrast, 3),
            "continuity": round(self.continuity, 3),
            "repetition": round(self.repetition, 3),
            "transition_quality": round(self.transition_quality, 3),
            "longest_same_type_run": self.longest_same_type_run,
            "longest_flat_run": self.longest_flat_run,
            "why": list(self.why),
        }


def read(moments: Any, reading: Any = None) -> SequenceReading:
    """Read the joins between an edit's shots.

    Args:
        moments: the selected moments, in the order they will be shown.
        reading: the editorial reading, when the caller has one. It supplies
            each shot's purpose and the seams the footage offers; without it
            contrast falls back to moment type alone and transition quality is
            not claimed at all.

    A single-shot edit has no seams and every reading is zero. That is not a
    perfect sequence and not a broken one -- `is_empty` is what says which,
    and a caller that averages the zeros in will conclude the wrong thing.
    """
    shots = list(moments or ())
    if len(shots) < 2:
        return SequenceReading(why=("one shot or none: there are no joins to read",))

    seams = tuple(
        _seam(index, before, after, reading)
        for index, (before, after) in enumerate(pairwise(shots))
    )
    total = len(seams)

    rhythm = sum(1 for seam in seams if seam.length_changed) / total
    changed = [seam for seam in seams if seam.length_changed]
    purposeful = (
        sum(1 for seam in changed if seam.purposeful) / len(changed) if changed else 0.0
    )
    contrast = sum(1 for seam in seams if seam.differs) / total
    continuity = sum(1 for seam in seams if seam.continuous) / total
    repetition = sum(1 for seam in seams if seam.same_type) / total

    measurable = [seam for seam in seams if reading is not None]
    quality = (
        sum(1 for seam in measurable if seam.on_seam) / len(measurable)
        if measurable
        else 0.0
    )

    lengths = [float(shot.context_duration) for shot in shots]
    why = [
        f"rhythm {rhythm:.2f}: {sum(1 for s in seams if s.length_changed)} of "
        f"{total} cuts change the shot length noticeably "
        f"(median shot {statistics.median(lengths):.0f}s)",
        f"purposeful rhythm {purposeful:.2f}: of the {len(changed)} that change "
        f"length, {sum(1 for s in changed if s.purposeful)} also change kind -- "
        "the rest vary for no reason the edit can name",
        f"contrast {contrast:.2f}: {sum(1 for s in seams if s.differs)} of "
        f"{total} join two shots of different kinds",
        f"continuity {continuity:.2f}: {sum(1 for s in seams if s.continuous)} of "
        f"{total} follow on from what came before",
        f"repetition {repetition:.2f}: longest run of one type is "
        f"{_longest(seams, lambda s: s.same_type) + 1} shot(s)",
    ]
    if reading is None:
        why.append("transition quality: not measured, there is no editorial reading")
    else:
        why.append(
            f"transition quality {quality:.2f}: "
            f"{sum(1 for s in measurable if s.on_seam)} of {len(measurable)} cuts "
            "land where the footage already changes"
        )

    return SequenceReading(
        seams=seams,
        rhythm=round(rhythm, 4),
        purposeful_rhythm=round(purposeful, 4),
        contrast=round(contrast, 4),
        continuity=round(continuity, 4),
        repetition=round(repetition, 4),
        transition_quality=round(quality, 4),
        longest_same_type_run=_longest(seams, lambda seam: seam.same_type) + 1,
        longest_flat_run=_longest(seams, lambda seam: not seam.length_changed) + 1,
        why=tuple(why),
    )


def _seam(index: int, before: Any, after: Any, reading: Any) -> Seam:
    """One join, read from what both shots already carry."""
    same_media = before.media_id == after.media_id
    gap = (
        float(after.context_start) - float(before.context_end)
        if same_media
        else float("inf")
    )
    earlier = max(float(before.context_duration), 1e-6)
    return Seam(
        index=index,
        gap_seconds=gap,
        same_media=same_media,
        same_type=before.moment_type is after.moment_type,
        length_ratio=float(after.context_duration) / earlier,
        purpose_before=_purpose(before, reading),
        purpose_after=_purpose(after, reading),
        on_seam=_on_seam(before, reading),
    )


def _purpose(moment: Any, reading: Any) -> ShotPurpose | None:
    if reading is None:
        return None
    semantics = reading.semantics_of(moment)
    return semantics.purpose if semantics is not None else None


def _on_seam(moment: Any, reading: Any) -> bool:
    """Whether this shot's out-point lands on a boundary the footage has."""
    if reading is None:
        return False
    shot = reading.shot(moment)
    if shot is None:
        return False
    edge = float(moment.context_end)
    return any(
        abs(edge - float(point)) <= SEAM_TOLERANCE for point in shot.cuts.out_of
    )


def _longest(seams: tuple[Seam, ...], holds) -> int:
    """The longest consecutive run of seams the predicate holds for."""
    best = current = 0
    for seam in seams:
        current = current + 1 if holds(seam) else 0
        best = max(best, current)
    return best


__all__ = [
    "CONTINUOUS_GAP_SECONDS",
    "FELT_LENGTH_CHANGE",
    "SEAM_TOLERANCE",
    "Seam",
    "SequenceReading",
    "read",
]
