"""Overlapping moments are one situation, and a situation keeps its onsets.

P0.6, the owner's fifth item (docs/PLAN.md, 2026-09-04). On the benchmark the
kill of Silvio Caruso was shown as its consequence alone: the body thrown into
the valley at 16:48, the kill at 16:40 nowhere. The moment carrying the kill
(chaos 975.5-1033.6 s, score 0.30) overlapped two stronger moments and the
selection dropped it -- by score, as the knapsack is built to -- and with it
the one event the survivors did not contain. Measured across the whole session:
43 of 61 moments dropped, 12 of them overlapping a kept moment, 8 of those
carrying an event onset no kept moment contained. That is a rule, not a case.

The rule, in two halves:

* **Two chosen moments never overlap.** Two clips sharing footage are one
  situation twice; the EDL's exclusivity guard used to trim the copy silently
  (§40), and refinement merged what it could. The selection now refuses the
  pair at the source, so every overlapping sibling of a chosen moment is a
  dropped moment by construction -- and the second half applies.
* **A dropped sibling's onsets go with the anchor.** For every dropped moment
  that overlaps a chosen one, each of its events whose importance reaches
  ``min_importance`` and whose onset lies inside no chosen moment is absorbed:
  the anchor's context grows to cover the event's own span (the event, not the
  sibling's whole core -- a chaos core runs to 150 s and is not "the
  event-carrying part"), its core grows the same way so the duration trim
  cannot take it back, the sibling's own grants travel with it (P0.3: the
  seconds it was authorised for stay credited), and the seam is a widening by
  refinement, marked with its seconds. What was absorbed is written into the
  moment's metadata and its explanation, so the story result can say why a
  clip is longer than its moment.

Nothing here invents a second: an anchor only ever grows into footage a
sibling was already authorised to show.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.moments.formation import Moment, replace_moment
from backend.moments.grants import METADATA_KEY, note_widening
from backend.timeline.authorization import Granter

#: Where the absorption is recorded on the moment.
SITUATION_KEY = "situation"


@dataclass(frozen=True, slots=True)
class Absorbed:
    """One dropped sibling's onsets, kept by an anchor."""

    moment_id: str
    onsets: tuple[float, ...]
    seconds: float


def overlaps(left: Moment, right: Moment) -> bool:
    """Whether two moments' contexts share footage on the same recording."""
    return (
        left.media_id == right.media_id
        and left.context_start < right.context_end
        and right.context_start < left.context_end
    )


def overlaps_any(moment: Moment, chosen: Sequence[Moment]) -> bool:
    return any(overlaps(moment, other) for other in chosen)


def committed_duration(moment: Moment, pool: Sequence[Moment], *, min_importance: float) -> float:
    """The seconds choosing ``moment`` commits the edit to.

    Its own context, plus the spans of every event at or above
    ``min_importance`` carried by a moment overlapping it whose onset lies
    outside it -- because choosing it drops every overlapping sibling, and the
    sibling's onsets then come with it. An upper bound: an onset another
    chosen moment holds is not absorbed twice, and the growth stops at a
    chosen neighbour's edge; neither is known before the selection. The
    knapsack prices this so the target it lands on is the target the edit
    will have, not one the absorption then overshoots.
    """
    low, high = moment.context_start, moment.context_end
    for other in pool:
        if other is moment or not overlaps(moment, other):
            continue
        for event in other.events:
            onset = float(event.start_seconds)
            if float(event.importance) < min_importance:
                continue
            if moment.context_start <= onset <= moment.context_end:
                continue
            low = min(low, onset)
            high = max(high, float(event.end_seconds))
    return high - low


def absorb_onsets(
    chosen: Sequence[Moment],
    dropped: Sequence[Moment],
    *,
    min_importance: float,
) -> tuple[list[Moment], list[Absorbed], float]:
    """Give every chosen moment the onsets its dropped, overlapping siblings carried.

    Returns the chosen moments (grown where they absorbed), what was absorbed,
    and the seconds added in total.
    """
    anchors = list(chosen)
    absorbed: list[Absorbed] = []
    added = 0.0
    for sibling in dropped:
        # The anchors this sibling overlaps; the first in time takes its onsets.
        indices = [i for i, anchor in enumerate(anchors) if overlaps(anchor, sibling)]
        if not indices:
            continue
        index = min(indices, key=lambda i: anchors[i].context_start)
        anchor = anchors[index]
        kept = [
            event
            for event in sibling.events
            if float(event.importance) >= min_importance
            and not _inside(float(event.start_seconds), anchors)
        ]
        if not kept:
            continue
        event_low = min(float(e.start_seconds) for e in kept)
        event_high = max(float(e.end_seconds) for e in kept)
        # An event's span may run into the next chosen moment; the anchor
        # stops at that edge -- two chosen moments never overlap, and the
        # footage past the edge is the neighbour's to show.
        others = [
            m for i, m in enumerate(anchors) if i != index and m.media_id == anchor.media_id
        ]
        after = [m.context_start for m in others if m.context_start >= anchor.context_end]
        before = [m.context_end for m in others if m.context_end <= anchor.context_start]
        event_high = min([event_high, *after])
        event_low = max([event_low, *before])
        low = min(anchor.context_start, event_low)
        high = max(anchor.context_end, event_high)
        seconds = (high - low) - anchor.context_duration
        onsets = [float(e.start_seconds) for e in kept]
        anchors[index] = _grow(
            anchor,
            sibling,
            kept,
            low=low,
            high=high,
            event_low=event_low,
            event_high=event_high,
            seconds=seconds,
        )
        absorbed.append(Absorbed(sibling.id, tuple(round(o, 3) for o in onsets), round(seconds, 3)))
        added += seconds
    return anchors, absorbed, round(added, 3)


def _inside(at: float, moments: Sequence[Moment]) -> bool:
    return any(m.context_start <= at <= m.context_end for m in moments)


def _grow(
    anchor: Moment,
    sibling: Moment,
    kept: Sequence,
    *,
    low: float,
    high: float,
    event_low: float,
    event_high: float,
    seconds: float,
) -> Moment:
    onsets = [float(e.start_seconds) for e in kept]
    events = tuple(anchor.events) + tuple(e for e in kept if e not in anchor.events)
    record = list(anchor.metadata.get(SITUATION_KEY) or [])
    record.append(
        {
            "moment_id": sibling.id,
            "onsets": [round(o, 3) for o in onsets],
            "seconds": round(seconds, 3),
        }
    )
    theirs = list(sibling.metadata.get(METADATA_KEY) or [])
    metadata = {
        **anchor.metadata,
        SITUATION_KEY: record,
        METADATA_KEY: [*(anchor.metadata.get(METADATA_KEY) or []), *theirs],
    }
    onset_text = ", ".join(f"{o:.1f} s" for o in onsets)
    grown = replace_moment(
        anchor,
        # The absorbed event is content now, not context: the duration trim
        # shaves pre-roll and post-roll and never crosses the core.
        start_seconds=min(anchor.start_seconds, event_low),
        end_seconds=max(anchor.end_seconds, event_high),
        events=events,
        explanation=(
            *anchor.explanation,
            f"kept the onset(s) at {onset_text} from the overlapping moment "
            f"{sibling.id or '?'} the selection dropped (+{seconds:.1f} s)",
        ),
        metadata=metadata,
    )
    if seconds <= 1e-6:
        return grown
    return note_widening(
        grown,
        Granter.REFINEMENT,
        start=low,
        end=high,
        reason=(
            f"situation: kept the onset(s) at {onset_text} of the overlapping moment "
            f"{sibling.id or '?'}, +{seconds:.1f} s"
        ),
    )


__all__ = [
    "SITUATION_KEY",
    "Absorbed",
    "absorb_onsets",
    "committed_duration",
    "overlaps",
    "overlaps_any",
]
