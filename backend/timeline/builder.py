"""Narrative plan to timeline (SPEC §40–§42).

The plan says *which* moments and in what order. This says *where* — it lays
them end to end from zero, so the finished video is the concatenation of the
selected spans, and every clip records the source timestamps it came from
rather than a copy of the frames (§42).

The input is a list of :class:`PlannedClip`, not a :class:`NarrativePlan`. That
is deliberate: §81 makes the job result the contract between stages, so the EDL
stage reads what STORY *decided* rather than re-deriving it. Re-running the
optimiser here would usually agree — and the one time it did not, the EDL would
describe a different video from the plan the user was shown, silently.
:func:`clips_from_plan` and :func:`clips_from_story_result` are the two
adapters, and both produce the same thing.

Two things happen here that the narrative phase deliberately did not do.

**The duration clamp.** §39 optimises towards the target and reports when it
cannot reach it; the hard band of §6 is enforced *here*, at the boundary, as a
last resort. It is last-resort because trimming a video to length by cutting the
end off a clip is worse than every alternative the optimiser had, so reaching
this path means something upstream fell short and the log says so.

**Transitions.** A cut is the default and stays the default (§41 lists
transitions; §69 says never apply an effect globally). The only automatic
non-cut is a fade at the very start and end of the programme, which is a
convention rather than an effect: a video that begins mid-frame reads as a
mistake.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.core.duration import DurationPolicy
from backend.core.ids import derived_id
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import MomentType, TrackKind, TransitionType
from backend.narrative.story import NarrativePlan
from backend.timeline.models import Marker, Timeline, TimelineClip, Track

logger = get_logger("timeline.builder", LogChannel.PIPELINE)

#: Programme-level fade, in seconds. Not an effect: the effects planner may add
#: transitions of its own, and this is applied whether it does or not.
OPENING_FADE_SECONDS: float = 0.5

#: A clip shorter than this cannot survive being trimmed by the clamp, so the
#: clamp drops it whole instead of leaving a flash frame.
MIN_CLIP_SECONDS: float = 1.0


@dataclass(slots=True)
class PlannedClip:
    """One selected span, as the narrative stage decided it.

    A moment reduced to what a timeline needs — a recording and two timestamps
    in it — plus the identity and reasoning that let a finished second be traced
    back to the analysis that chose it (§80).
    """

    media_id: str
    source_start: float
    source_end: float
    moment_id: str | None = None
    moment_type: MomentType | None = None
    score: float = 0.0
    role: str = "body"
    beat: str | None = None
    sources: tuple[str, ...] = ()

    @property
    def seconds(self) -> float:
        return self.source_end - self.source_start


@dataclass(frozen=True, slots=True)
class BuildResult:
    """The timeline, plus what had to be done to it to make it legal."""

    timeline: Timeline
    #: Clips dropped or shortened by the §6 clamp. Empty on the normal path.
    clamped: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def was_clamped(self) -> bool:
        return bool(self.clamped)


def clips_from_plan(plan: NarrativePlan) -> list[PlannedClip]:
    """Adapt a :class:`NarrativePlan` — what the STORY stage holds in memory."""
    return [
        PlannedClip(
            media_id=moment.media_id,
            source_start=moment.context_start,
            source_end=moment.context_end,
            moment_id=moment.metadata.get("id"),
            moment_type=moment.moment_type,
            score=moment.score,
            role=str(moment.metadata.get("role", "body")),
            beat=plan.beats[index] if index < len(plan.beats) else None,
            sources=moment.sources,
        )
        for index, moment in enumerate(plan.moments)
    ]


def clips_from_story_result(result: Mapping[str, Any]) -> list[PlannedClip]:
    """Adapt the STORY stage's stored job result — the §81 stage contract.

    This is the path the pipeline uses. The stage that chose these clips wrote
    them down; reading them back is what makes the EDL a description of *that*
    decision rather than a second opinion about it.
    """
    raw = result.get("clips")
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        # A stage that skipped, or an older result shape. Returning nothing
        # lets the caller report "no plan" rather than raising a TypeError
        # three frames down (§95).
        logger.warning(
            "The story result carried no clip list", extra={"type": type(raw).__name__}
        )
        return []
    return [
        PlannedClip(
            media_id=clip["media_id"],
            source_start=float(clip["source_start"]),
            source_end=float(clip["source_end"]),
            moment_id=clip.get("moment_id"),
            moment_type=_moment_type(clip.get("moment_type")),
            score=float(clip.get("score", 0.0)),
            role=str(clip.get("role", "body")),
            beat=clip.get("beat"),
        )
        for clip in raw
        if isinstance(clip, Mapping)
    ]


def build_timeline(
    clips: Sequence[PlannedClip],
    *,
    project_id: str,
    policy: DurationPolicy,
    target_seconds: float = 0.0,
    media_durations: Mapping[str, float] | None = None,
    notes: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> BuildResult:
    """Lay selected clips onto a non-destructive timeline (§40, §42).

    Args:
        clips: what the narrative stage selected, in the order it chose.
        project_id: owner of the timeline.
        policy: the §6 duration band. Enforced here as a last resort.
        target_seconds: what was asked for, carried for later comparison.
        media_durations: source lengths by media id, when known. A clip that
            would read past the end of its recording is trimmed to fit, because
            the alternative is a renderer failing on a seek nobody can explain.
        notes: anything the narrative stage wants carried forward.
        metadata: mode, hook summary and similar, stored on the timeline.

    Returns:
        The timeline and an account of any clamping, so the caller can report
        it rather than discover it in the finished file.
    """
    if not clips:
        return BuildResult(
            timeline=Timeline(
                project_id=project_id,
                target_seconds=target_seconds,
                notes=("no clips were selected to build a timeline from",),
            ),
            notes=("no clips were selected to build a timeline from",),
        )

    bounded = _bounded(clips, media_durations or {})
    bounded, clamped, clamp_notes = _apply_duration_band(bounded, policy)
    if not bounded:
        return BuildResult(
            timeline=Timeline(
                project_id=project_id,
                target_seconds=target_seconds,
                notes=tuple(clamp_notes),
            ),
            clamped=tuple(clamped),
            notes=tuple(clamp_notes),
        )

    laid_out = _lay_out(bounded, project_id)
    video = Track(kind=TrackKind.VIDEO, name="gameplay", clips=tuple(laid_out))
    timeline = Timeline(
        project_id=project_id,
        target_seconds=target_seconds,
        notes=(*notes, *clamp_notes),
        metadata=dict(metadata or {}),
    ).with_track(video)
    timeline = timeline.with_markers(_markers(laid_out))

    warnings: list[str] = []
    if clamped:
        # Loud, because reaching this path means the optimiser did not solve the
        # problem it was given and someone should know which video is affected.
        logger.warning(
            "The §6 duration band was enforced at the EDL boundary",
            extra={
                "project_id": project_id,
                "clips_affected": len(clamped),
                "duration_seconds": round(timeline.duration, 2),
                "band": [policy.min_seconds, policy.max_seconds],
            },
        )
        warnings.append(
            f"{len(clamped)} clip(s) were shortened or dropped to fit the "
            f"{policy.min_seconds // 60}-{policy.max_seconds // 60} minute band"
        )
    if timeline.duration < policy.min_seconds:
        # Not clampable: no amount of trimming makes a video longer. Reported
        # rather than padded, because padding means inventing footage.
        warnings.append(
            f"the edit is {timeline.duration / 60:.1f} minutes, below the "
            f"{policy.min_seconds // 60}-minute product minimum"
        )

    logger.info("Built the timeline", extra={"project_id": project_id, **timeline.summary()})
    return BuildResult(
        timeline=timeline,
        clamped=tuple(clamped),
        notes=tuple(clamp_notes),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _bounded(
    clips: Sequence[PlannedClip], durations: Mapping[str, float]
) -> list[PlannedClip]:
    """Each span, bounded by the recording it came from.

    §29 expanded the context, and expansion near the head or tail of a file can
    run past it. Bounding here rather than at render time means the EDL is
    already seekable, which is what makes it a *description* of the video.
    """
    bounded: list[PlannedClip] = []
    for clip in clips:
        start = max(0.0, clip.source_start)
        end = clip.source_end
        limit = durations.get(clip.media_id)
        if limit is not None:
            end = min(end, limit)
        if end - start <= 0:
            logger.warning(
                "Dropped a clip whose span lies outside its recording",
                extra={
                    "media_id": clip.media_id,
                    "span": [clip.source_start, clip.source_end],
                    "media_duration": limit,
                },
            )
            continue
        bounded.append(
            PlannedClip(
                media_id=clip.media_id,
                source_start=start,
                source_end=end,
                moment_id=clip.moment_id,
                moment_type=clip.moment_type,
                score=clip.score,
                role=clip.role,
                beat=clip.beat,
                sources=clip.sources,
            )
        )
    return bounded


def _apply_duration_band(
    clips: list[PlannedClip], policy: DurationPolicy
) -> tuple[list[PlannedClip], list[str], list[str]]:
    """Enforce §6's hard maximum. The last resort, and only ever downward.

    Trimming happens at the *end* of the programme and from the *end* of a clip,
    because the alternative -- dropping the weakest clip wherever it sits --
    would silently undo the optimiser's variety work and leave a video with a
    hole in the middle of its arc.
    """
    total = sum(clip.seconds for clip in clips)
    if total <= policy.max_seconds:
        return clips, [], []

    excess = total - policy.max_seconds
    dropped: set[int] = set()
    clamped: list[str] = []
    for index in range(len(clips) - 1, -1, -1):
        if excess <= 0:
            break
        clip = clips[index]
        removable = clip.seconds - MIN_CLIP_SECONDS
        if removable <= 0:
            # Too short to trim without leaving a flash frame, so it goes whole.
            excess -= clip.seconds
            dropped.add(index)
        else:
            taken = min(excess, removable)
            clip.source_end -= taken
            excess -= taken
        clamped.append(_clip_key(clip))

    survivors = [clip for index, clip in enumerate(clips) if index not in dropped]
    removed = total - sum(clip.seconds for clip in survivors)
    notes = [
        f"trimmed {removed:.1f}s to stay inside the {policy.max_seconds}s "
        f"product maximum (§6)"
    ]
    return survivors, clamped, notes


def _lay_out(clips: Sequence[PlannedClip], project_id: str) -> list[TimelineClip]:
    """Place the spans end to end from zero.

    Contiguity is the point: a gap on the video track is black frames nobody
    asked for, so the next clip starts exactly where the previous one ended.

    Clip ids are derived rather than generated, so re-running this stage on the
    same plan produces the same ids and every stored reference to them survives
    (§48, §78).
    """
    laid_out: list[TimelineClip] = []
    cursor = 0.0
    last = len(clips) - 1
    for index, clip in enumerate(clips):
        edge = index in (0, last)
        laid_out.append(
            TimelineClip(
                id=derived_id(
                    "timeline_clip", project_id, clip.media_id, index, round(clip.source_start, 3)
                ),
                media_id=clip.media_id,
                moment_id=clip.moment_id,
                track=TrackKind.VIDEO,
                clip_index=index,
                source_in=round(clip.source_start, 3),
                source_out=round(clip.source_end, 3),
                timeline_start=round(cursor, 3),
                timeline_end=round(cursor + clip.seconds, 3),
                transition_in=TransitionType.FADE if index == 0 else TransitionType.CUT,
                transition_out=TransitionType.FADE if index == last else TransitionType.CUT,
                moment_type=clip.moment_type,
                score=round(clip.score, 4),
                role=clip.role,
                metadata={
                    "beat": clip.beat,
                    "sources": list(clip.sources),
                    # Carried rather than left for the renderer to guess: the
                    # transition names the shape, this names its length.
                    **({"fade_seconds": OPENING_FADE_SECONDS} if edge else {}),
                },
            )
        )
        cursor += clip.seconds
    return laid_out


def _markers(clips: Sequence[TimelineClip]) -> list[Marker]:
    """Leave the structural decisions where a human can see them (§80).

    The hook and the climax are the two choices a viewer would notice being
    wrong, so they are the two the timeline points at.
    """
    markers: list[Marker] = []
    for clip in clips:
        if clip.role == "hook":
            markers.append(
                Marker(
                    id=derived_id("marker", clip.id, "hook"),
                    timeline_seconds=clip.timeline_start,
                    label="hook",
                    kind="structure",
                    metadata={"moment_type": _type_name(clip.moment_type)},
                )
            )
        if clip.metadata.get("beat") == "climax":
            markers.append(
                Marker(
                    id=derived_id("marker", clip.id, "climax"),
                    timeline_seconds=clip.timeline_start,
                    label="climax",
                    kind="structure",
                    metadata={"moment_type": _type_name(clip.moment_type)},
                )
            )
    return markers


def _moment_type(value: object) -> MomentType | None:
    if value is None:
        return None
    try:
        return MomentType(value)
    except ValueError:
        # A stored value the enum no longer knows. The clip is still a clip;
        # losing its type costs an effect trigger, not the edit.
        logger.warning("Unknown moment type on a planned clip", extra={"value": str(value)})
        return None


def _type_name(moment_type: MomentType | None) -> str | None:
    return moment_type.value if moment_type is not None else None


def _clip_key(clip: PlannedClip) -> str:
    return str(clip.moment_id or f"{clip.media_id}@{clip.source_start:.3f}")


__all__ = [
    "MIN_CLIP_SECONDS",
    "OPENING_FADE_SECONDS",
    "BuildResult",
    "PlannedClip",
    "build_timeline",
    "clips_from_plan",
    "clips_from_story_result",
]
