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
from backend.core.models.enums import FrameState
from backend.database.repositories.media import MediaRepository
from backend.media.ffmpeg import FFmpegRunner
from backend.metadata.generation import thumbnail_arguments, thumbnail_peak
from backend.metadata.hooks import burn_hook, hook_phrase

#: States a thumbnail may be cut from. The recording app is never the video's
#: face: measured 2026-08-28, a published thumbnail was the OBS window with
#: the game alive only in its preview.
_LIVE_STATES = frozenset({FrameState.GAMEPLAY, FrameState.HUD_ONLY, FrameState.UNKNOWN})

logger = get_logger("metadata.thumbnail", LogChannel.PIPELINE)

THUMBNAIL_FILENAME: Final[str] = "thumbnail.jpg"


def render_thumbnail(
    *,
    database,
    config,
    assets_dir: Path,
    moments: list[Any],
    language: str | None,
    hook_text: str | None = None,
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

    at_seconds = _live_peak(
        database=database,
        config=config,
        media=media,
        moments=moments,
        media_id=media_id,
        at_seconds=at_seconds,
        assets_dir=assets_dir,
    )

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

    if config.publishing.defaults.thumbnail_composition:
        _recompose(destination, config)

    if config.publishing.defaults.thumbnail_hook:
        phrase, emoji = hook_phrase(moments, language)
        burn_hook(destination, hook_text or phrase, emoji)
    return str(destination)


def _recompose(destination: Path, config) -> None:
    """§22-§23: crop and zoom to the subject the vision model locates."""
    try:
        from ai.vision import create_vision_provider
        from backend.metadata.composition import compose

        provider = create_vision_provider(config)
        if provider is None or not hasattr(provider, "locate_subject"):
            return
        box = provider.locate_subject(destination)
        if box is not None:
            compose(destination, box)
    except Exception:
        logger.exception("Thumbnail composition unavailable; the frame ships as extracted")


def _live_peak(
    *,
    database,
    config,
    media,
    moments,
    media_id: str,
    at_seconds: float,
    assets_dir: Path,
) -> float:
    """The peak, moved off any dead screen it landed on.

    The same evidence the screen guard reads -- §77's frame states plus the
    recorder probe -- applied to the one frame the channel wears. A peak
    inside a dead span walks to the span's end; if the whole neighbourhood is
    dead, the next-scoring moment gets its turn, and the last resort is the
    first live stretch of the recording.
    """
    try:
        from ai.ocr import create_ocr_provider
        from backend.analysis import frame_state as frame_state_module
        from backend.analysis.recorder_probe import recorder_spans
        from backend.database.repositories.vision import VisionRepository

        duration = getattr(media.metadata, "duration_seconds", None)
        spans = list(
            frame_state_module.spans(
                VisionRepository(database).list_for_media(media_id),
                duration_seconds=duration,
            )
        )
        try:
            ocr = create_ocr_provider(config)
        except Exception:
            ocr = None
        spans.extend(
            recorder_spans(
                Path(media.source_path),
                ffmpeg=FFmpegRunner(config.ffmpeg),
                ocr=ocr,
                scratch_dir=assets_dir / "recorder-probe",
            )
        )
        dead = [span for span in spans if span.state not in _LIVE_STATES]
        if not dead:
            return at_seconds

        def moved(candidate: float) -> float:
            changed = True
            while changed:
                changed = False
                for span in dead:
                    if span.start_seconds <= candidate < span.end_seconds:
                        candidate = span.end_seconds + 0.5
                        changed = True
            return candidate

        first = moved(max(at_seconds, 4.0))
        limit = (duration or first + 1.0) - 0.5
        if first <= limit and abs(first - at_seconds) < 20.0:
            return first
        # The chosen peak's whole neighbourhood is dead: give the other
        # moments their turn, best first.
        ranked = sorted(
            (m for m in moments if str(getattr(m, "media_id", "")) == media_id),
            key=lambda m: -float(getattr(m, "score", 0.0)),
        )
        for moment in ranked:
            candidate = moved(max(float(getattr(moment, "start_seconds", 0.0)), 4.0))
            if candidate <= min(float(getattr(moment, "end_seconds", candidate)), limit):
                logger.info(
                    "Thumbnail peak moved off a dead screen",
                    extra={"from": round(at_seconds, 1), "to": round(candidate, 1)},
                )
                return candidate
        return min(moved(4.0), limit)
    except Exception:
        logger.exception("Live-peak check failed; using the raw peak")
        return at_seconds


__all__ = ["THUMBNAIL_FILENAME", "render_thumbnail"]
