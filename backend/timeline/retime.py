"""Re-laying the timeline for time-warping effects (doctrine §11, §12).

A freeze_frame or a speed_ramp changes how long a clip *takes*, and the EDL is
the only honest place to say so. The planner stored both effect types from
Phase 8 on and the renderer refused to realise them, for a stated reason: a
warp baked into a segment without the timeline knowing makes every duration in
the system a lie -- §47's segment reuse compares against ``clip.duration``,
§76's QA gate compares the file against the edit, and the audio track is cut
to the spans the timeline declares. So the warp is written into the timeline
first, here, and everything downstream reads it back.

The division of truth is deliberate and worth stating once:

* **The clip's spans are the truth about duration.** A retimed clip's
  ``timeline_end - timeline_start`` exceeds its source span by exactly the
  warp's added seconds, and every consumer -- renderer, audio graph, QA --
  derives the added time from the spans, never from the metadata.
* **The metadata is the truth about shape.** ``metadata["retime"]`` records
  which effect, where in the clip it anchors, and (for a ramp) how deep the
  slowdown is. Shape without the spans cannot change a duration; spans without
  the shape degrade to a freeze at the clip's end rather than to a wrong-length
  segment -- because a §127 edit (a split, a trim) rewrites spans without
  understanding warps, and the renderer must still emit a segment of exactly
  the length the timeline promises.

Placed in ``backend.timeline`` rather than the renderer because the re-lay is
timeline arithmetic: the renderer and the audio builders import the *reading*
half (:func:`clip_retime`, :func:`output_offset`), and the EDL worker calls the
*writing* half (:func:`relay_timeline`) once, after effects are planned and
before captions are timed -- captions map onto clip positions, and timing them
against the pre-relay layout would leave every caption after a frozen clip
late by the length of the hold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import EffectType, TrackKind
from backend.effects.models import EffectInstance
from backend.timeline.models import Marker, Timeline, TimelineClip, Track

logger = get_logger("timeline.retime", LogChannel.PIPELINE)

#: The effects this module knows how to lay out. ``slow_motion`` is absent on
#: purpose: it warps a whole clip rather than a window inside one, which is a
#: ``speed`` change the clip model already expresses -- wiring it is a planner
#: decision, not a re-lay mechanism, and it stays unrealised for now.
TIME_WARP_EFFECTS: Final[frozenset[EffectType]] = frozenset(
    {EffectType.FREEZE_FRAME, EffectType.SPEED_RAMP}
)

#: The clip metadata key the re-lay writes and every reader consults.
RETIME_KEY: Final[str] = "retime"

#: Below this, an added duration is rounding, not a warp: a 60 fps frame is
#: 16.7 ms, and nothing shorter than half a frame can be held or slowed.
MIN_EXTRA_SECONDS: Final[float] = 0.01

#: A slow window shorter than this is a stutter, not a ramp.
MIN_WINDOW_SECONDS: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class ClipRetime:
    """One clip's warp, in clip-relative **source-clock** seconds.

    ``at_seconds`` is where the warp anchors inside the clip's source span:
    the held instant for a freeze, the start of the slow window for a ramp.
    ``extra_seconds`` is how many timeline seconds the warp adds -- the number
    the spans already carry, repeated here so a reader holding only the shape
    can do arithmetic without the clip.
    """

    effect: EffectType
    at_seconds: float
    extra_seconds: float
    #: Ramp only: how much source the slow phase covers, and how slow it runs.
    window_seconds: float = 0.0
    factor: float = 1.0

    def as_metadata(self) -> dict[str, float | str]:
        data: dict[str, float | str] = {
            "effect": self.effect.value,
            "at": round(self.at_seconds, 3),
            "extra_seconds": round(self.extra_seconds, 3),
        }
        if self.effect is EffectType.SPEED_RAMP:
            data["window_seconds"] = round(self.window_seconds, 3)
            data["factor"] = round(self.factor, 4)
        return data


@dataclass(frozen=True, slots=True)
class RelayResult:
    """The re-laid timeline, the effects translated onto it, and what changed."""

    timeline: Timeline
    effects: list[EffectInstance]
    notes: tuple[str, ...] = ()

    @property
    def retimed_clips(self) -> int:
        return sum(1 for clip in self.timeline.clips if RETIME_KEY in clip.metadata)


def is_retimed(clip: TimelineClip) -> bool:
    """Whether a clip's timeline seconds are not its source seconds.

    True for a ``speed`` warp as well as a windowed one, because every caller
    asking this question -- J/L planning above all -- cares about the mapping
    being non-linear, not about which mechanism bent it.
    """
    return clip.speed != 1.0 or clip_retime(clip) is not None


def clip_retime(clip: TimelineClip) -> ClipRetime | None:
    """Read a clip's warp back, reconciled against the spans.

    The spans win every disagreement. A §127 edit can split or trim a retimed
    clip without understanding the warp -- ``save_edit`` copies metadata
    wholesale -- so the stored shape can describe a clip that no longer exists.
    Reconciling here means the renderer always produces a segment of exactly
    ``clip.duration``: an anchor past the new source span becomes a hold on the
    last frame, a ramp window that no longer fits is re-derived from the extra
    seconds the spans still carry.
    """
    raw = clip.metadata.get(RETIME_KEY)
    if not isinstance(raw, Mapping):
        return None
    extra = clip.duration - clip.source_duration / clip.speed
    if extra <= MIN_EXTRA_SECONDS:
        return None
    try:
        effect = EffectType(str(raw.get("effect")))
    except ValueError:
        effect = EffectType.FREEZE_FRAME
    source = clip.source_duration
    at = _numeric(raw.get("at"), source)
    at = min(max(at, 0.0), source)

    if effect is EffectType.SPEED_RAMP:
        factor = _numeric(raw.get("factor"), 0.0)
        if 0.0 < factor < 1.0:
            window = extra * factor / (1.0 - factor)
            if at + window > source:
                # The trim moved the out-point into the slow phase. Keep the
                # extra (the spans demand it) and deepen the slowdown to fit.
                window = max(source - at, 0.0)
                factor = window / (window + extra) if window > 0 else 0.0
            if window >= MIN_WINDOW_SECONDS and 0.0 < factor < 1.0:
                return ClipRetime(
                    effect=EffectType.SPEED_RAMP,
                    at_seconds=at,
                    extra_seconds=extra,
                    window_seconds=window,
                    factor=factor,
                )
        # A ramp whose shape cannot be honoured degrades to a freeze rather
        # than to a wrong-length segment: the timeline's duration is a promise
        # the picture must keep even when the decoration cannot be.
        logger.warning(
            "A speed_ramp's stored shape no longer fits its clip; holding the frame instead",
            extra={"clip_id": clip.id, "at": at, "extra": round(extra, 3)},
        )
        return ClipRetime(effect=EffectType.FREEZE_FRAME, at_seconds=at, extra_seconds=extra)

    return ClipRetime(effect=EffectType.FREEZE_FRAME, at_seconds=at, extra_seconds=extra)


def output_offset(clip: TimelineClip, source_offset: float) -> float:
    """Clip-relative source seconds to clip-relative output seconds.

    The piecewise mapping a warp creates: before the anchor nothing moved,
    inside a slow window time stretches by ``1/factor``, after the warp
    everything is late by ``extra_seconds``. Captions, overlay effects and
    stingers all anchor to source-clock positions inside their clip, and each
    was measurably wrong without this -- a caption after a 1.5 s freeze
    appeared 1.5 s before its words were heard.

    For an unwarped clip this is the ``offset / speed`` mapping the captions
    module has always used, so plain clips cost one dictionary miss.
    """
    warp = clip_retime(clip)
    if warp is None:
        return source_offset / clip.speed
    offset = min(max(source_offset, 0.0), clip.source_duration)
    if warp.effect is EffectType.SPEED_RAMP:
        start, end = warp.at_seconds, warp.at_seconds + warp.window_seconds
        if offset <= start:
            return offset
        if offset >= end:
            return offset + warp.extra_seconds
        return start + (offset - start) / warp.factor
    # Freeze: the held frame is the last one before the anchor, so the anchor
    # instant itself is seen *after* the hold.
    if offset < warp.at_seconds:
        return offset
    return offset + warp.extra_seconds


def relay_timeline(
    timeline: Timeline,
    effects: Sequence[EffectInstance],
    *,
    max_duration_seconds: float | None = None,
) -> RelayResult:
    """Give every clip carrying a time-warp the timeline seconds it needs.

    Runs once in the EDL stage, after the planner (which needs the built
    positions to place effects at all) and before captions (which need the
    final positions to be timed against). Clips after a warped one shift by
    the accumulated extra seconds; markers move with the clip that carries
    them; the effect instances come back translated onto the new layout so
    the repository's clip-relative storage stays exact.

    ``max_duration_seconds`` is §6's ceiling: a timeline the builder clamped
    to the maximum must not be pushed back over it by decoration, so a warp
    that would cross the ceiling is skipped with a note rather than applied.
    """
    video = timeline.track(TrackKind.VIDEO)
    if video is None or not video.clips:
        return RelayResult(timeline=timeline, effects=list(effects))

    warps, notes = _warps_by_clip(video.in_order(), effects)
    if not warps:
        return RelayResult(timeline=timeline, effects=list(effects), notes=tuple(notes))

    duration = timeline.duration
    laid_out: list[TimelineClip] = []
    old_starts: dict[str, float] = {}
    deltas: dict[str, float] = {}
    cursor = 0.0
    for clip in video.in_order():
        old_starts[clip.id] = clip.timeline_start
        warp = warps.get(clip.id)
        if (
            warp is not None
            and max_duration_seconds is not None
            and duration + warp.extra_seconds > max_duration_seconds + 1e-6
        ):
            notes.append(
                f"skipped a {warp.effect.value} on clip {clip.clip_index}: it would "
                f"push the edit past the {max_duration_seconds:.0f}s maximum (§6)"
            )
            warp = None
        if warp is not None:
            duration += warp.extra_seconds

        length = clip.duration + (warp.extra_seconds if warp is not None else 0.0)
        update: dict[str, object] = {
            "timeline_start": round(cursor, 6),
            "timeline_end": round(cursor + length, 6),
        }
        if warp is not None:
            update["metadata"] = {**clip.metadata, RETIME_KEY: warp.as_metadata()}
        placed = clip.model_copy(update=update)
        deltas[clip.id] = placed.timeline_start - clip.timeline_start
        if clip.enabled:
            cursor += length
        laid_out.append(placed)

    relaid = timeline.with_track(
        Track(kind=TrackKind.VIDEO, name=video.name, clips=tuple(laid_out))
    )
    relaid = relaid.with_markers(_moved_markers(timeline.markers, laid_out, old_starts))
    if notes:
        relaid = relaid.model_copy(update={"notes": (*relaid.notes, *notes)})

    translated = [
        effect.model_copy(
            update={
                "start_seconds": round(
                    effect.start_seconds + deltas.get(effect.clip_id or "", 0.0), 6
                )
            }
        )
        for effect in effects
    ]
    logger.info(
        "Re-laid the timeline for time-warping effects",
        extra={
            "project_id": timeline.project_id,
            "retimed_clips": len(warps),
            "added_seconds": round(relaid.duration - timeline.duration, 3),
            "notes": notes,
        },
    )
    return RelayResult(timeline=relaid, effects=translated, notes=tuple(notes))


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _warps_by_clip(
    clips: Sequence[TimelineClip], effects: Sequence[EffectInstance]
) -> tuple[dict[str, ClipRetime], list[str]]:
    """Each clip's warp, derived from the planned instances.

    Effect times arrive in timeline coordinates -- the planner's frame -- and
    on the pre-relay layout those equal clip-relative source seconds plus the
    clip's start, which is what makes the subtraction below exact. At most one
    warp per clip: the planner's one-per-category rule guarantees it, and the
    guarantee is enforced rather than assumed because a second warp on one
    clip would compose in an order nobody chose.
    """
    by_id = {clip.id: clip for clip in clips}
    warps: dict[str, ClipRetime] = {}
    notes: list[str] = []
    for effect in effects:
        if effect.effect not in TIME_WARP_EFFECTS:
            continue
        clip = by_id.get(effect.clip_id or "")
        if clip is None:
            notes.append(
                f"a planned {effect.effect.value} names no clip on the timeline and was skipped"
            )
            continue
        if clip.id in warps or RETIME_KEY in clip.metadata:
            notes.append(
                f"clip {clip.clip_index} already carries a time warp; "
                f"the extra {effect.effect.value} was skipped"
            )
            continue
        warp = _warp_from_instance(effect, clip)
        if warp is None:
            notes.append(
                f"a {effect.effect.value} on clip {clip.clip_index} was too small to realise"
            )
            continue
        warps[clip.id] = warp
    return warps, notes


def _warp_from_instance(effect: EffectInstance, clip: TimelineClip) -> ClipRetime | None:
    """One planned instance as a warp, clamped to the clip that carries it."""
    source = clip.source_duration
    at = min(max(effect.start_seconds - clip.timeline_start, 0.0), source)

    if effect.effect is EffectType.FREEZE_FRAME:
        hold = effect.duration_seconds
        ceiling = _numeric(effect.params.get("max_duration_seconds"), hold)
        hold = round(min(hold, max(ceiling, 0.0)), 3)
        if hold < MIN_EXTRA_SECONDS:
            return None
        return ClipRetime(
            effect=EffectType.FREEZE_FRAME, at_seconds=round(at, 3), extra_seconds=hold
        )

    factor = _numeric(effect.params.get("slow_factor"), 0.0)
    if not 0.0 < factor < 1.0:
        return None
    window = round(min(effect.duration_seconds, source - at), 3)
    if window < MIN_WINDOW_SECONDS:
        return None
    extra = round(window * (1.0 / factor - 1.0), 3)
    if extra < MIN_EXTRA_SECONDS:
        return None
    return ClipRetime(
        effect=EffectType.SPEED_RAMP,
        at_seconds=round(at, 3),
        extra_seconds=extra,
        window_seconds=window,
        factor=factor,
    )


def _moved_markers(
    markers: Sequence[Marker],
    laid_out: Sequence[TimelineClip],
    old_starts: Mapping[str, float],
) -> list[Marker]:
    """Markers, moved with the footage they point at.

    A marker names an instant of the *old* layout. The clip containing that
    instant is found by its old position, and the instant is re-expressed
    through the clip's new position and its warp -- so a climax marker on a
    frozen clip still points at the climax, not at a spot the hold pushed
    everything past.
    """
    moved: list[Marker] = []
    for marker in markers:
        seconds = marker.timeline_seconds
        shifted = seconds
        for clip in laid_out:
            old_start = old_starts.get(clip.id, clip.timeline_start)
            old_end = old_start + clip.source_duration / clip.speed
            if old_start - 1e-6 <= seconds <= old_end + 1e-6:
                shifted = clip.timeline_start + output_offset(clip, seconds - old_start)
                break
        moved.append(
            marker.model_copy(update={"timeline_seconds": round(shifted, 6)})
            if shifted != seconds
            else marker
        )
    return moved


def _numeric(value: object, fallback: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return fallback


__all__ = [
    "MIN_EXTRA_SECONDS",
    "RETIME_KEY",
    "TIME_WARP_EFFECTS",
    "ClipRetime",
    "RelayResult",
    "clip_retime",
    "is_retimed",
    "output_offset",
    "relay_timeline",
]
