"""What the evidence would suggest, if there were any (V2-P10).

A proposal is a comparison, written down. For one tunable key it gathers every
measured video, groups them by the value that key had when each was cut -- P8's
stamp keeps the whole resolved body, so that value is recoverable years later
even if the file has changed since -- and asks whether one group held viewers
longer than the other.

That is all it is. In particular it is **not**:

* a significance test. Fifteen videos in two groups is not a sample anyone
  should compute a p-value from, and pretending otherwise would dress a hunch
  in arithmetic. What it produces is "these five did better than those six by
  this much", and a person decides what to make of it.
* a model. Nothing is fitted, nothing is predicted, and nothing extrapolates
  past the values that have actually been tried.
* a licence. A proposal changes nothing on its own; :class:`TuningLedger`
  applies it, and every guard there still has to pass.

The refusals matter more than the proposals here, because for the foreseeable
life of this project every call will be a refusal. They are specific on
purpose: "0 of 15 measured videos" tells the owner exactly how far away this
is, and "every video used the same value" says the comparison is impossible
rather than negative.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from backend.core.logging import LogChannel, get_logger
from backend.database.connection import loads

logger = get_logger("tuning.proposer", LogChannel.APPLICATION)


@dataclass(frozen=True, slots=True)
class Arm:
    """Every measured video that was cut with one value of one key."""

    value: float
    videos: tuple[str, ...]
    scores: tuple[float, ...]

    @property
    def median(self) -> float:
        return statistics.median(self.scores) if self.scores else 0.0

    @property
    def size(self) -> int:
        return len(self.scores)


@dataclass(frozen=True, slots=True)
class Proposal:
    """A suggested step, or the reason there is none."""

    style: str
    key: str
    metric: str
    #: How many measured videos were available at all.
    videos: int
    #: Populated only when a step is being suggested.
    delta: float = 0.0
    base_value: float = 0.0
    better: Arm | None = None
    worse: Arm | None = None
    #: Why there is no proposal. Empty when there is one.
    refusal: str = ""
    notes: tuple[str, ...] = field(default=())

    @property
    def has_step(self) -> bool:
        return not self.refusal and abs(self.delta) > 0.0

    def reason(self) -> str:
        """The sentence stored beside the change, if it is applied."""
        if not self.has_step or self.better is None or self.worse is None:
            return ""
        return (
            f"{self.metric} was {self.better.median:.1f} across "
            f"{self.better.size} video(s) at {self.better.value:g} and "
            f"{self.worse.median:.1f} across {self.worse.size} at "
            f"{self.worse.value:g}; moving {self.key} toward the first."
        )

    def evidence(self) -> dict[str, Any]:
        if self.better is None or self.worse is None:
            return {}
        return {
            "metric": self.metric,
            "comparison": "median, not a significance test",
            "better": {
                "value": self.better.value,
                "median": round(self.better.median, 3),
                "videos": list(self.better.videos),
            },
            "worse": {
                "value": self.worse.value,
                "median": round(self.worse.median, 3),
                "videos": list(self.worse.videos),
            },
        }


def propose(database: Any, config: Any, *, style: str, key: str) -> Proposal:
    """Compare what has been tried, or say why no comparison is possible."""
    tuning = config.style.tuning
    metric = str(tuning.metric)
    measured = _measured(database, style, key, metric)
    videos = len(measured)

    if videos < tuning.minimum_videos:
        return Proposal(
            style=style,
            key=key,
            metric=metric,
            videos=videos,
            refusal=(
                f"{videos} of {tuning.minimum_videos} measured video(s) for "
                f"{style!r}. Nothing can be compared yet."
            ),
        )

    arms = _arms(measured)
    if len(arms) < 2:
        only = next(iter(arms), None)
        return Proposal(
            style=style,
            key=key,
            metric=metric,
            videos=videos,
            refusal=(
                f"Every measured video used {key} = "
                f"{only.value if only else 'the same value'}: there is nothing "
                f"to compare it against. A value has to have been tried both "
                f"ways before it can be judged."
            ),
        )

    usable = [arm for arm in arms if arm.size >= tuning.minimum_per_arm]
    if len(usable) < 2:
        return Proposal(
            style=style,
            key=key,
            metric=metric,
            videos=videos,
            refusal=(
                f"No two values of {key} have {tuning.minimum_per_arm} measured "
                f"video(s) each: "
                + ", ".join(f"{arm.value:g}×{arm.size}" for arm in arms)
                + ". Two arms of one video are two anecdotes."
            ),
        )

    usable.sort(key=lambda arm: -arm.median)
    better, worse = usable[0], usable[-1]
    if better.median <= worse.median:
        return Proposal(
            style=style,
            key=key,
            metric=metric,
            videos=videos,
            refusal=f"No value of {key} did better than another on {metric}.",
        )

    limit = config.style.limits.get(key)
    if limit is None:
        return Proposal(
            style=style,
            key=key,
            metric=metric,
            videos=videos,
            refusal=f"{key} has no declared range, so nothing may move it.",
        )

    base = _base_value(config, style, key)
    span = float(limit.max) - float(limit.min)
    largest = span * float(tuning.max_step_fraction)
    wanted = better.value - base
    if abs(wanted) < 1e-9:
        return Proposal(
            style=style,
            key=key,
            metric=metric,
            videos=videos,
            refusal=(
                f"{key} is already at {base:g}, the value that did best. "
                f"Nothing to change."
            ),
        )

    step = max(-largest, min(largest, wanted))
    notes: list[str] = []
    if abs(step) < abs(wanted) - 1e-9:
        notes.append(
            f"the full move to {better.value:g} is {abs(wanted):g}; this step "
            f"is capped at {largest:g}"
        )
    tuned = base + step
    if not (float(limit.min) <= tuned <= float(limit.max)):
        step = max(float(limit.min) - base, min(float(limit.max) - base, step))
        notes.append("the step was shortened to stay inside the declared range")

    return Proposal(
        style=style,
        key=key,
        metric=metric,
        videos=videos,
        delta=round(step, 6),
        base_value=base,
        better=better,
        worse=worse,
        notes=tuple(notes),
    )


# -- what has actually been measured ----------------------------------------


def _measured(
    database: Any, style: str, key: str, metric: str
) -> list[tuple[str, float, float]]:
    """``(video_id, value_of_key_when_cut, metric)`` for every measured video.

    The value comes from the stamp P8 wrote with the edit, not from the file as
    it reads today: the file may have been edited since, and the question is
    what this video was actually cut with.
    """
    column = _COLUMN_FOR.get(metric)
    if column is None:
        logger.warning("No stored column for this metric", extra={"metric": metric})
        return []
    rows = database.fetch_all(
        f"SELECT o.video_id AS video_id, o.{column} AS score, e.resolved AS resolved "
        "FROM video_outcomes o JOIN edit_styles e ON e.project_id = o.project_id "
        "WHERE e.style = ? AND o." + column + " IS NOT NULL",
        (style,),
    )
    section, _, field_name = key.partition(".")
    found: list[tuple[str, float, float]] = []
    for row in rows:
        body = loads(row["resolved"] or "{}") or {}
        value = (body.get(section) or {}).get(field_name)
        if value is None:
            continue
        found.append((str(row["video_id"]), float(value), float(row["score"])))
    return found


#: Metric name in the API, column in ``video_outcomes``.
_COLUMN_FOR: dict[str, str] = {
    "averageViewPercentage": "average_view_percentage",
    "averageViewDuration": "average_view_duration_seconds",
    "estimatedMinutesWatched": "estimated_minutes_watched",
    "views": "views",
}


def _arms(measured: list[tuple[str, float, float]]) -> list[Arm]:
    grouped: dict[float, list[tuple[str, float]]] = {}
    for video_id, value, score in measured:
        grouped.setdefault(round(value, 6), []).append((video_id, score))
    return [
        Arm(
            value=value,
            videos=tuple(video for video, _ in items),
            scores=tuple(score for _, score in items),
        )
        for value, items in sorted(grouped.items())
    ]


def _base_value(config: Any, style: str, key: str) -> float:
    from backend.config.schema import _style_values

    entry = config.style.bible.get(style)
    return float(_style_values(entry).get(key, 0.0)) if entry else 0.0


def tunable_keys(config: Any) -> tuple[str, ...]:
    """Every key a proposal may be asked about: the ones with a declared range."""
    return tuple(sorted(config.style.limits))


__all__ = ["Arm", "Proposal", "propose", "tunable_keys"]
