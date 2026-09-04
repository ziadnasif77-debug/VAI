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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.config.schema import DurationOptimizerConfig
from backend.core.duration import DurationPolicy, format_duration
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import MomentType
from backend.moments.formation import Moment
from backend.narrative.situations import (
    absorb_onsets,
    committed_duration,
    overlaps,
    overlaps_any,
)

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
    media_durations: Mapping[str, float] | None = None,
) -> OptimisationResult:
    """Choose the subset of moments that best fills ``target_seconds`` (§39).

    Args:
        moments: scored candidates, in any order.
        target_seconds: the requested output length, already validated against
            the §6 band.
        config: ``narrative.optimizer`` -- objective weights and penalties.
        policy: supplies the tolerance the result is judged against.
        media_durations: source length per recording, so context can only grow
            into footage that exists. Without it, growth stops at the last
            moment's own edges rather than guessing where a recording ends.

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
    onsets_kept = 0

    if total < target_seconds - tolerance and len(selected) < len(moments):
        # Under the band with candidates left over: the DP found no exact fit,
        # so add the best remaining moments even though they overshoot slightly.
        selected, total, added = _fill_up(
            selected, moments, target_seconds, tolerance, config
        )
        if added:
            notes.append(f"added {added} moment(s) to reach the target band")
            value = _objective(selected, config)

    # P0.6: a dropped moment that overlaps a chosen one hands its onsets over
    # before the target is reconciled, so the trim below sees them as core.
    chosen_keys = {(m.media_id, m.start_seconds, m.end_seconds) for m in selected}
    dropped = [
        m for m in moments if (m.media_id, m.start_seconds, m.end_seconds) not in chosen_keys
    ]
    selected, absorbed, absorbed_seconds = absorb_onsets(
        selected, dropped, min_importance=config.situation_min_onset_importance
    )
    if absorbed:
        onsets_kept = sum(len(item.onsets) for item in absorbed)
        total = sum(moment.context_duration for moment in selected)
        notes.append(
            f"situations: kept {onsets_kept} onset(s) from {len(absorbed)} overlapping "
            f"moment(s) the selection dropped, +{format_duration(absorbed_seconds)}"
        )

    if config.allow_context_trim and total > target_seconds + tolerance:
        # Trimming a few seconds of pre-roll from every clip keeps every moment;
        # dropping one does not. §29's roll is the slack in the system.
        selected, trimmed = _trim_context(
            selected, total - target_seconds, config.max_context_trim_ratio
        )
        total = sum(moment.context_duration for moment in selected)
        if trimmed > 0:
            notes.append(f"trimmed {format_duration(trimmed)} of clip context")

    if config.allow_context_growth and total < target_seconds - tolerance:
        # Every moment is already in and the edit is still short: the only
        # honest seconds left are the ones either side of the clips already
        # chosen. Slack cuts both ways -- the trim above gives context back to
        # absorb an overshoot, and this borrows it to close a shortfall.
        #
        # It is bounded and it is not filler. A moment grows into footage that
        # leads into or out of itself, never into another clip's span and
        # never past the recording. When that is still not enough, the answer
        # is the honest note below: a ten-minute video cannot be made from a
        # seven-minute recording, and padding it with dead air would be a
        # worse answer than a short one.
        selected, grown = _grow_context(
            selected,
            target_seconds - total,
            config.max_context_growth_ratio,
            media_durations or {},
        )
        total = sum(moment.context_duration for moment in selected)
        if grown > 0:
            notes.append(f"extended clip context by {format_duration(grown)}")

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
        metadata={
            "tolerance_seconds": round(tolerance, 2),
            "candidates": len(moments),
            "onsets_kept": onsets_kept,
        },
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
    # P0.6: a moment costs what choosing it commits the edit to -- its own
    # context and the onsets of the overlapping siblings it will drop.
    committed = [
        committed_duration(moment, moments, min_importance=config.situation_min_onset_importance)
        for moment in ordered
    ]

    # (value, indices, type counts) per capacity.
    best: list[tuple[float, tuple[int, ...], dict[MomentType, int]] | None] = [
        (0.0, (), {})
    ] + [None] * capacity

    for index, moment in enumerate(ordered):
        cost = max(round(committed[index] / BUCKET_SECONDS), 1)
        if cost > capacity:
            continue
        # Descending so each moment is used at most once.
        for size in range(capacity, cost - 1, -1):
            previous = best[size - cost]
            if previous is None:
                continue
            prior_value, prior_indices, prior_counts = previous
            # P0.6: two chosen moments never overlap -- a situation is shown
            # once, and the sibling's onsets travel with the one chosen.
            if any(overlaps(moment, ordered[i]) for i in prior_indices):
                continue
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

    def committed(moment: Moment) -> float:
        return committed_duration(
            moment, candidates, min_importance=config.situation_min_onset_importance
        )

    total = sum(committed(moment) for moment in chosen)
    added = 0
    for moment in remaining:
        if total >= target - tolerance:
            break
        if overlaps_any(moment, chosen):
            continue
        chosen.append(moment)
        total += committed(moment)
        added += 1
    return chosen, total, added


def _grow_context(
    moments: Sequence[Moment],
    shortfall: float,
    max_ratio: float,
    media_durations: Mapping[str, float],
) -> tuple[list[Moment], float]:
    """Give every clip more run-up when the edit is short (§29, §39).

    The mirror of :func:`_trim_context`, and bounded the same way. Each clip
    takes an equal share of the shortfall, half before and half after, and
    every share is clipped by three things it must not cross:

    * **the recording** -- footage that does not exist cannot be shown;
    * **the neighbouring clip's span** -- the EDL enforces exclusivity, and a
      clip grown into the next one's footage would be silently trimmed there
      (§40), leaving the plan and the timeline disagreeing;
    * **``max_context_growth_ratio``** -- a moment that doubles in length is
      not a moment with more context, it is a different clip.

    Growth stops when the shortfall is met, so a small gap costs a small
    amount of footage from each clip rather than the maximum from the first.
    """
    from backend.moments.grants import note_widening
    from backend.timeline.authorization import Granter

    if shortfall <= 0 or not moments or max_ratio <= 0:
        return list(moments), 0.0

    ordered = sorted(moments, key=lambda moment: (moment.media_id, moment.context_start))
    share = shortfall / len(ordered)
    grown: list[Moment] = []
    total_gain = 0.0

    for index, moment in enumerate(ordered):
        room = moment.context_duration * max_ratio
        want = min(share, room)

        # The unclaimed footage either side, on this recording only.
        previous = ordered[index - 1] if index else None
        floor = (
            previous.context_end
            if previous is not None and previous.media_id == moment.media_id
            else 0.0
        )
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        ceiling = (
            following.context_start
            if following is not None and following.media_id == moment.media_id
            else media_durations.get(moment.media_id, moment.context_end)
        )

        before = min(want / 2.0, max(moment.context_start - floor, 0.0))
        after = min(want / 2.0, max(ceiling - moment.context_end, 0.0))
        # What one side could not take, the other may -- a clip against the
        # start of a recording still has room after it.
        before = min(before + max(want / 2.0 - after, 0.0), max(moment.context_start - floor, 0.0))
        after = min(after + max(want / 2.0 - before, 0.0), max(ceiling - moment.context_end, 0.0))

        gain = before + after
        if gain <= 0:
            grown.append(moment)
            continue
        total_gain += gain
        # P0.3: a widening is a grant by the duration optimizer (§39), issued
        # later against the exclusions; the mark carries the seconds added.
        grown.append(
            note_widening(
                moment,
                Granter.DURATION_OPTIMIZER,
                start=moment.context_start - before,
                end=moment.context_end + after,
                reason=(
                    f"duration optimizer: +{before:.1f} s before / +{after:.1f} s after, "
                    "towards the target"
                ),
            )
        )

    return grown, round(total_gain, 3)


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


# ---------------------------------------------------------------------------
# sequence repair (S33/S38 at selection time)
# ---------------------------------------------------------------------------

#: A swap must bring at least this share of the victim's value; below it the
#: variety would cost more entertainment than it buys.
_SWAP_VALUE_FLOOR: Final[float] = 0.6
#: Flat-run swaps may go lower: relief is the point, not equivalence.
_FLAT_SWAP_VALUE_FLOOR: Final[float] = 0.4
_REPAIR_ROUNDS: Final[int] = 12


def repair_sequence(
    selected: Sequence[Moment],
    pool: Sequence[Moment],
    *,
    pacing_config,
    config: DurationOptimizerConfig,
    target_seconds: float,
    tolerance: float,
) -> tuple[list[Moment], list[str]]:
    """Fix same-type and low-intensity runs by choosing, never reordering.

    The knapsack picks a *set*; chronology then lines same-type neighbours up
    shoulder to shoulder, and the variety term -- a share of the whole, blind
    to adjacency -- cannot see it. Measured live before this existed: seven
    same-type clips in a row against a limit of two, on a selection whose
    global type mix looked fine.

    Three repairs, in priority order, one change per round so every invariant
    is re-measured before the next decision:

    * a same-type run over ``max_consecutive_same_type`` swaps its weakest
      members for the best differently-typed bench moments, or drops them
      when the duration band allows;
    * a below-threshold stretch longer than
      ``max_consecutive_low_intensity_seconds`` gets the same treatment,
      victims chosen by intensity;
    * a weak final clip is dropped, because a video should end on strength
      (S16) and chronology forbids moving strength to the end.

    Every change keeps the total inside the duration band or strictly closer
    to the target than before; when neither swap nor drop is possible the run
    stays and the pacing report says so, exactly as it does today.
    """
    from backend.narrative.pacing import intensity_of

    chosen = sorted(selected, key=lambda m: (m.media_id, m.context_start))
    key = lambda m: (m.media_id, m.start_seconds)  # noqa: E731
    taken = {key(m) for m in chosen}
    bench = [m for m in pool if key(m) not in taken]
    notes: list[str] = []
    floor_total = target_seconds - tolerance
    ceiling = target_seconds + tolerance

    def total_of(moments: Sequence[Moment]) -> float:
        return sum(m.context_duration for m in moments)

    def fits(new_total: float) -> bool:
        old = abs(total_of(chosen) - target_seconds)
        return floor_total <= new_total <= ceiling or abs(new_total - target_seconds) <= old

    def swap(victim: Moment, candidate: Moment) -> None:
        chosen.remove(victim)
        chosen.append(candidate)
        chosen.sort(key=lambda m: (m.media_id, m.context_start))
        bench.remove(candidate)
        bench.append(victim)

    def best_bench(predicate, value_floor: float, victim: Moment) -> Moment | None:
        victim_value = _base_value(victim, config)
        candidates = [
            item
            for item in bench
            if predicate(item)
            and _base_value(item, config) >= value_floor * victim_value
            and fits(total_of(chosen) - victim.context_duration + item.context_duration)
        ]
        return max(candidates, key=lambda item: _base_value(item, config), default=None)

    for _ in range(_REPAIR_ROUNDS):
        if len(chosen) <= 3:
            break
        intensities = [intensity_of(m) for m in chosen]
        low_bar = sum(intensities) / len(intensities) * 0.75

        # -- same-type runs ------------------------------------------------
        run = _longest_type_run(chosen)
        if len(run) > pacing_config.max_consecutive_same_type:
            victim = min(run, key=lambda m: _base_value(m, config))
            candidate = best_bench(
                lambda item, other=victim.moment_type: item.moment_type is not other,
                _SWAP_VALUE_FLOOR,
                victim,
            )
            if candidate is not None:
                swap(victim, candidate)
                notes.append(
                    f"variety: swapped a {victim.moment_type.value} at "
                    f"{victim.start_seconds:.0f}s for a {candidate.moment_type.value} "
                    f"at {candidate.start_seconds:.0f}s"
                )
                continue
            if total_of(chosen) - victim.context_duration >= floor_total:
                chosen.remove(victim)
                bench.append(victim)
                notes.append(
                    f"variety: dropped a {victim.moment_type.value} at "
                    f"{victim.start_seconds:.0f}s from a run of {len(run)}"
                )
                continue

        # -- low-intensity stretches ----------------------------------------
        stretch = _longest_low_stretch(chosen, intensities, low_bar)
        if (
            stretch
            and sum(m.context_duration for m in stretch)
            > pacing_config.max_consecutive_low_intensity_seconds
        ):
            victim = min(stretch, key=intensity_of)
            candidate = best_bench(
                lambda item, bar=low_bar: intensity_of(item) >= bar,
                _FLAT_SWAP_VALUE_FLOOR,
                victim,
            )
            if candidate is not None:
                swap(victim, candidate)
                notes.append(
                    f"flatness: swapped a quiet {victim.moment_type.value} at "
                    f"{victim.start_seconds:.0f}s for a {candidate.moment_type.value} "
                    f"at {candidate.start_seconds:.0f}s"
                )
                continue
            if total_of(chosen) - victim.context_duration >= floor_total:
                chosen.remove(victim)
                bench.append(victim)
                notes.append(
                    f"flatness: dropped a quiet {victim.moment_type.value} at "
                    f"{victim.start_seconds:.0f}s"
                )
                continue

        # -- end on strength -------------------------------------------------
        last = chosen[-1]
        if (
            intensity_of(last) < low_bar
            and total_of(chosen) - last.context_duration >= floor_total
        ):
            chosen.pop()
            bench.append(last)
            notes.append(
                f"arc: dropped a quiet {last.moment_type.value} ending at "
                f"{last.start_seconds:.0f}s so the edit ends on strength"
            )
            continue

        break

    return chosen, notes


def _longest_type_run(chosen: Sequence[Moment]) -> list[Moment]:
    """The longest consecutive same-type stretch, first among equals."""
    best: list[Moment] = []
    current: list[Moment] = []
    for moment in chosen:
        if current and moment.moment_type is current[-1].moment_type:
            current.append(moment)
        else:
            current = [moment]
        if len(current) > len(best):
            best = list(current)
    return best


def _longest_low_stretch(
    chosen: Sequence[Moment], intensities: Sequence[float], low_bar: float
) -> list[Moment]:
    """The longest consecutive below-bar stretch, by seconds."""
    best: list[Moment] = []
    best_seconds = 0.0
    current: list[Moment] = []
    for moment, intensity in zip(chosen, intensities, strict=True):
        if intensity < low_bar:
            current.append(moment)
            seconds = sum(m.context_duration for m in current)
            if seconds > best_seconds:
                best, best_seconds = list(current), seconds
        else:
            current = []
    return best


__all__ = ["BUCKET_SECONDS", "OptimisationResult", "optimise", "repair_sequence"]
