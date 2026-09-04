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
from backend.editorial.jump_cuts import Budget, Evidence, decide
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
    seam_hints_by_media: Mapping[str, Sequence[float]] | None = None,
    level_stops_by_media: Mapping[str, Sequence[float]] | None = None,
    no_cut_by_media: Mapping[str, Sequence[tuple[float, float]]] | None = None,
    jump_cut_gap: float = 0.0,
    jump_cut_below: float = 0.0,
    cap_fn=None,
    onsets_by_media: Mapping[str, Sequence[float]] | None = None,
    reactions_by_media: Mapping[str, Sequence[tuple[float, float]]] | None = None,
    jump_cut_min_gap_seconds: float | None = None,
    jump_cut_budget: Budget | None = None,
) -> list[PlannedClip]:
    """Both refinements, in the order that keeps them honest.

    P0.6: every second the guard removes from inside a clip -- a dead
    interior, the sliver after a hot cut -- is a jump cut, and each one is
    asked of :func:`backend.editorial.jump_cuts.decide` with the evidence
    here: the words (``no_cut_by_media``), the event onsets, the reactions,
    and the dead states. ``jump_cut_min_gap_seconds`` defaults to
    ``bridge_interior_seconds``: the same line that already said a shorter
    dead stretch is bridged, not cut.

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
    min_gap = (
        bridge_interior_seconds if jump_cut_min_gap_seconds is None else jump_cut_min_gap_seconds
    )
    evidence_by_media = {
        media_id: Evidence(
            words=tuple((no_cut_by_media or {}).get(media_id, ())),
            onsets=tuple((onsets_by_media or {}).get(media_id, ())),
            reactions=tuple((reactions_by_media or {}).get(media_id, ())),
            dead=tuple(
                (span.start_seconds, span.end_seconds)
                for span in states_by_media.get(media_id, ())
                if span.state not in _OPENABLE
            ),
        )
        for media_id in {clip.media_id for clip in clips}
    }
    excised = _excise_dead_interiors(
        opened,
        states_by_media,
        pad=dead_state_pad_seconds,
        min_piece=min_piece_seconds,
        bridge_seconds=bridge_interior_seconds,
        evidence_by_media=evidence_by_media,
        min_gap=min_gap,
        budget=jump_cut_budget,
    )
    return _split_long_clips(
        excised,
        scenes_by_media,
        cap_fn=cap_fn,
        events_by_media=events_by_media or {},
        seam_hints_by_media=seam_hints_by_media or {},
        level_stops_by_media=level_stops_by_media or {},
        no_cut_by_media=no_cut_by_media or {},
        jump_cut_gap=jump_cut_gap,
        jump_cut_below=jump_cut_below,
        evidence_by_media=evidence_by_media,
        min_gap=min_gap,
        budget=jump_cut_budget,
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

#: How long an event must sit inside a stillness span before it may vouch
#: for any of it. The scope and the standoff hold for seconds; the combat
#: that began 0.03 s before a pause menu closed was the game resuming.
_VETO_MIN_OVERLAP_SECONDS = 1.0


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
      generic ``unknown_event`` blesses nothing.
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
                # Only an event that happens *during* the stillness may
                # vouch for it: the scope, the standoff, the cutscene kill.
                # An event that begins after the stillness ends is the game
                # resuming, and its neighbourhood must not reach back into
                # what came before. Measured across every probe-detected
                # freeze on this machine (2026-09-03): four spans were
                # blessed by a neighbouring event alone, and all four were
                # menus or a loading screen -- one of them the pause menu
                # that reached a finished video, because combat began 0.03 s
                # after it closed. Zero gameplay cases. The overlapping kind
                # is the one the veto was measured for and keeps it -- overlap
                # meaning at least a second of the event inside the stillness,
                # because that combat did technically begin 0.03 s before the
                # menu closed.
                during = [
                    (start, end)
                    for start, end in events
                    if min(end, span.end_seconds) - max(start, span.start_seconds)
                    >= _VETO_MIN_OVERLAP_SECONDS
                ]
                remains = _minus_neighbourhoods(span, during) if during else [span]
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
    evidence_by_media: Mapping[str, Evidence] | None = None,
    min_gap: float = 4.0,
    budget: Budget | None = None,
) -> list[PlannedClip]:
    """Cut the dead stretches out of a clip's middle, not only its opening.

    P0.6: each dead stretch is a jump cut and is asked of ``decide`` --
    refused when its edges fall inside a word, when an event onset or a
    reaction lies inside it, or when the budget is spent -- and a refused
    stretch stays in the clip, logged with its reason.

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
        if dead and evidence_by_media is not None:
            evidence = evidence_by_media.get(clip.media_id, Evidence())
            allowed: list[StateSpan] = []
            for span in dead:
                lo = max(span.start_seconds, clip.source_start)
                hi = min(span.end_seconds, clip.source_end)
                verdict = decide(lo, hi, evidence, min_gap_seconds=min_gap, budget=budget)
                if verdict:
                    allowed.append(span)
                else:
                    logger.info(
                        "Kept a dead stretch inside a clip: jump cut refused",
                        extra={
                            "media_id": clip.media_id,
                            "start": round(lo, 2),
                            "reason": verdict.reason,
                        },
                    )
            dead = allowed
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
    cap_fn=None,
    events_by_media: Mapping[str, Sequence[tuple[float, float]]] | None = None,
    seam_hints_by_media: Mapping[str, Sequence[float]] | None = None,
    level_stops_by_media: Mapping[str, Sequence[float]] | None = None,
    no_cut_by_media: Mapping[str, Sequence[tuple[float, float]]] | None = None,
    jump_cut_gap: float = 0.0,
    jump_cut_below: float = 0.0,
    evidence_by_media: Mapping[str, Evidence] | None = None,
    min_gap: float = 4.0,
    budget: Budget | None = None,
) -> list[PlannedClip]:
    refined: list[PlannedClip] = []
    for clip in clips:
        seams = sorted(
            [float(getattr(scene, "start_seconds", 0.0))
             for scene in scenes_by_media.get(clip.media_id, ())]
            + [start for start, _end in (events_by_media or {}).get(clip.media_id, ())]
            + list((seam_hints_by_media or {}).get(clip.media_id, ()))
        )
        if cap_fn is not None:
            # V2's dynamic pacing walks the clip instead of capping it once:
            # a planned clip can hold a calm setup AND the fight it leads
            # into, and a single cap cuts the setup at the fight's pace. The
            # level is read again at every cut, so the pace follows the heat
            # continuously and each piece's length answers to its own stretch.
            refined.extend(
                _walk(
                    clip,
                    cap_fn=cap_fn,
                    seams=seams,
                    stops=(level_stops_by_media or {}).get(clip.media_id, ()),
                    no_cut=(no_cut_by_media or {}).get(clip.media_id, ()),
                    min_piece=min_piece,
                    jump_cut_gap=jump_cut_gap,
                    jump_cut_below=jump_cut_below,
                    evidence=(evidence_by_media or {}).get(clip.media_id),
                    min_gap=min_gap,
                    budget=budget,
                )
            )
            continue

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
            # The static path keeps V1's judgement: no seam, ship the slab.
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


#: How much footage is read to grade the position a cut starts from. Half the
#: shape's minimum segment: long enough that one loud frame cannot promote a
#: calm stretch, short enough to see a level change when it arrives.
_PROBE_SECONDS: Final[float] = 2.0


def _out_of(
    cut: float, forbidden: Sequence[tuple[float, float]], *, floor_at: float, ceiling: float
) -> float:
    """Move ``cut`` off any span it may not land inside.

    The zones are spoken sentences: a cut inside one is a defect the viewer
    hears, and the pacing rule that holds a shot for the speaker only helps
    when the shot *starts* while they are talking. A shot that begins in
    silence and runs into a sentence needed this.

    Forward first -- finishing the sentence is what a person would do -- and
    backwards only if the end runs past the clip. Neither move is allowed to
    produce a piece under the floor, so a cut with nowhere legal to go stays
    where it was rather than becoming a sliver.
    """
    for start, stop in forbidden:
        if not start < cut < stop:
            continue
        if stop <= ceiling:
            return stop
        if start >= floor_at:
            return start
        break
    return cut


def _before(
    cut: float, forbidden: Sequence[tuple[float, float]], *, floor_at: float
) -> float:
    """Pull ``cut`` back to the start of any span it lands inside.

    The backwards half of :func:`_out_of`, for the one cut that cannot move
    forward: the final shot of the video, whose end the plan fixed.
    """
    for start, stop in forbidden:
        if start < cut < stop and start >= floor_at:
            return start
    return cut


def _paced(
    clip: PlannedClip, start: float, end: float, rules: Sequence[str]
) -> PlannedClip:
    """One piece, carrying the rules that decided its length (§80)."""
    piece = replace(clip, source_start=start, source_end=end)
    if not rules:
        return piece
    return replace(piece, sources=(*piece.sources, *(f"pacing: {rule}" for rule in rules)))


def _walk(
    clip: PlannedClip,
    *,
    cap_fn,
    seams: Sequence[float],
    stops: Sequence[float] = (),
    no_cut: Sequence[tuple[float, float]] = (),
    min_piece: float,
    jump_cut_gap: float,
    jump_cut_below: float,
    evidence: Evidence | None = None,
    min_gap: float = 4.0,
    budget: Budget | None = None,
) -> list[PlannedClip]:
    """Cut forward through a clip, re-reading the session at every piece.

    P0.6: the sliver skipped after a hot cut (``jump_cut_gap``) is a jump cut
    and is asked of ``decide`` like any other. Live footage is never dead, so
    on live footage the pieces resume exactly where they cut and play as one
    unbroken shot; the skip survives only across a dead stretch. Without
    evidence (no caller gave any) the old behaviour stands.

    The length is not a property of the clip; it is a property of the second
    the cut starts on. Measuring the plan the other way round -- one cap for
    the slab, each piece graded on its own afterwards -- is what made a calm
    stretch inside a hot slab come out at the hot pace.

    ``cap_fn`` may answer with a plain number or with anything carrying
    ``seconds`` and ``rules``. When it carries rules they ride on the piece,
    so a finished video can say why each shot is the length it is (§80).
    """
    pieces: list[PlannedClip] = []
    refused: list[str] = []
    position = clip.source_start
    end = clip.source_end
    guard = 0
    previous_length = 0.0
    while position < end and guard < 5000:
        guard += 1
        probe = replace(
            clip,
            source_start=position,
            source_end=min(position + _PROBE_SECONDS, end),
        )
        decision = cap_fn(probe, previous_length)
        cap = float(decision)
        rules = tuple(getattr(decision, "rules", ()))
        # A hot band's cap can sit far below the global piece floor; a floor
        # that scales with the cap is what lets a climax actually cut fast.
        floor = min(min_piece, max(0.8, cap * 0.45))
        remaining = end - position
        # A hot piece skips a sliver of source before the next one: played
        # contiguously they would be one unbroken shot no viewer could feel,
        # and the skip is what makes the cut a felt jump-cut.
        gap = jump_cut_gap if cap < jump_cut_below else 0.0
        if remaining <= cap:
            # The last piece ends where the plan says, and the plan does not
            # know about words -- the video's final shot ended half a second
            # inside its final syllable. Forward is not available here, so
            # this one snaps back.
            close = _before(end, no_cut, floor_at=position + floor)
            pieces.append(_paced(clip, position, close, rules))
            break

        def resume_after(cut: float, *, floor: float = floor, gap: float = gap) -> float:
            """Where the next piece starts: after the skip, or exactly at the cut."""
            if gap <= 0.0 or end - (cut + gap) < floor:
                return cut
            if evidence is None:
                return cut + gap
            verdict = decide(cut, cut + gap, evidence, min_gap_seconds=min_gap, budget=budget)
            if verdict:
                return cut + gap
            refused.append(verdict.reason)
            return cut

        target = position + cap
        # A shot does not span a change of level. Where the session turns
        # calmer or hotter inside this piece, that turn is the piece's end --
        # otherwise a clip starting in a quiet second runs seven seconds deep
        # into a climax at the quiet second's pace.
        turns = [t for t in stops if position + floor <= t < target]
        if turns:
            target = min(turns)
        # Land on the last real seam that still fits the cap; without one the
        # cut falls on the cap itself rather than shipping an over-long slab.
        candidates = [t for t in seams if position + floor <= t <= target]
        cut = max(candidates) if candidates else target
        cut = _out_of(cut, no_cut, floor_at=position + floor, ceiling=end)
        if end - cut < floor:
            # Cutting here would leave a sliver. Halve the tail instead --
            # two honest pieces, both inside the cap -- and only ship it long
            # when even halves would fall under the floor.
            if remaining / 2 >= floor:
                middle = position + remaining / 2
                pieces.append(_paced(clip, position, middle, rules))
                resumed = resume_after(middle)
                pieces.append(_paced(clip, resumed, end, rules))
            else:
                pieces.append(_paced(clip, position, end, rules))
            break
        pieces.append(_paced(clip, position, cut, rules))
        previous_length = cut - position
        position = resume_after(cut)
    if refused:
        # Once per clip, not once per piece: the reason is the same sentence
        # every time on live footage.
        pieces = [
            replace(
                piece,
                sources=(*piece.sources, f"jump cut refused ({len(refused)}x): {refused[0]}"),
            )
            for piece in pieces
        ]
    if len(pieces) > 1:
        logger.info(
            "Walked a slab at the pace of its own heat",
            extra={
                "media_id": clip.media_id,
                "seconds": round(clip.source_end - clip.source_start, 1),
                "pieces": len(pieces),
            },
        )
    return pieces or [clip]


def _cut_points(
    clip: PlannedClip,
    scenes: Sequence[Any],
    *,
    max_seconds: float,
    min_piece: float,
) -> list[float]:
    """Seams inside the clip, greedily fitted to honest pieces.

    Every piece must clear the floor and stretch as close to the cap as the
    seams allow; when they are too sparse even for that, the long piece ships
    whole -- an arithmetic midpoint is not a seam.
    """
    candidates = sorted(
        t
        for scene in scenes
        if clip.source_start + min_piece
        <= (t := float(getattr(scene, "start_seconds", 0.0)))
        <= clip.source_end - min_piece
    )
    # Cut as LATE as the cap allows: each piece stretches toward the cap
    # and lands on the last seam that still fits. Cutting at every eligible
    # seam -- the first version -- shredded calm stretches to the floor the
    # moment the seams were dense.
    cuts: list[float] = []
    previous = clip.source_start
    viable: float | None = None
    index = 0
    while index < len(candidates):
        candidate = candidates[index]
        if candidate - previous < min_piece:
            index += 1
            continue
        if candidate - previous <= max_seconds:
            viable = candidate
            index += 1
            continue
        if viable is None:
            # No seam fits under the cap; a long honest piece beats an
            # arithmetic midpoint mid-action.
            cuts.append(candidate)
            previous = candidate
            index += 1
            continue
        cuts.append(viable)
        previous = viable
        viable = None  # re-judge this candidate against the new start
    if viable is not None and clip.source_end - previous > max_seconds:
        cuts.append(viable)
    # Thin from the end until the tail piece is honest.
    while cuts and clip.source_end - cuts[-1] < min_piece:
        cuts.pop()
    return cuts


__all__ = ["guard_clips"]
