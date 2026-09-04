"""The last pass over the cut points, where an editor's habits live (§29, §39).

Two stages set clip boundaries before this, and each undoes the other's care.
§29 expands a moment's context and snaps it to scene cuts and speech — at
*segment* level, which Whisper defeats by emitting 44-second segments holding
five sentences. Then §39 trims that context proportionally to hit the target
duration, with no awareness of speech at all: it re-cuts what §29 just snapped.

Measured on a finished video, the result was 8 of 26 cut points landing
mid-sentence — one clip cut the player off mid-word, skipped nine seconds, and
the next clip resumed *inside the same sentence*. Two more clips were adjacent
in the recording and joined with a 30 ms audio fade in the middle of continuous
speech. Nothing downstream could repair either: the EDL reproduces the plan
clip for clip, deliberately (§40).

So this runs last, inside ``build_plan``, after ordering and the hook — the
final cut points, refined the way an editor would:

**Cut in pauses, not words.** Word timestamps are already stored (§14). A
boundary that lands inside a word run moves to the nearest inter-word pause
within a small window. Segment-level snapping cannot do this; word-level can,
because the pauses between sentences exist inside Whisper's mega-segments even
when the segmentation does not.

**Cut early in the pause, not in the middle.** Where inside the pause matters
as much as being in one. Leake et al. (SIGGRAPH 2017) measured dialogue scenes
and put ~90% of a retained gap *before* the incoming line and ~10% after the
outgoing one -- speech resumes almost as soon as the new shot lands, instead of
both clips hanging in half a silence each. One position serves both boundary
kinds: an out-point keeps a short tail after its last word, an in-point inherits
the rest of the gap as lead-in, and because the two fractions sum to one, two
clips cut at the same pause of the same take still reproduce continuous time.
The tail never shrinks below a floor that covers trailing S/P sounds, which
Whisper's word-end timestamps habitually clip.

**Never give back the event.** The core span (§28) is protected: a start may
not move meaningfully past the moment's first event, an end may not retreat
meaningfully before its last. "Meaningfully" is ``core_give_seconds`` — event
edges come from detectors with seconds of slop, and when the optimiser trims
all context the boundary *is* the core edge, pinned mid-word by a rule meant
to protect a kill, not a syllable. A mid-word cut is still a smaller failure
than a missing kill, which is why the give is small and bounded.

**One stretch of footage is one clip.** Consecutive clips that continue the
same recording are merged. The join was inaudible in the picture and audible in
the sound: every join carries a 30 ms fade (§72), which in continuous speech is
a dip mid-sentence.

Everything here is pure and returns new objects; ``build_plan`` stays a
function from moments to a plan.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

from ai.providers.base import TranscriptSegment
from backend.core.logging import LogChannel, get_logger
from backend.moments.formation import Moment, replace_moment

logger = get_logger("narrative.refinement", LogChannel.PIPELINE)

#: Two clips whose source spans are this close continue the same footage.
ADJACENCY_SECONDS: float = 0.05

#: A boundary within this of a word is "inside speech". Slightly generous,
#: because word timestamps wobble by a frame or two.
WORD_PAD_SECONDS: float = 0.15


@dataclass(frozen=True, slots=True)
class RefinementResult:
    """What came back, and what was done — for §80's notes."""

    moments: tuple[Moment, ...]
    beats: tuple[str, ...]
    notes: tuple[str, ...]
    snapped_cuts: int = 0
    merged_clips: int = 0


class SpeechIndex:
    """Word runs and the pauses between them, for one recording.

    Built once per media and queried per boundary. ``pauses`` are candidate cut
    positions: one inside every inter-word gap at least ``min_pause`` long,
    plus a position just before the first word and just after the last —
    cutting right against a word's edge clips its breath.

    The position inside a gap is ``pause_split`` of the way through it — 0.1
    puts the cut just after the outgoing words, so an in-point lands with ~90%
    of the gap as lead-in and speech resumes almost immediately (Leake et al.,
    SIGGRAPH 2017). ``min_trailing`` floors the tail so the cut clears the
    trailing consonants word-end timestamps clip; structurally the floor is
    also held above ``WORD_PAD_SECONDS``, or the chosen position would itself
    count as inside speech — values below that are clamped up, so the
    effective minimum tail is ``WORD_PAD_SECONDS + 0.05``. A gap that cannot
    hold the clearance on both sides yields no candidate at all: any position
    inside it sits against a word's breath, which is the cut this index exists
    to prevent, whatever ``min_pause`` was tuned down to.
    """

    __slots__ = ("_starts", "_words", "pauses")

    def __init__(
        self,
        segments: Sequence[TranscriptSegment],
        min_pause: float,
        *,
        pause_split: float = 0.1,
        min_trailing: float = 0.2,
    ) -> None:
        words: list[tuple[float, float]] = []
        for segment in segments:
            if segment.words:
                words.extend((w.start, w.end) for w in segment.words)
            elif segment.text.strip():
                # No word timing survived (an unbatched fallback, or an old
                # analysis). The whole segment becomes one opaque word run:
                # coarser, but it still keeps cuts out of the middle of it.
                words.append((segment.start, segment.end))
        words.sort()
        self._words = words
        self._starts = [start for start, _ in words]

        clearance = max(min_trailing, WORD_PAD_SECONDS + 0.05)
        pauses: list[float] = []
        if words:
            pauses.append(words[0][0] - WORD_PAD_SECONDS - 0.05)
            for (_, left_end), (right_start, _) in pairwise(words):
                gap = right_start - left_end
                if gap < min_pause or gap < 2 * clearance:
                    continue
                tail = min(max(pause_split * gap, clearance), gap - clearance)
                pauses.append(left_end + tail)
            pauses.append(words[-1][1] + WORD_PAD_SECONDS + 0.05)
        self.pauses = pauses

    def inside_speech(self, position: float) -> bool:
        """Whether ``position`` falls within a word, give or take the pad."""
        index = bisect_left(self._starts, position)
        for candidate in (index - 1, index):
            if 0 <= candidate < len(self._words):
                start, end = self._words[candidate]
                if start - WORD_PAD_SECONDS <= position <= end + WORD_PAD_SECONDS:
                    return True
        return False

    def nearest_pause(
        self, position: float, low: float, high: float
    ) -> float | None:
        """The closest pause to ``position`` inside ``[low, high]``, if any."""
        best: float | None = None
        for pause in self.pauses:
            if not low <= pause <= high:
                continue
            if best is None or abs(pause - position) < abs(best - position):
                best = pause
        return best


def refine(
    moments: Sequence[Moment],
    beats: Sequence[str],
    speech_by_media: Mapping[str, Sequence[TranscriptSegment]],
    *,
    snap_window_seconds: float = 2.5,
    min_pause_seconds: float = 0.35,
    pause_split: float = 0.1,
    min_trailing_seconds: float = 0.2,
    core_give_seconds: float = 1.0,
    stretch_window_seconds: float = 4.0,
    duration_by_media: Mapping[str, float] | None = None,
    enabled: bool = True,
) -> RefinementResult:
    """Snap every final cut to a pause, then merge continuous footage.

    Order matters: snapping first can close a small source gap between two
    clips, which the merge then collapses. Merging first would leave the seam
    unexamined.

    ``duration_by_media`` bounds every move. Whisper will happily timestamp a
    word past the end of the file it heard, and a boundary snapped to a "pause"
    after that word points at footage that does not exist — the EDL then clamps
    it and the plan and the timeline disagree about the same clip (§40).
    """
    padded_beats = list(beats) + ["body"] * (len(moments) - len(beats))
    if not enabled or not moments:
        return RefinementResult(tuple(moments), tuple(padded_beats[: len(moments)]), ())

    indexes = {
        media_id: SpeechIndex(
            segments,
            min_pause_seconds,
            pause_split=pause_split,
            min_trailing=min_trailing_seconds,
        )
        for media_id, segments in speech_by_media.items()
    }

    # Every clip's claim on its recording, so a snap can only roam the
    # unclaimed footage between clips. Without this, one clip's end snapped
    # into the next clip's span — the same two seconds of footage in the video
    # twice, which the EDL's exclusivity guard then trimmed, leaving the plan
    # and the timeline disagreeing about the same clip (§40).
    claims: dict[str, list[tuple[float, float, int]]] = {}
    for position, moment in enumerate(moments):
        claims.setdefault(moment.media_id, []).append(
            (moment.context_start, moment.context_end, position)
        )

    snapped_total = 0
    stuck_total = 0
    refined: list[Moment] = []
    for position, moment in enumerate(moments):
        index = indexes.get(moment.media_id)
        if index is None:
            refined.append(moment)
            continue
        media_end = (duration_by_media or {}).get(moment.media_id)
        neighbours = claims.get(moment.media_id, ())
        floor = max(
            (
                end
                for start, end, other in neighbours
                if other != position and end <= moment.context_start + ADJACENCY_SECONDS
            ),
            default=None,
        )
        ceiling = min(
            (
                start
                for start, end, other in neighbours
                if other != position and start >= moment.context_end - ADJACENCY_SECONDS
            ),
            default=None,
        )
        if media_end is not None:
            ceiling = media_end if ceiling is None else min(ceiling, media_end)
        moment, snapped = _snap_moment(
            moment,
            index,
            snap_window_seconds,
            stretch=max(stretch_window_seconds, snap_window_seconds),
            give=core_give_seconds,
            floor=floor,
            ceiling=ceiling,
        )
        snapped_total += snapped
        stuck_total += sum(
            1
            for boundary in (moment.context_start, moment.context_end)
            if index.inside_speech(boundary)
        )
        refined.append(moment)

    merged, kept_beats, merged_count = _merge_adjacent(refined, padded_beats)

    notes: list[str] = []
    if snapped_total:
        notes.append(f"moved {snapped_total} cut(s) out of mid-speech into a pause")
    if merged_count:
        notes.append(f"merged {merged_count} join(s) inside continuous footage")
    if stuck_total:
        # §80: an editor who had to cut mid-take says so. These boundaries sit
        # in speech runs with no reachable pause in any allowed direction.
        notes.append(f"{stuck_total} cut(s) remain mid-speech: no pause within reach")
    if notes:
        logger.info(
            "Refined the final cut points",
            extra={"snapped": snapped_total, "merged": merged_count},
        )
    return RefinementResult(
        tuple(merged), tuple(kept_beats), tuple(notes), snapped_total, merged_count
    )


def _snap_moment(
    moment: Moment,
    index: SpeechIndex,
    window: float,
    *,
    stretch: float | None = None,
    give: float = 0.0,
    floor: float | None = None,
    ceiling: float | None = None,
) -> tuple[Moment, int]:
    """Move this clip's boundaries out of mid-speech, core span respected.

    ``floor`` and ``ceiling`` are the nearest claimed edges in the source —
    another clip's end behind this one's start, another clip's start ahead of
    this one's end, and the end of the recording itself. A cut may only move
    through footage nobody else is showing.
    """
    updates: dict[str, float] = {}
    snapped = 0
    reach = window if stretch is None else stretch

    if index.inside_speech(moment.context_start):
        # The start may roam the window in both directions but never forward
        # past the first event — §28's core is content, not context.
        # Earlier only adds context, so it may reach further than the window.
        low = moment.context_start - reach
        if floor is not None:
            low = max(low, floor)
        pause = index.nearest_pause(
            moment.context_start,
            low,
            # ``give`` past the core start: an event edge carries seconds of
            # detector slop, and pinning a cut mid-word to honour the edge to
            # the decisecond protects a syllable, not the kill.
            min(moment.context_start + window, moment.start_seconds + give),
        )
        if pause is not None:
            updates["context_start"] = max(0.0, pause)
            snapped += 1

    if index.inside_speech(moment.context_end):
        # Later only lets the sentence finish -- the classic edit.
        high = moment.context_end + reach
        if ceiling is not None:
            high = min(high, ceiling)
        pause = index.nearest_pause(
            moment.context_end,
            max(moment.context_end - window, moment.end_seconds - give),
            high,
        )
        if pause is not None:
            updates["context_end"] = pause
            snapped += 1

    if not updates:
        return moment, 0
    result = replace_moment(moment, **updates)
    # P0.3: a cut moved to add context is a widening by refinement (§29);
    # the grant is issued later, against the exclusions.
    earlier = max(moment.context_start - result.context_start, 0.0)
    later = max(result.context_end - moment.context_end, 0.0)
    if earlier > 1e-6 or later > 1e-6:
        from backend.moments.grants import note_widening
        from backend.timeline.authorization import Granter

        result = note_widening(
            result,
            Granter.REFINEMENT,
            start=result.context_start,
            end=result.context_end,
            reason=(
                f"refinement: cut moved out of speech, start -{earlier:.1f} s / "
                f"end +{later:.1f} s"
            ),
        )
    return result, snapped


def _merge_adjacent(
    moments: Sequence[Moment], beats: Sequence[str]
) -> tuple[list[Moment], list[str], int]:
    """Collapse consecutive clips that continue the same recording.

    The merged clip keeps the first clip's beat — it is the one the sequence
    arrived at — and the higher score of the pair, because the merge is a
    presentation decision and must not demote a strong moment in any later
    ranking (§68's effects read these scores).
    """
    merged: list[Moment] = []
    kept_beats: list[str] = []
    merges = 0

    for moment, beat in zip(moments, beats, strict=True):
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.media_id == moment.media_id
            # Adjacent -- or overlapping *forwards*: two clips sharing footage
            # are one situation twice, and downstream the EDL's exclusivity
            # guard would trim the copy silently and mid-word (§40). A real
            # plan arrived with a 3.5s overlap between consecutive clips.
            #
            # Forwards matters. A hook sequence jumps backwards on purpose --
            # the teaser from minute 20, then the body from zero -- and "starts
            # before the previous ended" is true of that jump too. Without the
            # continuation checks the hook swallowed the whole body.
            and moment.context_start - previous.context_end <= ADJACENCY_SECONDS
            and moment.context_start >= previous.context_start - ADJACENCY_SECONDS
            and moment.context_end >= previous.context_end - ADJACENCY_SECONDS
        ):
            joined = replace_moment(
                previous,
                end_seconds=max(previous.end_seconds, moment.end_seconds),
                context_end=max(previous.context_end, moment.context_end),
                events=(*previous.events, *moment.events),
                score=max(previous.score, moment.score),
            )
            # P0.3: the following clip's own grants travel with the merge, so
            # the seconds it was authorised for stay credited to their
            # granters and only the seam is refinement's.
            from backend.moments.grants import METADATA_KEY

            theirs = list(moment.metadata.get(METADATA_KEY) or [])
            if theirs:
                joined = replace_moment(
                    joined,
                    metadata={
                        **joined.metadata,
                        METADATA_KEY: [*(joined.metadata.get(METADATA_KEY) or []), *theirs],
                    },
                )
            if joined.context_end > previous.context_end + 1e-6:
                # The seam between two clips is footage neither grant covered
                # on its own; the union is a widening by refinement.
                from backend.moments.grants import note_widening
                from backend.timeline.authorization import Granter

                joined = note_widening(
                    joined,
                    Granter.REFINEMENT,
                    start=joined.context_start,
                    end=joined.context_end,
                    reason=(
                        f"refinement: merged with the following clip, "
                        f"+{joined.context_end - previous.context_end:.1f} s"
                    ),
                )
            merged[-1] = joined
            merges += 1
            continue
        merged.append(moment)
        kept_beats.append(beat)

    return merged, kept_beats, merges


__all__ = ["ADJACENCY_SECONDS", "RefinementResult", "SpeechIndex", "refine"]
