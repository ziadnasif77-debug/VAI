"""Timeline operations: split, trim, move, delete, restore (SPEC §42, §78).

Every operation returns a **new** timeline. Nothing here mutates the one it was
given, which is what makes undo possible without a transaction log: the caller
keeps the previous object, or the interaction layer keeps a snapshot of it.

Two rules run through all of them.

**Deleting does not delete.** ``delete`` disables a clip; the clip stays in the
EDL carrying its source references, so "put that back" is a flag flip rather
than a re-derivation nobody can guarantee is identical (§78). ``remove``
exists for the caller that genuinely wants the row gone, and is named
differently on purpose.

**Positions re-flow, sources never move.** After any change the video track is
laid out end to end from zero again, because a gap renders as black frames. The
``source_in``/``source_out`` of an untouched clip is exactly what it was, which
is what §42 means by non-destructive: the operation changed where you see it,
not which frames it is.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from backend.core.errors import ErrorCode, ValidationError
from backend.core.ids import derived_id
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import TrackKind, TransitionType
from backend.timeline.authorization import AuthorizationError, Granter, issue
from backend.timeline.models import EPSILON, Timeline, TimelineClip, Track

logger = get_logger("timeline.operations", LogChannel.PIPELINE)

#: The shortest clip an operation may produce. Below about a third of a second
#: a cut reads as a glitch rather than an edit.
MIN_RESULT_SECONDS: float = 0.3


def split(timeline: Timeline, clip_id: str, at_seconds: float) -> Timeline:
    """Cut a clip in two at a timeline position (§42).

    Both halves keep reading the same recording — the first from the original
    ``source_in`` to the split point, the second from there to the original
    ``source_out``. No frames move, which is why the split is reversible by
    deleting one half.

    Args:
        at_seconds: a position on the *timeline*, not in the source. That is the
            position a user can see, and translating it is this function's job.
    """
    clip = _require_clip(timeline, clip_id)
    if not clip.contains(at_seconds):
        raise _invalid(
            f"{at_seconds:.3f}s is outside clip {clip_id} "
            f"({clip.timeline_start:.3f}-{clip.timeline_end:.3f}s)"
        )

    left_duration = at_seconds - clip.timeline_start
    right_duration = clip.timeline_end - at_seconds
    if min(left_duration, right_duration) < MIN_RESULT_SECONDS:
        raise _invalid(
            f"splitting at {at_seconds:.3f}s would leave a "
            f"{min(left_duration, right_duration):.3f}s fragment; the minimum is "
            f"{MIN_RESULT_SECONDS}s"
        )

    boundary = clip.source_at(at_seconds)
    left = clip.model_copy(
        update={
            "source_out": boundary,
            "timeline_end": at_seconds,
            "transition_out": TransitionType.CUT,
        }
    )
    right = clip.model_copy(
        update={
            "id": derived_id("timeline_clip", clip.id, "split", round(at_seconds, 3)),
            "source_in": boundary,
            "timeline_start": at_seconds,
            "transition_in": TransitionType.CUT,
        }
    )
    return _rewrite(timeline, clip.track, _replace(timeline, clip, [left, right]))


def trim(
    timeline: Timeline,
    clip_id: str,
    *,
    start_delta: float = 0.0,
    end_delta: float = 0.0,
    granted_by: Granter | None = None,
    reason: str = "",
    exclusions: Sequence[tuple[float, float]] = (),
) -> Timeline:
    """Move a clip's in and out points within its recording (§42).

    Positive ``start_delta`` starts later; positive ``end_delta`` ends later.
    The source bounds are respected — a trim cannot invent footage before the
    beginning of a file, and asking for it is an error rather than a clamp,
    because silently doing something else is how a user loses trust in an edit.

    P0.3: narrowing needs no one. **Widening** -- an earlier start or a later
    end -- is a new authorized span, and the only granter this operation
    accepts is :attr:`Granter.HUMAN` (§78, §42): the Critic and the
    post-render critic only ever narrow, a style never reaches here, and
    nothing else may type a granter in. The grant is issued against the
    recording's ``exclusions`` and the trim is cut back to it, so a person
    cannot drag a clip into a menu either.
    """
    clip = _require_clip(timeline, clip_id)
    source_in = clip.source_in + start_delta
    source_out = clip.source_out + end_delta
    if source_in < -EPSILON:
        raise _invalid(f"trimming clip {clip_id} would start {-source_in:.3f}s before the file")

    widening = source_in < clip.source_in - EPSILON or source_out > clip.source_out + EPSILON
    metadata = dict(clip.metadata)
    if widening:
        if granted_by is not Granter.HUMAN:
            raise AuthorizationError(
                f"widening clip {clip_id} to [{source_in:.3f}, {source_out:.3f}] needs a "
                "grant, and only a person may give one here; "
                + ("no granter was named" if granted_by is None else f"{granted_by!r} may not"),
                details={"clip_id": clip_id, "granted_by": repr(granted_by)},
                recoverable=False,
            )
        span = issue(
            clip.media_id,
            max(0.0, source_in),
            source_out,
            Granter.HUMAN,
            reason or f"trimmed by hand: start {start_delta:+.2f} s, end {end_delta:+.2f} s",
            exclusions=exclusions,
        )
        # The grant may have been cut back at an exclusion; the trim follows it.
        source_in, source_out = max(source_in, span.start), min(source_out, span.end)
        chain = list(metadata.get("authorized") or [])
        chain.append(span.to_dict())
        metadata["authorized"] = chain
    if source_out - source_in < MIN_RESULT_SECONDS:
        raise _invalid(
            f"trimming clip {clip_id} would leave {source_out - source_in:.3f}s; "
            f"the minimum is {MIN_RESULT_SECONDS}s"
        )

    duration = (source_out - source_in) / clip.speed
    trimmed = clip.model_copy(
        update={
            "source_in": max(0.0, source_in),
            "source_out": source_out,
            "timeline_end": clip.timeline_start + duration,
            "metadata": metadata,
        }
    )
    return _rewrite(timeline, clip.track, _replace(timeline, clip, [trimmed]))


def move(timeline: Timeline, clip_id: str, to_index: int) -> Timeline:
    """Reorder a clip on its track (§42).

    The position is an index rather than a timestamp, because after a move
    every following clip's timestamp changes anyway, and an index is the thing
    the user actually meant.
    """
    clip = _require_clip(timeline, clip_id)
    track = _require_track(timeline, clip.track)
    clips = [item for item in track.in_order() if item.id != clip_id]
    if not 0 <= to_index <= len(clips):
        raise _invalid(
            f"index {to_index} is outside 0-{len(clips)} for the {clip.track.value} track"
        )
    clips.insert(to_index, clip)
    return _rewrite(timeline, clip.track, clips)


def delete(timeline: Timeline, clip_id: str) -> Timeline:
    """Disable a clip (§78). The footage stays; the viewer stops seeing it.

    This is what "cut that bit out" does. :func:`restore` is its exact inverse,
    which would not be true of removing the row.
    """
    return set_enabled(timeline, clip_id, enabled=False)


def restore(timeline: Timeline, clip_id: str) -> Timeline:
    """Re-enable a clip disabled by :func:`delete` (§78)."""
    return set_enabled(timeline, clip_id, enabled=True)


def set_enabled(timeline: Timeline, clip_id: str, *, enabled: bool) -> Timeline:
    clip = _require_clip(timeline, clip_id)
    if clip.enabled == enabled:
        return timeline
    updated = clip.model_copy(update={"enabled": enabled})
    return _rewrite(timeline, clip.track, _replace(timeline, clip, [updated]))


def remove(timeline: Timeline, clip_id: str) -> Timeline:
    """Take a clip out of the EDL entirely.

    Distinct from :func:`delete` because it is not undoable from the timeline
    alone: the source references go with it. Used when rebuilding, not editing.
    """
    clip = _require_clip(timeline, clip_id)
    return _rewrite(timeline, clip.track, _replace(timeline, clip, []))


def reflow(timeline: Timeline, kind: TrackKind = TrackKind.VIDEO) -> Timeline:
    """Re-lay a track end to end from zero, re-indexing as it goes.

    Called by every operation above. Exposed because a caller that has made
    several changes wants one re-flow, not one per change.
    """
    track = timeline.track(kind)
    if track is None:
        return timeline
    return _rewrite(timeline, kind, list(track.in_order()))


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _rewrite(timeline: Timeline, kind: TrackKind, clips: Sequence[TimelineClip]) -> Timeline:
    """Place ``clips`` in order on a track and hand back a new timeline.

    A disabled clip keeps its **place in the order** -- its ``clip_index`` --
    so restoring it puts it back between the same neighbours rather than at the
    end. It contributes no duration, because it renders as nothing; its stored
    timeline position is a placeholder, and the validator ignores it for the
    same reason.
    """
    laid_out: list[TimelineClip] = []
    cursor = 0.0
    contiguous = kind in {TrackKind.VIDEO, TrackKind.AUDIO}
    for index, clip in enumerate(clips):
        if contiguous:
            placed = clip.model_copy(
                update={
                    "clip_index": index,
                    "timeline_start": round(cursor, 6),
                    "timeline_end": round(cursor + clip.duration, 6),
                }
            )
            if clip.enabled:
                cursor += clip.duration
        else:
            placed = clip.model_copy(update={"clip_index": index})
        laid_out.append(placed)

    track = timeline.track(kind)
    name = track.name if track is not None else ""
    return timeline.with_track(Track(kind=kind, name=name, clips=tuple(laid_out)))


def _replace(
    timeline: Timeline, clip: TimelineClip, replacements: Iterable[TimelineClip]
) -> list[TimelineClip]:
    """The track's clips with ``clip`` swapped for ``replacements``, in order."""
    track = _require_track(timeline, clip.track)
    result: list[TimelineClip] = []
    for item in track.in_order():
        if item.id == clip.id:
            result.extend(replacements)
        else:
            result.append(item)
    return result


def _require_clip(timeline: Timeline, clip_id: str) -> TimelineClip:
    clip = timeline.clip(clip_id)
    if clip is None:
        raise ValidationError(
            f"No clip {clip_id!r} on this timeline.",
            code=ErrorCode.INVALID_TIMELINE_OPERATION,
            details={"clip_id": clip_id},
        )
    return clip


def _require_track(timeline: Timeline, kind: TrackKind) -> Track:
    track = timeline.track(kind)
    if track is None:
        raise ValidationError(
            f"This timeline has no {kind.value} track.",
            code=ErrorCode.INVALID_TIMELINE_OPERATION,
            details={"track": kind.value},
        )
    return track


def _invalid(message: str) -> ValidationError:
    return ValidationError(
        message.capitalize() + ".", code=ErrorCode.INVALID_TIMELINE_OPERATION
    )


__all__ = [
    "MIN_RESULT_SECONDS",
    "delete",
    "move",
    "reflow",
    "remove",
    "restore",
    "set_enabled",
    "split",
    "trim",
]
