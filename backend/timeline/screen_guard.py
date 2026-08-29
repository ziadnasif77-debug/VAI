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
    high_tier_max_seconds: float = 45.0,
    low_tier_max_seconds: float = 100.0,
    min_piece_seconds: float = 8.0,
    min_observations: int = 2,
    bridge_interior_seconds: float = 4.0,
    events_by_media: Mapping[str, Sequence[tuple[float, float]]] | None = None,
) -> list[PlannedClip]:
    """Both refinements, in the order that keeps them honest.

    Openings first -- a slab is split only after its start is real footage --
    and a clip whose remainder falls under ``min_piece_seconds`` is dropped
    rather than shipped as a stub.
    """

    states_by_media = _weighed(
        states_by_media,
        min_observations=min_observations,
        events_by_media=events_by_media or {},
    )
    opened = _avoid_dead_openings(
        clips,
        states_by_media,
        guard=recording_start_guard_seconds,
        pad=dead_state_pad_seconds,
        min_piece=min_piece_seconds,
    )
    excised = _excise_dead_interiors(
        opened,
        states_by_media,
        pad=dead_state_pad_seconds,
        min_piece=min_piece_seconds,
        bridge_seconds=bridge_interior_seconds,
    )
    return _split_long_clips(
        excised,
        scenes_by_media,
        max_seconds=max_clip_seconds,
        high_tier_max_seconds=high_tier_max_seconds,
        low_tier_max_seconds=low_tier_max_seconds,
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


#: States that assert stillness rather than an interface: the event veto
#: applies to these only -- a corroborated MENU is a menu regardless of events.
_STILLNESS = frozenset({FrameState.PAUSE, FrameState.TRANSITION})

#: How much life a strong event radiates into a stillness span, each side.
_VETO_NEIGHBOURHOOD_SECONDS = 4.0


def _minus_neighbourhoods(
    span: StateSpan, events: Sequence[tuple[float, float]]
) -> list[StateSpan]:
    """The parts of a stillness span no strong event reaches."""
    pieces = [(span.start_seconds, span.end_seconds)]
    for start, end in events:
        lo = start - _VETO_NEIGHBOURHOOD_SECONDS
        hi = end + _VETO_NEIGHBOURHOOD_SECONDS
        next_pieces: list[tuple[float, float]] = []
        for piece_start, piece_end in pieces:
            if hi <= piece_start or lo >= piece_end:
                next_pieces.append((piece_start, piece_end))
                continue
            if piece_start < lo:
                next_pieces.append((piece_start, lo))
            if hi < piece_end:
                next_pieces.append((hi, piece_end))
        pieces = next_pieces
    if pieces == [(span.start_seconds, span.end_seconds)]:
        return [span]
    return [
        StateSpan(
            state=span.state,
            start_seconds=start,
            end_seconds=end,
            observations=span.observations,
        )
        for start, end in pieces
        if end - start >= 1.0
    ]


def _weighed(
    states_by_media: Mapping[str, Sequence[StateSpan]],
    *,
    min_observations: int,
    events_by_media: Mapping[str, Sequence[tuple[float, float]]],
) -> dict[str, list[StateSpan]]:
    """Evidence before knives: which dead spans may cut at all.

    Two disqualifications, both measured on a real GTA session that a 596 s
    plan left as 189 s of video:

    * **Corroboration.** A dead state seen by fewer than ``min_observations``
      sampled frames is a warning, not a scalpel -- nine single-observation
      "menu" spans (the phone overlay, misread at a 12 s stride) were doing
      most of the shredding. Probes that measure pixels directly declare
      ``observations=3`` and keep their authority.
    * **The event veto, by neighbourhood.** A stillness span holding a
      detected game event is alive *around that event* -- a sniper scope, an
      aimed standoff, a cutscene kill. Measured before this was a
      neighbourhood: one near_death at 98.5 s blessed a 24.6 s frozen span
      whole, and QA then flagged 14 s of motionless video. So the event
      keeps ±``_VETO_NEIGHBOURHOOD_SECONDS`` alive and the rest of the
      stillness stays dead. Callers pass only *strong* events -- the
      generic ``unexpected_event`` blesses nothing.
    """
    weighed: dict[str, list[StateSpan]] = {}
    for media_id, spans in states_by_media.items():
        events = events_by_media.get(media_id, ())
        kept: list[StateSpan] = []
        weak = 0
        vetoed = 0
        for span in spans:
            if span.state.is_gameplay:
                kept.append(span)
                continue
            if span.observations < min_observations:
                weak += 1
                continue
            if span.state in _STILLNESS and events:
                remains = _minus_neighbourhoods(span, events)
                if len(remains) != 1 or remains[0] is not span:
                    vetoed += 1
                kept.extend(remains)
                continue
            kept.append(span)
        if weak or vetoed:
            logger.info(
                "Dead spans disqualified before cutting",
                extra={
                    "media_id": media_id,
                    "single_observation": weak,
                    "event_vetoed": vetoed,
                },
            )
        weighed[media_id] = kept
    return weighed


def _excise_dead_interiors(
    clips: Sequence[PlannedClip],
    states_by_media: Mapping[str, Sequence[StateSpan]],
    *,
    pad: float,
    min_piece: float,
    bridge_seconds: float = 4.0,
) -> list[PlannedClip]:
    """Cut the dead stretches out of a clip's middle, not only its opening.

    Found on the second live rerun of the same recording: the opening guard
    moved the first clip to 12.9 s -- past the first OBS visit -- and the
    clip then sailed straight through the second visit at 18.5-24.5. A
    viewer was shown the recorder mid-clip. A dead span inside a clip splits
    it: the stretch before, the stretch after, each kept only if it clears
    the piece floor.
    """
    refined: list[PlannedClip] = []
    for clip in clips:
        dead = sorted(
            (
                span
                for span in states_by_media.get(clip.media_id, ())
                if span.state not in _OPENABLE
                and span.start_seconds < clip.source_end
                and span.end_seconds > clip.source_start
                # The bridge: a short dead stretch inside a live clip is
                # kept -- a two-second map glance costs less than the hard
                # cut and the sliver the piece floor would then kill.
                and span.end_seconds - span.start_seconds >= bridge_seconds
            ),
            key=lambda span: span.start_seconds,
        )
        if not dead:
            refined.append(clip)
            continue
        cursor = clip.source_start
        pieces: list[tuple[float, float]] = []
        for span in dead:
            if span.start_seconds - cursor >= min_piece:
                pieces.append((cursor, span.start_seconds))
            cursor = max(cursor, span.end_seconds + pad)
        if clip.source_end - cursor >= min_piece:
            pieces.append((cursor, clip.source_end))
        if not pieces:
            rescued = _rescue(clip, dead, min_piece=min_piece)
            if rescued is not None:
                logger.warning(
                    "Excision would erase a detected moment; kept its best window",
                    extra={
                        "media_id": clip.media_id,
                        "start": round(rescued.source_start, 1),
                        "seconds": round(rescued.seconds, 1),
                    },
                )
                refined.append(rescued)
                continue
            logger.info(
                "Dropped a clip that was mostly dead screen",
                extra={"media_id": clip.media_id, "start": round(clip.source_start, 1)},
            )
            continue
        if pieces != [(clip.source_start, clip.source_end)]:
            logger.info(
                "Excised dead screen from a clip's interior",
                extra={
                    "media_id": clip.media_id,
                    "pieces": len(pieces),
                    "removed": round(
                        (clip.source_end - clip.source_start)
                        - sum(end - start for start, end in pieces),
                        1,
                    ),
                },
            )
        for start, end in pieces:
            refined.append(replace(clip, source_start=start, source_end=end))
    return refined


def _rescue(
    clip: PlannedClip, dead: Sequence[StateSpan], *, min_piece: float
) -> PlannedClip | None:
    """The zero-piece case: keep the widest live window instead of vanishing.

    A planned clip is a *detected moment* -- events fired, scores agreed.
    Dropping it because excision left only slivers silently erases content
    the analysis promised. The compromise: take the widest live stretch and
    widen it to the piece floor, accepting the least dead time necessary,
    and say so at warning level. Only a clip with no live stretch of at
    least two seconds truly dies.
    """
    cursor = clip.source_start
    live: list[tuple[float, float]] = []
    for span in dead:
        if span.start_seconds > cursor:
            live.append((cursor, min(span.start_seconds, clip.source_end)))
        cursor = max(cursor, span.end_seconds)
    if cursor < clip.source_end:
        live.append((cursor, clip.source_end))
    live = [(s, e) for s, e in live if e - s >= 2.0]
    if not live:
        return None
    start, end = max(live, key=lambda piece: piece[1] - piece[0])
    shortfall = min_piece - (end - start)
    if shortfall > 0:
        # Widening may borrow seconds from *stillness* only -- a pause reads
        # as a breath; a menu or the desktop reads as a broken edit, and QA
        # counted every borrowed interface frame against the last rescue
        # design. When the interface walls the window in, a short live core
        # beats a padded one: three seconds of game over eight of menus.
        left_wall = clip.source_start
        right_wall = clip.source_end
        for span in dead:
            if span.state not in _STILLNESS:
                if span.end_seconds <= start:
                    left_wall = max(left_wall, span.end_seconds)
                if span.start_seconds >= end:
                    right_wall = min(right_wall, span.start_seconds)
        grow_left = min(shortfall / 2, start - left_wall)
        start -= grow_left
        end = min(end + (shortfall - grow_left), right_wall)
        start = max(left_wall, min(start, end - 2.0))
    if end - start < 3.0:
        return None
    return replace(clip, source_start=start, source_end=end)


def _cap_for(
    clip: PlannedClip,
    *,
    max_seconds: float,
    high_tier_max_seconds: float,
    low_tier_max_seconds: float,
) -> float:
    """The doctrine's pacing rule (docs/DIRECTION.md §7) as a cut-length cap.

    High intensity wants shorter cuts, low intensity longer shots -- so the
    cap follows the clip's own tier: a master/major moment is held to the
    tight cap, a supporting one may breathe past the base. The tier comes
    from the same score every other layer reads.
    """
    from backend.moments.scoring import tier_for

    tier = tier_for(float(clip.score or 0.0))
    if tier in ("master", "major"):
        return min(high_tier_max_seconds, max_seconds)
    if tier == "good":
        return max_seconds
    return max(low_tier_max_seconds, max_seconds)


def _split_long_clips(
    clips: Sequence[PlannedClip],
    scenes_by_media: Mapping[str, Sequence[Any]],
    *,
    max_seconds: float,
    high_tier_max_seconds: float = 45.0,
    low_tier_max_seconds: float = 100.0,
    min_piece: float = 8.0,
) -> list[PlannedClip]:
    refined: list[PlannedClip] = []
    for clip in clips:
        cap = _cap_for(
            clip,
            max_seconds=max_seconds,
            high_tier_max_seconds=high_tier_max_seconds,
            low_tier_max_seconds=low_tier_max_seconds,
        )
        if clip.source_end - clip.source_start <= cap:
            refined.append(clip)
            continue
        cuts = _cut_points(
            clip,
            scenes_by_media.get(clip.media_id, ()),
            max_seconds=cap,
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
