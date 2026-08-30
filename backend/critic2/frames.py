"""Frames of the finished video, described.

The one thing in this pipeline that decodes the render in order to *see* it.
QA decodes it too, but for signal properties -- black, freeze, loudness -- and
a video can pass every one of those while showing the same corridor four times.

Sampled uniformly, and that is not laziness. The defects this exists to find
are about what recurs, and a sample biased toward the interesting parts is
blind to exactly them: the analysis stage's own cascade nominates candidate
regions, which is right for finding moments and wrong for noticing that
nothing has changed for half a minute.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.core.errors import ErrorCode
from backend.core.logging import LogChannel, get_logger

logger = get_logger("critic2.frames", LogChannel.QA)


def sample_times(duration_seconds: float, *, max_frames: int) -> list[float]:
    """Evenly spaced instants, never the very first or last frame."""
    if duration_seconds <= 0 or max_frames <= 0:
        return []
    count = max(1, min(max_frames, int(duration_seconds // 2)))
    step = duration_seconds / (count + 1)
    return [round(step * (index + 1), 3) for index in range(count)]


def extract(
    context: Any, render_path: Path, times: Sequence[float], into: Path
) -> list[tuple[float, Path]]:
    """Pull one JPEG per instant out of the render.

    One FFmpeg call per frame rather than one pass with a select filter: a
    seek is cheap on a finished MP4, and a single failure then costs one
    frame instead of the whole sample.
    """
    into.mkdir(parents=True, exist_ok=True)
    taken: list[tuple[float, Path]] = []
    for index, at in enumerate(times):
        destination = into / f"{index:04d}.jpg"
        try:
            context.ffmpeg.run(
                [
                    *context.ffmpeg.base_arguments(),
                    "-ss",
                    f"{at:.3f}",
                    "-i",
                    str(render_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    str(destination),
                ],
                timeout_seconds=60,
                error_code=ErrorCode.ENCODING_FAILED,
            )
        except Exception:
            logger.info("A frame could not be read from the render", extra={"at": at})
            continue
        if destination.is_file():
            taken.append((at, destination))
    return taken


def describe(
    context: Any,
    *,
    render_path: Path,
    clips: Sequence[Any],
    provider: Any = None,
    max_frames: int = 60,
) -> dict[str, tuple[str, ...]]:
    """``clip_id -> labels`` for what the finished video actually shows.

    Degrades to an empty map, which is a real answer: without a vision model
    the critic still measures everything the lanes and the plan can tell it,
    and simply cannot speak about what recurs on screen.
    """
    duration = max((clip.timeline_end for clip in clips), default=0.0)
    times = sample_times(duration, max_frames=max_frames)
    if not times:
        return {}

    into = context.paths.analysis / "critic2-frames"
    taken = extract(context, render_path, times, into)
    if not taken:
        return {}

    described = _labels(context, taken, provider)
    by_clip: dict[str, list[str]] = {}
    for at, labels in described:
        clip = _clip_at(clips, at)
        if clip is None:
            continue
        by_clip.setdefault(clip.id, []).extend(labels)
    return {clip_id: tuple(sorted(set(labels))) for clip_id, labels in by_clip.items()}


def _labels(
    context: Any, taken: Sequence[tuple[float, Path]], provider: Any
) -> list[tuple[float, tuple[str, ...]]]:
    """Ask the vision model what each frame shows, in batches."""
    try:
        if provider is None:
            from ai.vision import create_vision_provider

            provider = create_vision_provider(context.config)
    except Exception:
        logger.info("No vision model for the critic; it watches without labels")
        return []

    batch = max(1, int(context.config.analysis.vision.max_frames_per_request))
    described: list[tuple[float, tuple[str, ...]]] = []
    try:
        for start in range(0, len(taken), batch):
            chunk = taken[start : start + batch]
            try:
                observations = provider.describe(
                    tuple(path for _at, path in chunk),
                    tuple(at for at, _path in chunk),
                )
            except Exception:
                logger.exception("A batch of render frames could not be described")
                continue
            for (at, _path), observation in zip(chunk, observations, strict=False):
                described.append((at, tuple(getattr(observation, "labels", ()) or ())))
    finally:
        # §54: the card goes back whether or not the watching succeeded, and a
        # tidy-up failure never costs the findings that were already made.
        with contextlib.suppress(Exception):
            provider.unload()
    return described


def _clip_at(clips: Sequence[Any], at: float):
    for clip in clips:
        if clip.timeline_start <= at < clip.timeline_end:
            return clip
    return clips[-1] if clips else None


__all__ = ["describe", "extract", "sample_times"]
