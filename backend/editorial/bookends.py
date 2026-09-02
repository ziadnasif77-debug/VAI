"""Where the video starts and where it stops (V2-P1).

Two decisions the pipeline has never made, for opposite reasons.

**The opening.** `choose_hook` picks the strongest moment in the session and
moves it to the front — a flash-forward, the standard YouTube cold open. In a
chronological edit that is a reorder, so `story.build_plan` refuses it outright
and substitutes an empty selection reading *"the edit was asked to run in time
order"*. Measured across 102 plans on this machine, that is **every single
one**: `chronological` defaults to true because the owner asked for time order
three separate times, so the hook subsystem is not weak here, it is switched
off, and hook strength is 0.0000 before and after V2-P0 for that reason alone.

What was lost with it is not the flash-forward. It is the *question* — a
chronological edit opens on whatever happened first, and what happened first is
often the weakest thing in the session. A viewer who leaves in eight seconds
never reaches the good part, whatever order it is in.

**The ending.** `repair_sequence` drops a weak final clip, once, as the third
of three repairs, and only when the duration band allows. Nothing decides where
a video should *stop*.

## Choosing boundaries is not reordering

Both decisions here work by moving the edit's first and last index. Nothing is
lifted out of sequence, nothing is shown early, and the chronology constitution
is untouched — a prefix that is dropped was never going to be reordered, it was
going to be watched first.

That makes this available to a chronological edit, which is what every edit on
this machine is, and it needs no exception to any rule.

## What it refuses to do

**It will not open on an outcome.** Dropping a prefix to reach a stronger start
is trimming; skipping past the setup to open on the victory is a flash-forward
wearing a different name, and the constitution says no. So an opening candidate
must not be an outcome type, and the shots dropped before it must not be the
ones that explain it — a `SETUP` or an `ANTICIPATION` shot is kept even when it
is weak, because it is why the next shot lands.

**It will not empty the video.** Both ends are bounded as a share of the edit,
because a policy that could trim half a video from each end is not a taste, it
is a defect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.editorial.semantics import ShotPurpose
from backend.narrative.hook import HOOK_STRENGTH, OUTCOME_TYPES

logger = get_logger("editorial.bookends", LogChannel.PIPELINE)

#: The most of an edit either end may give up, as a share of its shots.
#:
#: A fifth. Enough to cut past a slow start of two or three shots in a
#: twenty-shot edit; not enough for "trim the weak part" to become "choose a
#: different video". The optimiser chooses the video.
MAX_TRIM_SHARE: Final[float] = 0.20

#: How much stronger a later shot must be before it is worth starting there.
#:
#: A shot that is barely better is not better -- moving the start for a 3 %
#: gain throws away real footage for a difference nobody can see, and it is
#: exactly how a bounded policy turns into a creeping one.
WORTH_MOVING: Final[float] = 0.15


@dataclass(frozen=True, slots=True)
class BookendPolicy:
    """What a style asks about where its videos begin and end.

    Neutral means the edit starts at its first shot and ends at its last,
    which is what every edit this machine has made has done.
    """

    #: Move the start past shots weaker than what follows them.
    trim_weak_opening: bool = False
    #: Stop before trailing shots weaker than what preceded them.
    trim_weak_ending: bool = False
    #: How much of the edit either end may give up.
    max_share: float = MAX_TRIM_SHARE

    @property
    def is_neutral(self) -> bool:
        return not (self.trim_weak_opening or self.trim_weak_ending)

    def describe(self) -> str:
        if self.is_neutral:
            return "starts at the first shot and ends at the last"
        ends = [
            name
            for name, asked in (
                ("opening", self.trim_weak_opening),
                ("ending", self.trim_weak_ending),
            )
            if asked
        ]
        return f"trims a weak {' and '.join(ends)}, up to {self.max_share:.0%} of the edit"


NEUTRAL: Final[BookendPolicy] = BookendPolicy()


@dataclass(frozen=True, slots=True)
class Bookends:
    """Where this edit should begin and end, and why."""

    #: Index of the first shot to show.
    start: int = 0
    #: Index one past the last shot to show, in slice terms.
    stop: int = 0

    opening_reason: str = ""
    ending_reason: str = ""

    @property
    def moved(self) -> bool:
        return bool(self.opening_reason or self.ending_reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "stop": self.stop,
            "opening": self.opening_reason,
            "ending": self.ending_reason,
        }


def read(moments: Any, policy: BookendPolicy, reading: Any = None) -> Bookends:
    """Decide where this edit begins and ends.

    Returns indices rather than a new list, so the caller keeps the one
    responsibility that matters — nothing here can drop a shot without the
    caller agreeing to a slice it can see.
    """
    shots = list(moments or ())
    stop = len(shots)
    if policy.is_neutral or len(shots) < 3:
        return Bookends(start=0, stop=stop)

    budget = max(1, int(len(shots) * policy.max_share))
    start, opening = (
        _opening(shots, budget, reading) if policy.trim_weak_opening else (0, "")
    )
    stop, ending = (
        _ending(shots, start, budget, reading)
        if policy.trim_weak_ending
        else (stop, "")
    )
    return Bookends(
        start=start, stop=stop, opening_reason=opening, ending_reason=ending
    )


def apply(moments: Any, bookends: Bookends) -> Any:
    """The edit between its bookends, or the caller's own list when they moved nothing."""
    shots = list(moments or ())
    if not bookends.moved or (bookends.start, bookends.stop) == (0, len(shots)):
        return moments
    return shots[bookends.start : bookends.stop]


def apply_to_plan(plan: Any, bookends: Bookends) -> Any:
    """The plan between its bookends, with everything indexed by shot moved too.

    Returns the caller's own plan when nothing moved. `beats` is sliced with
    the moments because it is indexed by them, and the optimisation result is
    left alone with a note saying so: its deviation described the selection the
    optimiser made, and after a trim it describes a plan that no longer exists.
    Recomputing it here would be this module claiming to have re-run the
    optimiser, which it has not.
    """
    import dataclasses

    shots = list(plan.moments)
    if not bookends.moved or (bookends.start, bookends.stop) == (0, len(shots)):
        return plan

    kept = tuple(shots[bookends.start : bookends.stop])
    if not kept:
        return plan
    beats = tuple(plan.beats[bookends.start : bookends.stop]) if plan.beats else ()
    notes = tuple(
        note
        for note in (bookends.opening_reason, bookends.ending_reason)
        if note
    )
    return dataclasses.replace(
        plan,
        moments=kept,
        beats=beats,
        notes=(
            *plan.notes,
            *notes,
            "the optimiser's deviation above describes the untrimmed selection",
        ),
    )


# -- the two ends ------------------------------------------------------------


def _opening(shots: list, budget: int, reading: Any) -> tuple[int, str]:
    """The first shot worth opening on.

    Looks only inside the budget, keeps anything that explains what follows,
    and refuses to start on an outcome — beginning at the victory is a
    flash-forward however it was arrived at.
    """
    best_at, best_gain = 0, 0.0
    opening = _strength(shots[0])
    for index in range(1, min(budget + 1, len(shots))):
        candidate = shots[index]
        if candidate.moment_type in OUTCOME_TYPES:
            continue
        if _explains(shots[:index], candidate, reading):
            # The walk-up to the ambush is why the ambush lands. Stop rather
            # than continue: everything further on is only reachable by
            # dropping this, and dropping it is what the rule forbids.
            break
        gain = _strength(candidate) - opening
        if gain > best_gain:
            best_at, best_gain = index, gain

    if best_gain < WORTH_MOVING:
        return 0, ""
    dropped = shots[:best_at]
    return best_at, (
        f"starts {best_at} shot(s) in: opening on "
        f"{shots[best_at].moment_type.value} ({_strength(shots[best_at]):.2f}) "
        f"rather than {shots[0].moment_type.value} ({opening:.2f}); "
        f"{sum(m.context_duration for m in dropped):.0f}s of weak lead-in dropped"
    )


def _ending(shots: list, start: int, budget: int, reading: Any) -> tuple[int, str]:
    """Where to stop, so the video ends on something rather than trailing off.

    §16's rule -- a video should end on strength -- as a boundary rather than
    as a reorder. `repair_sequence` already drops a single weak final clip when
    the duration band allows; this is the same intent applied to a trailing
    *run*, and it is the one place a reaction is worth more than its score.
    """
    stop = len(shots)
    floor = max(start + 2, stop - budget)
    ending = _strength(shots[stop - 1])
    best_stop, best_gain = stop, 0.0
    for candidate_stop in range(stop - 1, floor - 1, -1):
        if _is_reaction(shots[candidate_stop], reading):
            # A reaction is how an edit stops feeling like it ran out of
            # footage, so it is never trimmed for being quiet. The shot being
            # *dropped* is the one to ask about -- the first version asked
            # about the new last shot instead, which meant the guard could
            # only fire after the reaction had already gone.
            break
        last = shots[candidate_stop - 1]
        gain = _strength(last) - ending
        if gain > best_gain:
            best_stop, best_gain = candidate_stop, gain

    if best_gain < WORTH_MOVING:
        return stop, ""
    dropped = shots[best_stop:]
    return best_stop, (
        f"stops {stop - best_stop} shot(s) early: ending on "
        f"{shots[best_stop - 1].moment_type.value} ({_strength(shots[best_stop - 1]):.2f}) "
        f"rather than {shots[stop - 1].moment_type.value} ({ending:.2f}); "
        f"{sum(m.context_duration for m in dropped):.0f}s of trailing-off dropped"
    )


def _strength(moment: Any) -> float:
    """How strong a shot is as a bookend.

    The moment's own score weighted by what its type is worth in an opening --
    `HOOK_STRENGTH`, reused rather than reinvented, because "what opens well"
    is a judgement this project already made and writing a second one would
    give the system two opinions about the same question.
    """
    weight = HOOK_STRENGTH.get(moment.moment_type, 0.5)
    return float(moment.score) * weight


def _explains(dropped: list, candidate: Any, reading: Any) -> bool:
    """Whether opening on `candidate` would throw away its own setup.

    The first version asked only whether a dropped shot was a SETUP, and that
    was too broad by a mile: `ShotPurpose.SETUP` means several distinct things
    were on screen, which is true of most gameplay, and it made the first shot
    of 14 projects in 17 unmovable. A shot being *establishing* is not the same
    fact as a shot being **necessary to the one after it**.

    The narrower question is answerable, because the editorial reading already
    groups related episodes into situations: a setup that shares a situation
    with the candidate is its run-up, and one that does not is a different
    scene that merely happened to come first.
    """
    if reading is None:
        return False
    target = reading.shot(candidate)
    situation = getattr(target, "situation_id", "") if target else ""
    if not situation:
        return False
    for moment in dropped:
        semantics = reading.semantics_of(moment)
        if semantics is None or semantics.purpose not in (
            ShotPurpose.SETUP,
            ShotPurpose.ANTICIPATION,
        ):
            continue
        shot = reading.shot(moment)
        if shot is not None and shot.situation_id == situation:
            return True
    return False


def _is_reaction(moment: Any, reading: Any) -> bool:
    if reading is None:
        return False
    semantics = reading.semantics_of(moment)
    return semantics is not None and semantics.purpose is ShotPurpose.REACTION


__all__ = [
    "MAX_TRIM_SHARE",
    "NEUTRAL",
    "WORTH_MOVING",
    "BookendPolicy",
    "Bookends",
    "apply",
    "apply_to_plan",
    "read",
]
