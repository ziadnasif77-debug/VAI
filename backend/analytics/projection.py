"""Where the audience left, and what was on screen when they did (V2-P9).

A retention curve is a fraction of a video against the share of viewers still
watching. On its own it says nothing about editing: it has no clips in it, no
levels, no styles. This module puts it beside the edit that produced it, so a
dip at 38% of the video becomes "the third shot of the inventory sequence, cut
at 2.1s in a calm band, under the patient style".

What this is:

    **Outcome correlation.** Two records placed side by side, with the join
    made explicit and checkable.

What it is deliberately not:

    A prediction. Nothing here says a future edit will retain better, and
    nothing adjusts a decision. One video's curve is one video's curve; the
    phase permitted to change anything is P10, inside the bounds P8 declared,
    and only once there is enough data to argue with. Until then this is a
    report a person reads.

The honest failure mode is answering anyway. A ratio without the video's length
is not a time, and a curve read against a timeline that has been re-edited
since the render describes shots that were never on screen -- both refuse here
rather than produce a plausible-looking table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from backend.core.logging import LogChannel, get_logger

logger = get_logger("analytics.projection", LogChannel.QA)

#: A drop of at least this much of the audience between two samples is called a
#: dip. Not tuned against anything -- there is no data to tune against, and a
#: threshold invented from outcome data would be the learning this phase is
#: not allowed to claim. It is a reporting threshold, and it says so.
DIP_DROP: float = 0.05


@dataclass(frozen=True, slots=True)
class Reading:
    """One sample of the curve, and the shot that was on screen for it."""

    elapsed_ratio: float
    at_seconds: float
    audience_watch_ratio: float
    clip_id: str | None = None
    clip_index: int | None = None
    role: str = ""
    level: str = ""
    #: How far into that shot the sample fell, in seconds.
    into_clip_seconds: float = 0.0
    effects: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.clip_id is not None


@dataclass(frozen=True, slots=True)
class Dip:
    """A place where the audience left faster than elsewhere."""

    from_ratio: float
    to_ratio: float
    at_seconds: float
    drop: float
    reading: Reading

    def describe(self) -> str:
        where = (
            f"shot {self.reading.clip_index}"
            if self.reading.clip_index is not None
            else "no shot on record"
        )
        return (
            f"{self.drop:.0%} of the audience left between {self.from_ratio:.0%} "
            f"and {self.to_ratio:.0%} ({self.at_seconds:.0f}s), during {where}"
            + (f" [{self.reading.level}]" if self.reading.level else "")
        )


@dataclass(frozen=True, slots=True)
class Projection:
    """The curve and the edit, side by side."""

    project_id: str
    video_id: str
    duration_seconds: float
    readings: tuple[Reading, ...] = ()
    dips: tuple[Dip, ...] = ()
    style: str = ""
    style_version: int | None = None
    #: Why a reading could not be placed, when some could not be.
    notes: tuple[str, ...] = field(default=())

    @property
    def matched_fraction(self) -> float:
        if not self.readings:
            return 0.0
        return sum(1 for r in self.readings if r.matched) / len(self.readings)


def project(
    database: Any,
    outcome: Any,
    *,
    config: Any = None,
) -> Projection | None:
    """Place one outcome's curve against the edit that produced it.

    Returns ``None`` -- with the reason logged -- when the join cannot be made
    honestly: no curve, no rendered length to turn ratios into seconds, or a
    timeline whose length no longer matches the video that was measured.
    """
    if not getattr(outcome, "points", ()):
        logger.info(
            "No curve to project", extra={"video_id": getattr(outcome, "video_id", "")}
        )
        return None

    duration = _rendered_seconds(database, outcome.project_id)
    if not duration:
        logger.info(
            "No rendered length, so a ratio cannot become a time",
            extra={"project_id": outcome.project_id},
        )
        return None

    clips = _clips(database, outcome.project_id)
    notes: list[str] = []
    if clips:
        edit_seconds = max(clip["timeline_end"] for clip in clips)
        if abs(edit_seconds - duration) > 1.0:
            # The edit changed after the render the audience watched. Reading
            # the curve against it would name shots that were never on screen.
            logger.warning(
                "The stored edit no longer matches the video that was measured",
                extra={
                    "project_id": outcome.project_id,
                    "edit_seconds": round(edit_seconds, 2),
                    "video_seconds": round(duration, 2),
                },
            )
            return None
    else:
        notes.append("no stored timeline; the curve is reported without shots")

    effects = _effects_by_clip(database, outcome.project_id)
    levels = _levels(database, outcome.project_id, config)
    readings = tuple(
        _reading(point, duration, clips, effects, levels) for point in outcome.points
    )
    style, version = _style_of(database, outcome.project_id)
    return Projection(
        project_id=outcome.project_id,
        video_id=outcome.video_id,
        duration_seconds=duration,
        readings=readings,
        dips=_dips(readings),
        style=style,
        style_version=version,
        notes=tuple(notes),
    )


def _reading(point: Any, duration: float, clips, effects, levels) -> Reading:
    at = max(0.0, min(duration, float(point.elapsed_ratio) * duration))
    clip = _clip_at(clips, at)
    if clip is None:
        return Reading(
            elapsed_ratio=float(point.elapsed_ratio),
            at_seconds=at,
            audience_watch_ratio=float(point.audience_watch_ratio),
        )
    return Reading(
        elapsed_ratio=float(point.elapsed_ratio),
        at_seconds=at,
        audience_watch_ratio=float(point.audience_watch_ratio),
        clip_id=clip["id"],
        clip_index=int(clip["clip_index"]),
        role=_role_of(clip),
        level=levels.get(int(clip["clip_index"]), ""),
        into_clip_seconds=round(at - float(clip["timeline_start"]), 2),
        effects=tuple(effects.get(clip["id"], ())),
    )


def _dips(readings: Sequence[Reading]) -> tuple[Dip, ...]:
    """Steepest first. A dip is a measurement, not a verdict on the shot."""
    found: list[Dip] = []
    for before, after in pairwise(readings):
        drop = before.audience_watch_ratio - after.audience_watch_ratio
        if drop >= DIP_DROP:
            found.append(
                Dip(
                    from_ratio=before.elapsed_ratio,
                    to_ratio=after.elapsed_ratio,
                    at_seconds=after.at_seconds,
                    drop=drop,
                    reading=after,
                )
            )
    found.sort(key=lambda dip: -dip.drop)
    return tuple(found)


# -- what the edit says -----------------------------------------------------


def _rendered_seconds(database: Any, project_id: str) -> float:
    row = database.fetch_one(
        "SELECT duration_seconds FROM renders WHERE project_id = ? "
        "AND duration_seconds IS NOT NULL ORDER BY completed_at DESC LIMIT 1",
        (project_id,),
    )
    return float(row["duration_seconds"]) if row and row["duration_seconds"] else 0.0


def _clips(database: Any, project_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in database.fetch_all(
            "SELECT id, clip_index, timeline_start, timeline_end, metadata "
            "FROM timeline_clips WHERE project_id = ? AND track = 'video' "
            "AND enabled = 1 ORDER BY clip_index",
            (project_id,),
        )
    ]


def _role_of(clip: dict[str, Any]) -> str:
    """A clip's role travels in its metadata, beside its score and type."""
    from backend.database.connection import loads

    try:
        return str((loads(clip.get("metadata") or "{}") or {}).get("role") or "")
    except Exception:
        return ""


def _clip_at(clips: Sequence[dict[str, Any]], at: float) -> dict[str, Any] | None:
    for clip in clips:
        if float(clip["timeline_start"]) <= at < float(clip["timeline_end"]):
            return clip
    return clips[-1] if clips and at >= float(clips[-1]["timeline_end"]) else None


def _effects_by_clip(database: Any, project_id: str) -> dict[str, list[str]]:
    by_clip: dict[str, list[str]] = {}
    for row in database.fetch_all(
        "SELECT clip_id, effect_type FROM timeline_effects "
        "WHERE project_id = ? AND enabled = 1 AND clip_id IS NOT NULL",
        (project_id,),
    ):
        by_clip.setdefault(row["clip_id"], []).append(str(row["effect_type"]))
    return by_clip


def _levels(database: Any, project_id: str, config: Any) -> dict[int, str]:
    """The semantic level each shot was cut at, if the lanes are still stored."""
    if config is None:
        return {}
    try:
        from backend.database.repositories.timeline import TimelineRepository
        from backend.semantic.levels import clip_levels

        timeline = TimelineRepository(database).load(project_id)
        return dict(clip_levels(database, timeline, config=config))
    except Exception:
        logger.info(
            "The session's lanes are not available; the curve is reported "
            "without levels",
            extra={"project_id": project_id},
        )
        return {}


def _style_of(database: Any, project_id: str) -> tuple[str, int | None]:
    row = database.fetch_one(
        "SELECT style, version FROM edit_styles WHERE project_id = ?", (project_id,)
    )
    return (str(row["style"]), int(row["version"])) if row else ("", None)


__all__ = ["DIP_DROP", "Dip", "Projection", "Reading", "project"]
