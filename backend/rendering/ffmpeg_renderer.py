"""Cutting and concatenating the programme (SPEC §65, §67, §7, §47).

The EDL says which seconds of which recordings the video is made of. This turns
that into one continuous file.

Clip by clip, not in one enormous filter graph. A single `filter_complex`
trimming seventy clips is a legitimate way to do this and a bad way to *ship*
it: no progress that means anything, no way to resume after a crash three
quarters of the way through a twenty-minute encode, and a failure that reports
one unreadable graph rather than the clip that broke. Segments give §47 its
resume, §82 a cancellation point every few seconds, and a stack trace that
names a clip.

The cost is a second encode. Segments are therefore written at visually
lossless quality (`intermediate_arguments`), because every artefact introduced
here survives into the finished video, and disk is the cheapest thing in this
pipeline.

Nothing is ever held in memory. FFmpeg reads and writes files; §7's rule is
kept by not having a code path that could break it.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, MediaError, RenderError
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.media.ffmpeg import CancelledError, FFmpegRunner, format_seconds, progress_arguments
from backend.media.probe import probe_media
from backend.rendering.encoder import EncoderChoice, EncodeTarget, intermediate_arguments
from backend.timeline.models import TimelineClip

logger = get_logger("rendering.renderer", LogChannel.RENDERING)

RenderProgress = Callable[[float, str], None]
CancelCheck = Callable[[], bool]

#: Where the per-clip intermediates live, under the project's render directory.
SEGMENT_DIRNAME: Final[str] = "segments"

#: The concat demuxer's list file.
SEGMENT_LIST_FILENAME: Final[str] = "segments.txt"

#: How far a segment's measured duration may sit from its requested one before
#: it is treated as unfinished. A frame at 30 fps is 33 ms; this allows two.
SEGMENT_TOLERANCE_SECONDS: Final[float] = 0.08


@dataclass(frozen=True, slots=True)
class RenderedProgramme:
    """The concatenated video, and what went into it."""

    path: Path
    clips: int
    duration_seconds: float
    reused_segments: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "clips": self.clips,
            "duration_seconds": round(self.duration_seconds, 3),
            "reused_segments": self.reused_segments,
            "notes": list(self.notes),
        }


def render_programme(
    clips: Sequence[TimelineClip],
    sources: dict[str, Path],
    *,
    destination: Path,
    work_dir: Path,
    runner: FFmpegRunner,
    config: AppConfig,
    encoder: EncoderChoice,
    target: EncodeTarget,
    on_progress: RenderProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> RenderedProgramme:
    """Cut every enabled clip and concatenate them into one file (§65).

    Args:
        clips: the timeline's video clips, in order. Disabled clips are skipped
            here rather than filtered by the caller, so "what the viewer sees"
            has one definition.
        sources: recording paths by media id.
        destination: the programme video.
        work_dir: where segments are written. Kept between runs so an
            interrupted render resumes (§47).

    Returns:
        The concatenated programme. Its duration is *measured*, not assumed:
        the sum of the requested spans and what FFmpeg actually produced can
        differ by a frame per cut, and every later stage compares against a
        real number.
    """
    enabled = [clip for clip in clips if clip.enabled]
    if not enabled:
        raise RenderError(
            "The timeline has no enabled clips to render.",
            code=ErrorCode.RENDER_FAILED,
            details={"clips": len(clips)},
            recoverable=False,
        )

    segments_dir = Path(work_dir) / SEGMENT_DIRNAME
    segments_dir.mkdir(parents=True, exist_ok=True)
    total_seconds = sum(clip.duration for clip in enabled)

    segments: list[Path] = []
    reused = 0
    elapsed = 0.0
    for position, clip in enumerate(enabled):
        if should_cancel is not None and should_cancel():
            raise CancelledError("Render cancelled between clips.")

        source = sources.get(clip.media_id)
        if source is None or not source.is_file():
            raise RenderError(
                f"The recording for clip {clip.clip_index} is not available.",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"media_id": clip.media_id, "clip_id": clip.id},
                recoverable=False,
            )

        segment = segments_dir / f"{position:05d}_{clip.id}.mp4"
        if _is_complete(segment, clip.duration, runner):
            reused += 1
        else:
            _cut(
                clip,
                source,
                segment,
                runner=runner,
                config=config,
                encoder=encoder,
                target=target,
                should_cancel=should_cancel,
            )
        segments.append(segment)

        elapsed += clip.duration
        if on_progress is not None:
            on_progress(
                min(0.9, elapsed / max(total_seconds, 1e-6) * 0.9),
                f"Cut {position + 1}/{len(enabled)} clips",
            )

    if on_progress is not None:
        on_progress(0.92, "Joining the clips")
    _concatenate(segments, destination, segments_dir, runner, config)

    measured = probe_media(destination, runner).duration_seconds or 0.0
    result = RenderedProgramme(
        path=destination,
        clips=len(enabled),
        duration_seconds=measured,
        reused_segments=reused,
        notes=(
            (f"{reused} segment(s) reused from an earlier run",) if reused else ()
        ),
    )
    logger.info("Rendered the programme video", extra=result.summary())
    return result


def clear_segments(work_dir: Path) -> int:
    """Delete the intermediates. Returns how many files went.

    Called once the finished file exists. They are kept until then on purpose:
    §47's resume is worth more during a render than the disk is after one.
    """
    segments_dir = Path(work_dir) / SEGMENT_DIRNAME
    if not segments_dir.is_dir():
        return 0
    removed = 0
    for path in segments_dir.iterdir():
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _cut(
    clip: TimelineClip,
    source: Path,
    destination: Path,
    *,
    runner: FFmpegRunner,
    config: AppConfig,
    encoder: EncoderChoice,
    target: EncodeTarget,
    should_cancel: CancelCheck | None,
) -> None:
    """Extract one clip, re-encoded so the cut lands on the requested frame.

    ``-ss`` before ``-i`` seeks by keyframe and is fast; the re-encode is what
    makes the cut frame-accurate. Copying the stream instead would move every
    in-point to the nearest keyframe -- up to a couple of seconds away, which
    is the difference between a clip that opens on the kill and one that opens
    after it.
    """
    partial = destination.with_suffix(".part.mp4")
    partial.unlink(missing_ok=True)

    argv = [
        *runner.base_arguments(),
        *progress_arguments(),
        "-ss", format_seconds(clip.source_in),
        "-i", str(source),
        "-t", format_seconds(clip.duration),
        # One video stream, no audio: the audio path builds the programme
        # separately, because the mix needs the tracks apart (§72).
        "-map", "0:v:0",
        "-an",
        *_scale_arguments(clip, target),
        *intermediate_arguments(encoder, config.render),
        "-r", str(target.fps),
        # Every segment must start at zero for the concat demuxer, and a
        # source seek leaves the original timestamps in place.
        "-reset_timestamps", "1",
        "-video_track_timescale", "90000",
        str(partial),
    ]

    runner.run_with_progress(
        argv,
        total_seconds=clip.duration,
        should_cancel=should_cancel,
        timeout_seconds=config.ffmpeg.timeout_seconds,
        error_code=ErrorCode.ENCODING_FAILED,
        error_type=RenderError,
        error_message=f"Could not cut clip {clip.clip_index} from {source.name}.",
        details={"clip_id": clip.id, "source_in": clip.source_in},
    )
    partial.replace(destination)


def _scale_arguments(clip: TimelineClip, target: EncodeTarget) -> list[str]:
    """Scale and pad every clip to the output frame.

    Recordings in a project can differ in resolution, and the concat demuxer
    refuses streams that do not match. Padding rather than stretching keeps a
    4:3 capture from being distorted into 16:9; the bars are honest.
    """
    return [
        "-vf",
        (
            f"scale={target.width}:{target.height}:force_original_aspect_ratio=decrease,"
            f"pad={target.width}:{target.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1"
        ),
    ]


def _is_complete(segment: Path, expected_seconds: float, runner: FFmpegRunner) -> bool:
    """Whether an existing segment can be reused (§47).

    Measured rather than trusted. A segment left behind by a killed process is
    a readable file of the wrong length, and reusing it would splice a
    truncated clip into the finished video without a word.
    """
    if not segment.is_file() or segment.stat().st_size == 0:
        return False
    try:
        measured = probe_media(segment, runner).duration_seconds or 0.0
    except (MediaError, RenderError, OSError):
        # An unreadable segment is simply not reusable. Anything else is a bug
        # and should not be swallowed here.
        return False
    return abs(measured - expected_seconds) <= SEGMENT_TOLERANCE_SECONDS


def _concatenate(
    segments: Sequence[Path],
    destination: Path,
    work_dir: Path,
    runner: FFmpegRunner,
    config: AppConfig,
) -> None:
    """Join the segments without re-encoding them."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    partial.unlink(missing_ok=True)

    if len(segments) == 1:
        shutil.copyfile(segments[0], partial)
        partial.replace(destination)
        return

    # Relative names with forward slashes: the concat demuxer resolves them
    # against the list file's directory and treats a backslash as an escape,
    # so a Windows path written literally would not survive.
    listing = work_dir / SEGMENT_LIST_FILENAME
    listing.write_text(
        "".join(f"file '{path.name}'\n" for path in segments), encoding="utf-8"
    )

    with log_duration(logger, "Joined the render segments", segments=len(segments)):
        runner.run(
            [
                *runner.base_arguments(),
                "-f", "concat",
                "-safe", "0",
                "-i", str(listing),
                "-c", "copy",
                str(partial),
            ],
            timeout_seconds=config.ffmpeg.timeout_seconds,
            error_code=ErrorCode.RENDER_FAILED,
            error_type=RenderError,
            error_message="Could not join the rendered clips.",
            details={"segments": len(segments)},
        )
    partial.replace(destination)


__all__ = [
    "SEGMENT_DIRNAME",
    "SEGMENT_LIST_FILENAME",
    "RenderedProgramme",
    "clear_segments",
    "render_programme",
]
