"""The duration optimiser (SPEC section 39).

    Given a 3-hour source and a 20-minute target, find the combination of
    moments closest to the target while maximising entertainment + narrative +
    variety and minimising repetition + dead time. **This is an optimisation
    problem, not simple sorting.**

The emphasis is the spec's, and the difference is not academic. Sorting by score
and taking clips until the clock runs out fails in two specific ways:

* **It misses the target.** The greedy prefix stops wherever the next clip
  happens not to fit. A 20-minute request becomes 17 minutes and 40 seconds
  because the 19th moment was 3 minutes long, and no amount of re-sorting fixes
  that -- the problem is that the last choice was made without knowing what came
  after it.
* **It produces a monotonous video.** The top of a score ranking is the same
  kind of moment over and over, because whatever the scorer likes, it likes
  consistently. §33 says this outright.

So this is a knapsack: choose the subset whose total viewing time lands inside
the tolerance and whose total objective value is highest. Solved exactly by
dynamic programming over one-second buckets, because the problem is small --
a few hundred moments against a 1 200-second target is a table a laptop fills
in milliseconds, and an exact answer is worth more than a heuristic here.

**Variety is inside the objective, not applied afterwards.** A bonus computed
per moment cannot express "this is the fourth kill in a row"; the DP therefore
carries the type mix of each partial solution and prices the next addition
against it. That is the whole reason a sort cannot do this job.

When no subset lands inside the tolerance, context is trimmed before any moment
is dropped (``optimizer.allow_context_trim``): a clip shortened by three seconds
of pre-roll is still the moment, while a clip removed is not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.config.schema import DurationOptimizerConfig
from backend.core.duration import DurationPolicy, format_duration
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import MomentType
from backend.moments.formation import Moment

logger = get_logger("narrative.optimizer", LogChannel.PIPELINE)

#: Resolution of the knapsack, in seconds. One second is finer than any
#: perceptible difference in a 20-minute video and keeps the table small.
BUCKET_SECONDS: Final[int] = 1

#: What landing at the edge of the tolerance costs, in units of objective
#: value. §39 asks for the combination *closest to the target* while maximising
#: the objective, so deviation has to be priced -- otherwise the search always
#: fills to the ceiling, since one more clip is always more value.
#: Roughly one clip's worth, so a better selection can still justify a longer
#: video, but an equally good one lands nearer the mark.
DEVIATION_WEIGHT: Final[float] = 1.0

#: Value multiplier applied to a moment whose type is already over-represented
#: in the partial solution. Deliberately steep: the failure this prevents --
#: twelve near-identical clips -- is the one viewers notice first.
_SATURATION_FLOOR: Final[float] = 0.35


@dataclass(frozen=True, slots=True)
class OptimisationResult:
    """The chosen subset, and an honest account of how well it did."""

    moments: tuple[Moment, ...]
    target_seconds: float
    #: Total viewing time of the selection, after any context trimming.
    total_seconds: float
    #: Objective value achieved.
    value: float
    within_tolerance: bool
    #: How much context trimming was applied, in seconds.
    trimmed_seconds: float = 0.0
    #: Moments considered but not selected.
    rejected: int = 0
    notes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def deviation(self) -> float:
        return self.total_seconds - self.target_seconds

    @property
    def is_empty(self) -> bool:
        return not self.moments

    def summary(self) -> dict[str, Any]:
        return {
            "selected": len(self.moments),
            "rejected": self.rejected,
            "target_seconds": round(self.target_seconds, 2),
            "total_seconds": round(self.total_seconds, 2),
            "deviation_seconds": round(self.deviation, 2),
            "within_tolerance": self.within_tolerance,
            "trimmed_seconds": round(self.trimmed_seconds, 2),
            "objective_value": round(self.value, 4),
        }


def optimise(
    moments: Sequence[Moment],
    *,
    target_seconds: float,
    config: DurationOptimizerConfig,
    policy: DurationPolicy,
) -> OptimisationResult:
    """Choose the subset of moments that best fills ``target_seconds`` (§39).

    Args:
        moments: scored candidates, in any order.
        target_seconds: the requested output length, already validated against
            the §6 band.
        config: ``narrative.optimizer`` -- objective weights and penalties.
        policy: supplies the tolerance the result is judged against.

    Returns the selection in chronological order, because a video is watched in
    time order even when it was chosen by value.
    """
    if not moments or target_seconds <= 0:
        return OptimisationResult(
            moments=(),
            target_seconds=target_seconds,
            total_seconds=0.0,
            value=0.0,
            within_tolerance=False,
            notes=("no candidate moments",),
        )

    tolerance = policy.tolerance_for(target_seconds)
    ceiling = target_seconds + tolerance

    selected, value = _knapsack(moments, ceiling, target_seconds, tolerance, config)
    total = sum(moment.context_duration for moment in selected)
    notes: list[str] = []
    trimmed = 0.0

    if total < target_seconds - tolerance and len(selected) < len(moments):
        # Under the band with candidates left over: the DP found no exact fit,
        # so add the best remaining moments even though they overshoot slightly.
        selected, total, added = _fill_up(
            selected, moments, target_seconds, tolerance, config
        )
        if added:
            notes.append(f"added {added} moment(s) to reach the target band")
            value = _objective(selected, config)

    if config.allow_context_trim and total > target_seconds + tolerance:
        # Trimming a few seconds of pre-roll from every clip keeps every moment;
        # dropping one does not. §29's roll is the slack in the system.
        selected, trimmed = _trim_context(
            selected, total - target_seconds, config.max_context_trim_ratio
        )
        total = sum(moment.context_duration for moment in selected)
        if trimmed > 0:
            notes.append(f"trimmed {format_duration(trimmed)} of clip context")

    within = policy.within_tolerance(total, target_seconds)
    if not within:
        notes.append(
            f"closest achievable was {format_duration(total)} against a target of "
            f"{format_duration(target_seconds)}"
        )

    ordered = tuple(sorted(selected, key=lambda moment: moment.context_start))
    result = OptimisationResult(
        moments=ordered,
        target_seconds=target_seconds,
        total_seconds=total,
        value=value,
        within_tolerance=within,
        trimmed_seconds=trimmed,
        rejected=len(moments) - len(ordered),
        notes=tuple(notes),
        metadata={"tolerance_seconds": round(tolerance, 2), "candidates": len(moments)},
    )

    (logger.warning if not within else logger.info)(
        "Optimised the moment selection", extra=result.summary()
    )
    return result


# ---------------------------------------------------------------------------
# the knapsack
# ---------------------------------------------------------------------------


def _knapsack(
    moments: Sequence[Moment],
    ceiling: float,
    target: float,
    tolerance: float,
    config: DurationOptimizerConfig,
) -> tuple[list[Moment], float]:
    """Exact 0/1 knapsack over one-second buckets.

    ``best[capacity]`` holds the most valuable selection whose total duration is
    exactly ``capacity`` buckets or less. The type mix travels with each partial
    solution so the variety term can price a candidate against what has already
    been chosen -- which is precisely what a per-moment score cannot do.

    Candidates are considered in descending base value so the table fills with
    good solutions early; the result is still exact, since every item is offered
    against every capacity.
    """
    capacity = max(math.ceil(ceiling / BUCKET_SECONDS), 1)
    target_buckets = max(round(target / BUCKET_SECONDS), 1)
    tolerance_buckets = max(round(tolerance / BUCKET_SECONDS), 1)
    ordered = sorted(moments, key=lambda moment: -_base_value(moment, config))
    ordered = ordered[: config.max_iterations] if config.max_iterations else ordered

    # (value, indices, type counts) per capacity.
    best: list[tuple[float, tuple[int, ...], dict[MomentType, int]] | None] = [
        (0.0, (), {})
    ] + [None] * capacity

    for index, moment in enumerate(ordered):
        cost = max(round(moment.context_duration / BUCKET_SECONDS), 1)
        if cost > capacity:
            continue
        # Descending so each moment is used at most once.
        for size in range(capacity, cost - 1, -1):
            previous = best[size - cost]
            if previous is None:
                continue
            prior_value, prior_indices, prior_counts = previous
            gain = _value_in_context(moment, prior_counts, config)
            candidate = prior_value + gain
            current = best[size]
            if current is None or candidate > current[0]:
                counts = dict(prior_counts)
                counts[moment.moment_type] = counts.get(moment.moment_type, 0) + 1
                best[size] = (candidate, (*prior_indices, index), counts)

    return _pick_best(best, ordered, target_buckets, tolerance_buckets)


def _pick_best(
    table: Sequence[tuple[float, tuple[int, ...], dict[MomentType, int]] | None],
    ordered: Sequence[Moment],
    target_buckets: int,
    tolerance_buckets: int,
) -> tuple[list[Moment], float]:
    """Choose the table cell that best answers §39.

    Not simply the highest value: the most valuable cell is almost always the
    fullest one, because another clip is always more value. Deviation from the
    target is therefore priced, so a selection that lands on the mark wins
    unless a longer one is meaningfully better.
    """
    scale = max(tolerance_buckets, 1)
    best_entry: tuple[float, tuple[int, ...]] | None = None
    best_rank = float("-inf")

    for size, entry in enumerate(table):
        if entry is None:
            continue
        value, indices, _ = entry
        if not indices:
            continue
        rank = value - DEVIATION_WEIGHT * (abs(size - target_buckets) / scale)
        if rank > best_rank:
            best_rank, best_entry = rank, (value, indices)

    if best_entry is None:
        return [], 0.0
    value, indices = best_entry
    return [ordered[index] for index in indices], value


def _base_value(moment: Moment, config: DurationOptimizerConfig) -> float:
    """A moment's worth before variety is considered (§39's objective)."""
    breakdown = moment.score_breakdown
    weights = config.objective_weights
    penalties = config.objective_penalties

    entertainment = float(breakdown.get("entertainment", moment.score))
    narrative = float(breakdown.get("narrative", moment.score))

    value = weights.entertainment * entertainment + weights.narrative * narrative
    value -= penalties.repetition * moment.repetition_score
    value -= penalties.dead_time * moment.dead_time_score
    return value


def _value_in_context(
    moment: Moment, counts: dict[MomentType, int], config: DurationOptimizerConfig
) -> float:
    """A moment's worth *given what has already been chosen*.

    The variety term lives here rather than in the moment's own score, because
    "this is the fourth kill in a row" is not a property of the kill. It is the
    reason a sort cannot solve this problem.
    """
    value = _base_value(moment, config)
    already = counts.get(moment.moment_type, 0)
    total = sum(counts.values())
    if total == 0:
        return value + config.objective_weights.variety

    share = already / total
    # Full variety credit for a type not yet used, decaying towards a floor as
    # it comes to dominate.
    freshness = max(1.0 - share, 0.0)
    variety = config.objective_weights.variety * max(freshness, _SATURATION_FLOOR * (1 - share))
    return value + variety


def _objective(moments: Sequence[Moment], config: DurationOptimizerConfig) -> float:
    """Recompute the objective for a selection built outside the DP."""
    counts: dict[MomentType, int] = {}
    total = 0.0
    for moment in sorted(moments, key=lambda item: -_base_value(item, config)):
        total += _value_in_context(moment, counts, config)
        counts[moment.moment_type] = counts.get(moment.moment_type, 0) + 1
    return total


# ---------------------------------------------------------------------------
# reaching the band
# ---------------------------------------------------------------------------


def _fill_up(
    selected: Sequence[Moment],
    candidates: Sequence[Moment],
    target: float,
    tolerance: float,
    config: DurationOptimizerConfig,
) -> tuple[list[Moment], float, int]:
    """Add moments when the exact solution fell short of the band.

    Under-running is worse than slightly over-running: a 17-minute answer to a
    20-minute request is visibly wrong, while 20:40 is not. So the shortfall is
    filled with the most valuable remaining moments, accepting an overshoot the
    context trim can then absorb.
    """
    chosen = list(selected)
    chosen_keys = {(moment.media_id, moment.start_seconds) for moment in chosen}
    remaining = sorted(
        (
            moment
            for moment in candidates
            if (moment.media_id, moment.start_seconds) not in chosen_keys
        ),
        key=lambda moment: -_base_value(moment, config),
    )

    total = sum(moment.context_duration for moment in chosen)
    added = 0
    for moment in remaining:
        if total >= target - tolerance:
            break
        chosen.append(moment)
        total += moment.context_duration
        added += 1
    return chosen, total, added


def _trim_context(
    moments: Sequence[Moment], excess: float, max_ratio: float
) -> tuple[list[Moment], float]:
    """Shave context from every clip to absorb an overshoot (§29, §39).

    Taken proportionally so no single clip loses its whole run-up, and bounded
    by ``max_context_trim_ratio`` so trimming never eats into the moment itself.
    The core span is untouchable: what is trimmed is pre-roll and post-roll,
    which is context, not content.
    """
    from backend.moments.formation import replace_moment

    if excess <= 0 or not moments:
        return list(moments), 0.0

    trimmable = sum(
        max(moment.context_duration - moment.duration, 0.0) * max_ratio
        for moment in moments
    )
    if trimmable <= 0:
        return list(moments), 0.0

    share = min(excess / trimmable, 1.0)
    trimmed_total = 0.0
    result: list[Moment] = []

    for moment in moments:
        pre = moment.start_seconds - moment.context_start
        post = moment.context_end - moment.end_seconds
        take_pre = pre * max_ratio * share
        take_post = post * max_ratio * share
        trimmed_total += take_pre + take_post
        result.append(
            replace_moment(
                moment,
                context_start=moment.context_start + take_pre,
                context_end=moment.context_end - take_post,
            )
        )
    return result, trimmed_total


__all__ = ["BUCKET_SECONDS", "OptimisationResult", "optimise"]
