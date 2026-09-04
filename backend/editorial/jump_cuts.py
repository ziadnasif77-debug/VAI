"""The jump-cut decision, asked once and answered with a reason (P0.6).

The brief (docs/BRIEF_P0.md, PHASE E) changes the question from "can this gap
be removed?" to a decision that reads the evidence. A jump cut -- footage
skipped between two pieces that play back to back -- is allowed only when

1. the removed region is genuinely dead,
2. the gap is long enough to matter,
3. it is not inside a spoken word,
4. it does not remove an event onset,
5. it does not remove a reaction,
6. the sequence has not already spent its jump-cut budget.

Every refusal names its condition, and a refused cut keeps the footage: the
pieces play contiguously, which a viewer cannot feel as a cut at all. Two
callers ask, both in :mod:`backend.timeline.screen_guard`: the piece walker,
which used to skip a sliver of live footage after every hot cut so the cut
would be *felt* -- 268 of those on the P0.3 gate render, 21 of them skipping
an event's first frames -- and the interior excision, which removes a dead
screen from a clip's middle.

"Genuinely dead" is evidence, not absence: a content state the exclusion
layer read as not-gameplay (a menu, a loading screen, a dead screen), or a
dead-time verdict a caller holds. A sliver of live combat is not dead because
nobody sampled it. The budget is a mechanism here; its values are derived
from measurement on the benchmark and recorded in PLAN.md before they enter
the YAML (owner, 2026-09-04), so a caller without a budget runs unbudgeted
and says so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True, slots=True)
class Evidence:
    """What is known about the footage a cut would skip."""

    #: Spoken words, as (start, end) in source seconds. A cut edge inside one
    #: is heard.
    words: Sequence[tuple[float, float]] = ()
    #: Event onsets in source seconds. Skipping one removes the cause.
    onsets: Sequence[float] = ()
    #: Reactions, as (start, end): a laugh, a shout, the beat after a kill.
    reactions: Sequence[tuple[float, float]] = ()
    #: Stretches read as dead screen or dead time, as (start, end).
    dead: Sequence[tuple[float, float]] = ()


@dataclass(slots=True)
class Budget:
    """Jump cuts per minute a sequence may spend, counted as they are spent.

    ``per_minute`` is the ceiling; ``spent`` records the cuts taken as
    (source position). The window is the last sixty seconds of source before
    the cut being asked about, so a burst is priced where it happens.
    """

    per_minute: float
    spent: list[float] = field(default_factory=list)

    def would_exceed(self, at: float) -> bool:
        recent = sum(1 for when in self.spent if at - 60.0 < when <= at)
        return recent + 1 > self.per_minute

    def spend(self, at: float) -> None:
        self.spent.append(at)


def decide(
    start: float,
    end: float,
    evidence: Evidence,
    *,
    min_gap_seconds: float,
    budget: Budget | None = None,
) -> Verdict:
    """Whether the footage in ``[start, end)`` may be skipped between two pieces.

    The conditions are checked in the brief's order and the first failure is
    the reason; a cut that passes all six spends the budget it was given.
    """
    if end <= start:
        return Verdict(False, "nothing to remove")
    length = end - start
    if not _covered(start, end, evidence.dead):
        return Verdict(False, f"the {length:.2f} s it would skip is live footage, not dead")
    if length < min_gap_seconds:
        return Verdict(
            False, f"a {length:.2f} s gap is shorter than the {min_gap_seconds:.2f} s that matters"
        )
    for edge in (start, end):
        word = _inside(edge, evidence.words)
        if word is not None:
            return Verdict(
                False,
                f"the cut at {edge:.2f} s lands inside a word ({word[0]:.2f}-{word[1]:.2f} s)",
            )
    lost = [onset for onset in evidence.onsets if start <= onset < end]
    if lost:
        return Verdict(False, f"it would remove an event onset at {lost[0]:.2f} s")
    for r_start, r_end in evidence.reactions:
        if r_start < end and r_end > start:
            return Verdict(False, f"it would remove a reaction ({r_start:.2f}-{r_end:.2f} s)")
    if budget is not None and budget.would_exceed(start):
        return Verdict(
            False, f"the sequence has spent its {budget.per_minute:g} jump cuts a minute"
        )
    if budget is not None:
        budget.spend(start)
    return Verdict(True, f"skipped {length:.2f} s of dead footage")


def _covered(start: float, end: float, dead: Sequence[tuple[float, float]]) -> bool:
    """Whether ``[start, end]`` lies inside the union of ``dead`` stretches."""
    cursor = start
    for d_start, d_end in sorted(dead):
        if d_start > cursor + 1e-6:
            if d_start >= end:
                break
            return False
        cursor = max(cursor, d_end)
        if cursor >= end - 1e-6:
            return True
    return cursor >= end - 1e-6


def _inside(at: float, spans: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    for span in spans:
        if span[0] < at < span[1]:
            return span
    return None


__all__ = ["Budget", "Evidence", "Verdict", "decide"]
