"""The suggested thumbnail, renderable from anywhere that holds the pieces.

Extracted from the metadata route the day auto-publish needed it: the route
and the publish worker both want "one frame at the best moment's peak, with
the hook burned on", and a worker cannot hold an AppState. This function
holds the whole recipe; its callers hold only their own plumbing.

Written into the project's assets directory (§43: user-facing artefacts live
with the project), so the export screen and a later publication find it at a
stable path. Regeneration overwrites: the suggestion is derived state, and
two thumbnails for one project would only raise which one is real. Every
failure is ``None`` and a log line -- metadata without a thumbnail is still
metadata.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from backend.core.errors import ErrorCode, GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.database.repositories.media import MediaRepository
from backend.media.ffmpeg import FFmpegRunner
from backend.metadata.generation import thumbnail_arguments, thumbnail_peak
from backend.metadata.hooks import burn_hook, hook_phrase

logger = get_logger("metadata.thumbnail", LogChannel.PIPELINE)

THUMBNAIL_FILENAME: Final[str] = "thumbnail.jpg"


def render_thumbnail(
    *,
    database,
    config,
    assets_dir: Path,
    moments: list[Any],
    language: str | None,
) -> str | None:
    """Extract the peak frame, burn the hook, return the path -- or ``None``."""
    peak = thumbnail_peak(moments)
    if peak is None:
        return None
    media_id, at_seconds = peak
    media = MediaRepository(database).get(media_id)
    if media is None or not media.source_path:
        return None
    source = Path(media.source_path)
    if not source.is_file():
        return None

    destination = assets_dir / THUMBNAIL_FILENAME
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        runner = FFmpegRunner(config.ffmpeg)
        runner.run(
            [*runner.base_arguments(), *thumbnail_arguments(source, at_seconds, destination)],
            error_code=ErrorCode.FRAME_EXTRACTION_FAILED,
            details={"media_id": media_id},
        )
    except (GamingEditorError, OSError) as error:
        logger.warning(
            "Thumbnail extraction failed; the metadata is suggested without one",
            extra={"media_id": media_id, "error": str(error)},
        )
        return None

    if config.publishing.defaults.thumbnail_hook:
        phrase, emoji = hook_phrase(moments, language)
        burn_hook(destination, phrase, emoji)
    return str(destination)


__all__ = ["THUMBNAIL_FILENAME", "render_thumbnail"]
