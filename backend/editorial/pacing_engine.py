"""Dynamic pacing — the owner's bands as a function of the moment (P1-B).

The static 2.2 clips/minute retires from law to indicator. Each selected
clip is classified by the Semantic Timeline's level over its own span, and
the level's band decides how long a cut may run: calm breathes to fifteen
seconds, a climax cuts under two -- same footage, same order, higher
density where the session itself burns hotter.

Chronology is constitutional here as everywhere in V2: this engine shapes
DURATIONS by splitting at real scene seams; it never reorders, and the
validator downstream would refuse it if it tried.
"""

from __future__ import annotations

from typing import Any

from backend.core.logging import LogChannel, get_logger
from backend.semantic.timeline import SemanticTimeline

logger = get_logger("editorial.pacing", LogChannel.PIPELINE)


def cap_for(
    clip: Any, timeline: SemanticTimeline | None, config: Any, *, fallback: float
) -> float:
    """The maximum seconds this clip may run, by its semantic level.

    Without a timeline (dynamic off, or evidence missing) the caller's
    static fallback stands -- V1 behaviour, untouched.
    """
    pacing = config.editorial.pacing
    if timeline is None or not pacing.dynamic:
        return fallback
    level = timeline.level_for(clip.source_start, clip.source_end)
    band = getattr(pacing.bands, level)
    return float(band.max)


def describe(clip: Any, timeline: SemanticTimeline | None, config: Any) -> str | None:
    """A §80 reason string for logs and plans, or None when static."""
    if timeline is None or not config.editorial.pacing.dynamic:
        return None
    level = timeline.level_for(clip.source_start, clip.source_end)
    band = getattr(config.editorial.pacing.bands, level)
    return f"{level} pacing: cuts target {band.min:.1f}-{band.max:.1f}s"


__all__ = ["cap_for", "describe"]
