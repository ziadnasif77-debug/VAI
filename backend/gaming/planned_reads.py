"""The base frames the edit will use, and nobody has looked at (P0.2.2).

The exclusion layer (:mod:`backend.gaming.content`) refuses what a detector
saw. OCR and vision read the *candidate* frames -- the cascade's keyframes,
roughly one every five seconds with gaps that run to a hundred and sixty --
while the FRAMES stage extracts a frame every three seconds and stores every
one of them. On the acceptance render that difference was a three-second pause
menu at 2:43: no candidate frame fell inside it, and the base frame that
showed it in full sat on disk with ``analyzed = 0``.

Reading every base frame would cost as much as the OCR stage again. Reading
the ones that fall inside the *planned* clips costs a tenth of that and is
where the answer matters: a menu outside the edit was never going to reach the
video. Measured on the 88-minute benchmark before this was built: 368 frames
in 569 s, 20 of the 24 unsampled stretches inside the edit observed, one real
pause menu found, and **zero seconds of gameplay wrongly refused**.

This module is the selection -- pure, so the rule can be tested without a
database or an OCR engine. The EDL worker does the reading and the storing.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from typing import Any, Final

#: One base interval either side of a clip. A menu that is on screen when the
#: clip opens was on screen before it: the frame that proves it can sit just
#: outside the boundary. On the benchmark the pause menu's frame was 0.27 s
#: before the clip that carried it.
DEFAULT_MARGIN_SECONDS: Final[float] = 3.0

#: A base frame this close to a stored OCR or vision sample adds little: the
#: sample already speaks for that second. Two seconds keeps the pass to the
#: frames that change the answer.
DEFAULT_MIN_GAP_SECONDS: Final[float] = 2.0


def select(
    frames: Sequence[Any],
    spans: Sequence[tuple[float, float]],
    looked: Sequence[float],
    *,
    margin_seconds: float = DEFAULT_MARGIN_SECONDS,
    min_gap_seconds: float = DEFAULT_MIN_GAP_SECONDS,
) -> list[Any]:
    """The frames worth reading: inside a planned span, unread, unsampled.

    Args:
        frames: rows with ``timestamp`` and ``analyzed``, any sampling level
            the caller chose (the worker passes the base pass).
        spans: ``(start, end)`` in source seconds -- the planned clips.
        looked: timestamps a detector already sampled, in any order.
        margin_seconds: how far outside a span a frame may sit and still
            count. Zero means strictly inside.
        min_gap_seconds: a frame closer than this to a stored sample is
            skipped. Zero reads every frame inside the spans.

    Returns the chosen rows in time order. A frame already marked ``analyzed``
    is never returned: that flag is how a second EDL run does not read the
    same frame twice, and how the OCR stage, which resets it, asks for a fresh
    pass after it replaces the reads.
    """
    if not frames or not spans:
        return []
    ordered = sorted(float(at) for at in looked)
    reach = max(0.0, float(margin_seconds))
    gap = max(0.0, float(min_gap_seconds))
    chosen = []
    for frame in sorted(frames, key=lambda item: float(item.timestamp)):
        if bool(getattr(frame, "analyzed", False)):
            continue
        at = float(frame.timestamp)
        if not any(start - reach <= at <= end + reach for start, end in spans):
            continue
        if gap > 0 and _distance(ordered, at) < gap:
            continue
        chosen.append(frame)
    return chosen


def _distance(ordered: Sequence[float], at: float) -> float:
    """Seconds to the nearest stored sample, or infinity when there is none."""
    if not ordered:
        return float("inf")
    index = bisect.bisect_left(ordered, at)
    best = float("inf")
    for candidate in (index - 1, index):
        if 0 <= candidate < len(ordered):
            best = min(best, abs(ordered[candidate] - at))
    return best


__all__ = ["DEFAULT_MARGIN_SECONDS", "DEFAULT_MIN_GAP_SECONDS", "select"]
