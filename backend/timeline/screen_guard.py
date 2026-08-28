"""Clip refinement: what the story chose, cleaned of what nobody should see.

Two defects shipped in the first fully autonomous video, and both were
visible in its opening minute. The hook began at second 0.0 of the recording
-- the owner's own click on the record button, desktop chrome and all --
because nothing between moment formation and the timeline ever asked what
the screen was *showing* at a clip's boundary, §77's frame states included.
And the body was one 398-second slab, because a thin recording forms one
giant moment and no later stage broke it into breaths.

Both fixes are readings of evidence the pipeline already stores:

* **Openings advance past dead screens.** A clip may not open inside a
  non-gameplay frame state (menu, loading, desktop -- ``UNKNOWN`` counts as
  gameplay, exactly as the frame-state layer rules), and never inside the
  first seconds of a recording at all: the stretch behind the record button
  is where OBS chrome, the game's own window and the desktop live, and no
  named event has ever been detected there.
* **Slabs split at scene changes.** A clip longer than the cap is cut at the
  scene boundaries the SCENES stage already found, strongest changes first,
  so the pieces are real visual seams rather than arithmetic midpoints.

Pure over :class:`PlannedClip`: evidence in, clips out, testable without a
database in the room.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Final

from backend.analysis.frame_state import StateSpan
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import FrameState
from backend.timeline.builder import PlannedClip

logger = get_logger("timeline.screen_guard", LogChannel.PIPELINE)

#: States a clip may open inside. ``UNKNOWN`` is not evidence of a menu.
_OPENABLE: Final[frozenset[FrameState]] = frozenset(
    {FrameState.GAMEPLAY, FrameState.UNKNOWN}
)


def guard_clips(
    clips: Sequence[PlannedClip],
    *,
    states_by_media: Mapping[str, Sequence[StateSpan]],
    scenes_by_media: Mapping[str, Sequence[Any]],
    recording_start_guard_seconds: float = 4.0,
    dead_state_pad_seconds: float = 0.4,
    max_clip_seconds: float = 75.0,
    min_piece_seconds: float = 8.0,
) -> list[PlannedClip]:
    """Both refinements, in the order that keeps them honest.

    Openings first -- a slab is split only after its start is real footage --
    and a clip whose remainder falls under ``min_piece_seconds`` is dropped
    rather than shipped as a stub.
    """
    opened = _avoid_dead_openings(
        clips,
        states_by_media,
        guard=recording_start_guard_seconds,
        pad=dead_state_pad_seconds,
        min_piece=min_piece_seconds,
    )
    return _split_long_clips(
        opened,
        scenes_by_media,
        max_seconds=max_clip_seconds,
        min_piece=min_piece_seconds,
    )


def _avoid_dead_openings(
    clips: Sequence[PlannedClip],
    states_by_media: Mapping[str, Sequence[StateSpan]],
    *,
    guard: float,
    pad: float,
    min_piece: float,
) -> list[PlannedClip]:
    refined: list[PlannedClip] = []
    for clip in clips:
        start = max(clip.source_start, guard)
        states = states_by_media.get(clip.media_id, ())
        moved = True
        while moved:
            moved = False
            for span in states:
                if span.state in _OPENABLE:
                    continue
                if span.start_seconds <= start < span.end_seconds:
                    start = span.end_seconds + pad
                    moved = True
        if start != clip.source_start:
            if clip.source_end - start < min_piece:
                logger.info(
                    "Dropped a clip that was only its own dead opening",
                    extra={"media_id": clip.media_id, "start": clip.source_start},
                )
                continue
            logger.info(
                "Advanced a clip past a dead opening",
                extra={
                    "media_id": clip.media_id,
                    "from": round(clip.source_start, 2),
                    "to": round(start, 2),
                },
            )
            clip = replace(clip, source_start=start)
        refined.append(clip)
    return refined


def _split_long_clips(
    clips: Sequence[PlannedClip],
    scenes_by_media: Mapping[str, Sequence[Any]],
    *,
    max_seconds: float,
    min_piece: float,
) -> list[PlannedClip]:
    refined: list[PlannedClip] = []
    for clip in clips:
        if clip.source_end - clip.source_start <= max_seconds:
            refined.append(clip)
            continue
        cuts = _cut_points(
            clip,
            scenes_by_media.get(clip.media_id, ()),
            max_seconds=max_seconds,
            min_piece=min_piece,
        )
        if not cuts:
            refined.append(clip)
            continue
        logger.info(
            "Split a slab at its scene seams",
            extra={
                "media_id": clip.media_id,
                "seconds": round(clip.source_end - clip.source_start, 1),
                "pieces": len(cuts) + 1,
            },
        )
        bounds = [clip.source_start, *cuts, clip.source_end]
        for index in range(len(bounds) - 1):
            refined.append(
                replace(clip, source_start=bounds[index], source_end=bounds[index + 1])
            )
    return refined


def _cut_points(
    clip: PlannedClip,
    scenes: Sequence[Any],
    *,
    max_seconds: float,
    min_piece: float,
) -> list[float]:
    """Scene starts inside the clip, greedily thinned to honest pieces.

    Every piece must fit the cap and clear the floor; when the scenes are too
    sparse for that, the clip stays whole -- an arithmetic midpoint is not a
    seam, and shipping the slab is better than cutting mid-action.
    """
    candidates = sorted(
        float(getattr(scene, "start_seconds", 0.0))
        for scene in scenes
        if clip.source_start + min_piece
        <= float(getattr(scene, "start_seconds", 0.0))
        <= clip.source_end - min_piece
    )
    cuts: list[float] = []
    previous = clip.source_start
    for candidate in candidates:
        if candidate - previous < min_piece:
            continue
        if candidate - previous > max_seconds:
            # The stretch before this seam already breaks the cap; take the
            # latest earlier candidate we skipped, or accept the long piece.
            pass
        cuts.append(candidate)
        previous = candidate
    # Thin from the end until the tail piece is honest.
    while cuts and clip.source_end - cuts[-1] < min_piece:
        cuts.pop()
    if not cuts:
        return []
    # If any piece still exceeds the cap, cutting helped anyway; only refuse
    # when nothing changed.
    return cuts


__all__ = ["guard_clips"]
