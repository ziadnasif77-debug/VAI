"""Per-clip semantic levels, read once and shared by the stages that judge.

The pacing engine chooses a shot's length from its level; every stage that
then judges that shot -- the Critic trimming it, QA calling it a flash --
must read the level from the same place, or the system argues with itself
about which quick cut was deliberate.
"""

from __future__ import annotations

from typing import Any

from backend.core.logging import LogChannel, get_logger

logger = get_logger("semantic.levels", LogChannel.PIPELINE)


def clip_levels(
    database: Any,
    timeline: Any,
    *,
    config: Any,
) -> dict[int, str]:
    """``clip_index -> level`` for every video clip, or an empty map.

    Empty is a working answer, not a failure: a caller without levels falls
    back to its flat rule, which is what it did before V2 existed.
    """
    try:
        from backend.database.repositories.media import MediaRepository
        from backend.semantic.timeline import load_timeline

        if not config.editorial.pacing.dynamic:
            return {}
        media_repository = MediaRepository(database)
        timelines = {}
        for media_id in timeline.media_ids():
            media = media_repository.get(media_id)
            duration = (
                getattr(media.metadata, "duration_seconds", None) if media else None
            )
            if not duration:
                continue
            timelines[media_id] = load_timeline(
                database,
                media_id,
                duration_seconds=float(duration),
                config=config,
            )
        return {
            clip.clip_index: semantic.level_for(clip.source_in, clip.source_out)
            for clip in timeline.video_clips()
            if (semantic := timelines.get(clip.media_id)) is not None
        }
    except Exception:
        logger.exception("Clip levels unavailable; the caller uses its flat rule")
        return {}


def floor_for(level: str | None, config: Any) -> float:
    """The shortest a shot at ``level`` may be and still read (§V2 bands)."""
    bands = config.editorial.pacing.bands
    readability = float(config.editorial.pacing.min_piece_seconds)
    if level is None:
        return readability
    band = getattr(bands, level, None)
    if band is None:
        return readability
    # Half the band's own minimum, never under the readability floor: the
    # Critic may tighten a shot, not erase it.
    return max(readability, float(band.min) * 0.5)


__all__ = ["clip_levels", "floor_for"]
