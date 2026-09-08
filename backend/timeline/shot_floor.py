"""No shot shorter than the floor, no band cut faster than its budget (P0.6).

The owner watched the P0.3 gate render and stopped: 288 clips in fifteen
minutes, median 2.1 s, 130 of them under two seconds -- "as if the connection
kept dropping". The jump-cut decision (:mod:`backend.editorial.jump_cuts`)
stopped the cuts that *removed* footage; these two rules govern how short and
how frequent the remaining shots may be. They are the owner's items 2 and 3,
and both numbers were derived by measuring the benchmark rather than chosen
(docs/PLAN.md, 2026-09-08).

**The floor.** A piece under ``min_shot_seconds`` is not a shot; it joins a
neighbour or it is not shown. Applied last, after everything that can shorten
a clip -- the pacing tiers, the exclusions, the duration clamp -- and the
Critic is held to the same number by :func:`backend.semantic.levels.floor_for`,
so it cannot trim a shot below it either.

**The budget.** Each semantic band may spend so many cuts a minute; over that,
its shortest shots join their neighbours until it is inside its ceiling. This
runs at the pacing stage, where the bands are read.

**One merge rule, and it is not chaining.** The *shortest* shot joins the
*shorter* of its two contiguous neighbours, and the search starts again from
the new list. Walking the list once and folding every short piece into the
group being accumulated is a different rule with a worse answer: on the
benchmark it took the climax band to a 15.7 s median -- from a stutter
straight to a slideshow -- while the pairwise rule leaves it at 4.7 s.
:func:`plan` is written to be re-entered from the top for exactly that reason
and ``test_p0_6_the_merge_is_pairwise_not_a_chain`` guards it.

Only pieces of the *same moment* join. Two clips that happen to touch in the
source are two decisions the story made, and merging them would put footage
under a grant that never covered it (P0.3): a merged span lies inside the
governing grant only when both pieces already did, which is guaranteed within
one moment and nothing else. A piece with no such neighbour is dropped, and
what it carried is logged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from backend.core.logging import LogChannel, get_logger
from backend.timeline.builder import PlannedClip

logger = get_logger("timeline.shot_floor", LogChannel.PIPELINE)

#: Two pieces touch when the gap between them is under this. A cut the pacing
#: engine made is exact; a hundredth of a second absorbs the arithmetic.
TOLERANCE: Final[float] = 0.01

#: A stop, not a policy: the loops below always shorten the list, so this only
#: ever catches a bug.
_MAX_ROUNDS: Final[int] = 10_000

#: What a caller passes as a band ceiling: cuts per minute, by band name.
Ceilings = Mapping[str, float]
#: ``(media_id, start, end) -> band name`` for the shot starting there.
BandOf = Callable[[str, float, float], str | None]


@dataclass(frozen=True, slots=True)
class Group:
    """One finished shot: the pieces it was made of, and its bounds."""

    indices: tuple[int, ...]
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass(slots=True)
class _Shot:
    indices: list[int]
    media_id: str
    key: str
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


def plan(
    spans: Sequence[tuple[str, float, float, str]],
    *,
    floor: float = 0.0,
    ceilings: Ceilings | None = None,
    band_of: BandOf | None = None,
) -> tuple[list[Group], list[int], list[str]]:
    """Group ``spans`` into shots that clear the floor and the budget.

    Args:
        spans: ``(media_id, start, end, key)`` in playing order. Two spans may
            join only when they share a media and a key and touch in time.
        floor: the shortest a shot may be.
        ceilings: cuts per minute a band may spend, by band name.
        band_of: which band a shot belongs to; required with ``ceilings``.

    Returns the groups in order, the indices of spans shown by nothing, and
    the notes a reader of the plan should see.
    """
    shots = [
        _Shot([index], media_id, key, start, end)
        for index, (media_id, start, end, key) in enumerate(spans)
    ]
    dropped: list[int] = []
    notes: list[str] = []

    joined = _apply_floor(shots, dropped, floor)
    if joined:
        notes.append(
            f"shot floor: joined {joined} piece(s) shorter than {floor:.1f} s into a neighbour"
        )
    if dropped:
        notes.append(
            f"shot floor: dropped {len(dropped)} piece(s) under {floor:.1f} s with nothing to join"
        )

    if ceilings and band_of is not None:
        capped = _apply_ceilings(shots, ceilings, band_of)
        if capped:
            notes.append(f"jump-cut budget: joined {capped} piece(s) to stay inside the ceilings")

    groups = [Group(tuple(shot.indices), shot.start, shot.end) for shot in shots]
    return groups, dropped, notes


def _apply_floor(shots: list[_Shot], dropped: list[int], floor: float) -> int:
    if floor <= 0.0:
        return 0
    joined = 0
    for _ in range(_MAX_ROUNDS):
        short = [index for index, shot in enumerate(shots) if shot.seconds < floor - 1e-9]
        if not short:
            break
        # The shortest first, and the search starts again after every join:
        # this is what keeps the rule pairwise instead of a chain.
        index = min(short, key=lambda i: (shots[i].seconds, i))
        neighbour = _neighbour(shots, index)
        if neighbour is None:
            dropped.extend(shots[index].indices)
            del shots[index]
            continue
        _join(shots, index, neighbour)
        joined += 1
    return joined


def _apply_ceilings(shots: list[_Shot], ceilings: Ceilings, band_of: BandOf) -> int:
    capped = 0
    for _ in range(_MAX_ROUNDS):
        index = _shortest_over_budget(shots, ceilings, band_of)
        if index is None:
            break
        neighbour = _neighbour(shots, index)
        if neighbour is None:  # pragma: no cover -- filtered by the search
            break
        _join(shots, index, neighbour)
        capped += 1
    return capped


def _shortest_over_budget(
    shots: Sequence[_Shot], ceilings: Ceilings, band_of: BandOf
) -> int | None:
    """The shortest joinable shot in the band furthest over its ceiling."""
    by_band: dict[str, list[int]] = {}
    for index, shot in enumerate(shots):
        band = band_of(shot.media_id, shot.start, shot.end)
        if band is not None:
            by_band.setdefault(band, []).append(index)

    excesses: list[tuple[float, str]] = []
    for band, indices in by_band.items():
        ceiling = ceilings.get(band)
        if ceiling is None or ceiling <= 0:
            continue
        seconds = sum(shots[i].seconds for i in indices)
        if seconds <= 0:
            continue
        density = 60.0 * len(indices) / seconds
        if density > ceiling:
            excesses.append((density - ceiling, band))

    for _excess, band in sorted(excesses, reverse=True):
        for index in sorted(by_band[band], key=lambda i: (shots[i].seconds, i)):
            if _neighbour(shots, index) is not None:
                return index
    return None


def _neighbour(shots: Sequence[_Shot], index: int) -> int | None:
    """The contiguous neighbour this shot joins: the shorter of the two."""
    options: list[int] = []
    if index and _touching(shots[index - 1], shots[index]):
        options.append(index - 1)
    if index + 1 < len(shots) and _touching(shots[index], shots[index + 1]):
        options.append(index + 1)
    if not options:
        return None
    return min(options, key=lambda i: (shots[i].seconds, i))


def _touching(left: _Shot, right: _Shot) -> bool:
    return (
        left.media_id == right.media_id
        and left.key == right.key
        and abs(right.start - left.end) <= TOLERANCE
    )


def _join(shots: list[_Shot], first: int, second: int) -> None:
    low, high = sorted((first, second))
    early, late = shots[low], shots[high]
    shots[low] = _Shot(
        [*early.indices, *late.indices], early.media_id, early.key, early.start, late.end
    )
    del shots[high]


def apply_to_clips(
    clips: Sequence[PlannedClip],
    *,
    floor: float = 0.0,
    ceilings: Ceilings | None = None,
    band_of: BandOf | None = None,
) -> tuple[list[PlannedClip], list[str]]:
    """The floor and the budget over planned clips.

    A joined shot keeps the first piece's identity -- same moment, same
    grants, and its span lies inside them because both pieces did -- and says
    in its ``sources`` how many pieces it was made of (§80).
    """
    if not clips:
        return [], []
    spans = [
        (clip.media_id, clip.source_start, clip.source_end, clip.moment_id or "")
        for clip in clips
    ]
    groups, dropped, notes = plan(spans, floor=floor, ceilings=ceilings, band_of=band_of)

    for index in dropped:
        clip = clips[index]
        logger.info(
            "Dropped a piece too short to be a shot",
            extra={
                "media_id": clip.media_id,
                "start": round(clip.source_start, 2),
                "seconds": round(clip.seconds, 2),
            },
        )

    kept: list[PlannedClip] = []
    for group in groups:
        first = clips[group.indices[0]]
        if len(group.indices) == 1 and abs(first.seconds - group.seconds) <= 1e-9:
            kept.append(first)
            continue
        kept.append(
            replace(
                first,
                source_start=group.start,
                source_end=group.end,
                sources=(
                    *first.sources,
                    f"shot floor: {len(group.indices)} pieces joined into {group.seconds:.1f}s",
                ),
            )
        )
    return kept, notes


__all__ = ["BandOf", "Ceilings", "Group", "apply_to_clips", "plan"]
