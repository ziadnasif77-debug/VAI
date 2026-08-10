"""Proxy generation (SPEC sections 55, 65, 42, 47, 82).

§55: a 720p proxy backs the UI preview and the visual analysis stages, while
the original stays untouched and is what the final render cuts from (§42).

The work is done in segments rather than in one long FFmpeg run, for three
reasons that all come from the same fact -- a two-hour proxy takes minutes:

* **Resume** (§47). A crash, a cancellation or a closed laptop lid at ninety
  minutes must not throw away ninety minutes. Completed segments survive and
  the next run continues from the first one missing.
* **Cancellation** (§82). Segment boundaries are the checkpoints where a
  worker can stop and leave the project consistent.
* **Honest progress** (§60). Progress is measured against the source timeline,
  not guessed from output file size.

Segments are concatenated with stream copy, then the result is probed and its
duration checked against the source. Concatenation that silently drops a
segment would produce a proxy whose timestamps no longer match the recording,
and every moment found in it would point at the wrong part of the video -- so
the check is not optional.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend.config.schema import ProxyConfig
from backend.core.errors import ErrorCode, MediaError
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.media.chunking import plan_chunks
from backend.media.ffmpeg import (
    CancelCheck,
    CancelledError,
    FFmpegRunner,
    ProgressReport,
    progress_arguments,
)
from backend.media.probe import ProbeResult, probe_media

logger = get_logger("media.proxy", LogChannel.PIPELINE)

#: Default length of one proxy segment. Ten minutes bounds what a crash costs
#: while keeping the number of concat inputs small on an eight-hour source.
DEFAULT_SEGMENT_SECONDS: Final[float] = 600.0

#: Directory holding partial segments inside the project's proxy directory.
SEGMENT_DIRNAME: Final[str] = "segments"
SEGMENT_LIST_FILENAME: Final[str] = "segments.txt"

#: How far the assembled proxy may differ from the source before it is rejected.
#: A fixed second of slack for container rounding, plus a tenth per segment for
#: encoder priming at each join.
DURATION_TOLERANCE_BASE_SECONDS: Final[float] = 1.0
DURATION_TOLERANCE_PER_SEGMENT_SECONDS: Final[float] = 0.1


#: Progress callback: ``(fraction_of_whole_job, human_readable_step)``. The
#: message is what the analysis screen shows beside the bar (§60).
ProxyProgress = Callable[[float, str], None]


@dataclass(frozen=True, slots=True)
class ProxyResult:
    """A finished proxy and what it cost to make."""

    path: Path
    duration_seconds: float
    width: int | None
    height: int | None
    fps: float | None
    size_bytes: int
    segment_count: int
    #: Segments already on disk from an earlier interrupted run.
    reused_segments: int

    @property
    def was_resumed(self) -> bool:
        return self.reused_segments > 0


def generate_proxy(
    source: Path,
    destination: Path,
    runner: FFmpegRunner,
    config: ProxyConfig,
    *,
    probe: ProbeResult,
    work_dir: Path | None = None,
    segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
    on_progress: ProxyProgress | None = None,
    should_cancel: CancelCheck | None = None,
    timeout_seconds: int | None = None,
) -> ProxyResult:
    """Transcode ``source`` into a preview/analysis proxy at ``destination``.

    Args:
        source: the original recording. Only ever read (§42).
        destination: where the finished proxy goes. Written atomically.
        runner: the FFmpeg process layer.
        config: the ``proxy`` section of ``config/rendering.yaml``.
        probe: the source's probe result. Supplies the duration segments are
            planned against, and the exact stream indices to map -- guessing
            them is how an embedded cover image becomes the "video track".
        work_dir: where segments live. Defaults to a directory beside the
            destination, so an interrupted run is resumable in place.
        segment_seconds: length of one segment.
        on_progress: called as ``(fraction, message)``.
        should_cancel: polled between and during segments (§82).

    Returns:
        The proxy's own probed properties -- not the requested ones. What the
        encoder produced is what later stages will read.
    """
    source_path = Path(source)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)

    duration = probe.duration_seconds
    if duration <= 0:
        raise MediaError(
            f"Cannot build a proxy for {source_path.name}: its duration is unknown.",
            code=ErrorCode.PROXY_GENERATION_FAILED,
            details={"source": str(source_path)},
            recoverable=False,
        )

    existing = _reuse_existing(target, duration, runner)
    if existing is not None:
        return existing

    segments_dir = Path(work_dir) if work_dir is not None else target.parent / SEGMENT_DIRNAME
    segments_dir.mkdir(parents=True, exist_ok=True)

    plan = plan_chunks(duration, chunk_seconds=segment_seconds, overlap_seconds=0.0)
    filters = _video_filters(probe, config)
    mapping = _stream_mapping(probe)

    encoded: list[Path] = []
    reused = 0
    with log_duration(
        logger,
        "Generated proxy",
        source=str(source_path),
        destination=str(target),
        segments=len(plan),
    ) as fields:
        for chunk in plan:
            if should_cancel is not None and should_cancel():
                raise _cancelled(source_path)

            segment_path = segments_dir / f"{chunk.index:04d}{target.suffix}"
            if _segment_is_complete(segment_path, chunk.core_duration, runner):
                encoded.append(segment_path)
                reused += 1
                _report(on_progress, chunk.core_end / duration,
                        f"Reusing proxy segment {chunk.index + 1}/{len(plan)}")
                continue

            _encode_segment(
                source_path,
                segment_path,
                runner,
                config,
                start=chunk.core_start,
                duration=chunk.core_duration,
                filters=filters,
                mapping=mapping,
                total_duration=duration,
                completed_before=chunk.core_start,
                index=chunk.index,
                count=len(plan),
                on_progress=on_progress,
                should_cancel=should_cancel,
                timeout_seconds=timeout_seconds,
            )
            encoded.append(segment_path)

        _report(on_progress, 0.98, "Assembling proxy")
        _assemble(encoded, target, segments_dir, runner, timeout_seconds=timeout_seconds)

        result_probe = probe_media(target, runner, require_video=True)
        _verify_duration(target, result_probe, duration, len(plan))

        # §84: segments are as large as the proxy itself. Once the proxy is
        # verified they are dead weight.
        shutil.rmtree(segments_dir, ignore_errors=True)

        fields["reused_segments"] = reused
        fields["size_bytes"] = target.stat().st_size
        fields["proxy_duration"] = result_probe.duration_seconds

    _report(on_progress, 1.0, "Proxy ready")
    return ProxyResult(
        path=target,
        duration_seconds=result_probe.duration_seconds,
        width=result_probe.metadata.width,
        height=result_probe.metadata.height,
        fps=result_probe.metadata.fps,
        size_bytes=target.stat().st_size,
        segment_count=len(plan),
        reused_segments=reused,
    )


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------


def _encode_segment(
    source: Path,
    destination: Path,
    runner: FFmpegRunner,
    config: ProxyConfig,
    *,
    start: float,
    duration: float,
    filters: list[str],
    mapping: list[str],
    total_duration: float,
    completed_before: float,
    index: int,
    count: int,
    on_progress: ProxyProgress | None,
    should_cancel: CancelCheck | None,
    timeout_seconds: int | None,
) -> None:
    """Encode one segment to a temporary file, then move it into place.

    The move is what makes resume safe: a segment file exists only once it is
    complete, so a half-written segment from a killed process is never mistaken
    for finished work.
    """
    partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    partial.unlink(missing_ok=True)

    argv = [
        *runner.base_arguments(),
        *runner.input_arguments(source, start=start, duration=duration),
        *mapping,
        *filters,
        "-c:v",
        config.codec,
        "-preset",
        config.preset,
        "-crf",
        str(config.crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        config.audio_codec,
        "-b:a",
        config.audio_bitrate,
        # Every segment starts its own timeline at zero, which is what the
        # concat demuxer expects when it stitches them back together.
        "-reset_timestamps",
        "1",
        *progress_arguments(),
        str(partial),
    ]

    def relay(report: ProgressReport) -> None:
        position = completed_before + min(report.out_time_seconds, duration)
        _report(
            on_progress,
            position / total_duration,
            f"Encoding proxy segment {index + 1}/{count}",
        )

    try:
        runner.run_with_progress(
            argv,
            total_seconds=duration,
            on_progress=relay,
            should_cancel=should_cancel,
            timeout_seconds=timeout_seconds,
            error_code=ErrorCode.PROXY_GENERATION_FAILED,
            error_message=f"Proxy encoding failed for {source.name} segment {index}.",
            details={"source": str(source), "segment": index,
                     "start": start, "duration": duration},
        )
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(destination)


def _assemble(
    segments: list[Path],
    destination: Path,
    work_dir: Path,
    runner: FFmpegRunner,
    *,
    timeout_seconds: int | None,
) -> None:
    """Concatenate the segments into the final proxy without re-encoding."""
    if not segments:
        raise MediaError(
            "Proxy generation produced no segments.",
            code=ErrorCode.PROXY_GENERATION_FAILED,
            details={"destination": str(destination)},
        )

    partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    partial.unlink(missing_ok=True)

    if len(segments) == 1:
        shutil.move(str(segments[0]), str(partial))
        partial.replace(destination)
        return

    # Relative names with forward slashes: the concat demuxer resolves them
    # against the list file's own directory and treats a backslash as an escape
    # character, so a Windows path written literally would not survive.
    listing = work_dir / SEGMENT_LIST_FILENAME
    listing.write_text(
        "".join(f"file '{path.name}'\n" for path in segments), encoding="utf-8"
    )

    try:
        runner.run(
            [
                *runner.base_arguments(),
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(partial),
            ],
            timeout_seconds=timeout_seconds,
            error_code=ErrorCode.PROXY_GENERATION_FAILED,
            error_message=f"Could not assemble the proxy from {len(segments)} segments.",
            details={"destination": str(destination), "segments": len(segments)},
        )
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(destination)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _video_filters(probe: ProbeResult, config: ProxyConfig) -> list[str]:
    """Build the scale and frame-rate filters, from what the source actually is.

    Both are one-directional. A 480p capture is not upscaled to 720p and a 30 fps
    capture is not interpolated to 30 -- doing either would cost encoding time to
    invent detail that is not in the recording.
    """
    stages: list[str] = []

    height = probe.metadata.height
    # -2 keeps the aspect ratio and rounds the width to an even number, which
    # yuv420p requires; the target height is forced even for the same reason.
    target_height = config.resolution - (config.resolution % 2)
    if height is None or height > target_height:
        stages.append(f"scale=-2:{target_height}")

    source_fps = probe.metadata.fps
    if source_fps is None or source_fps > config.fps:
        stages.append(f"fps={config.fps}")

    return ["-vf", ",".join(stages)] if stages else []


def _stream_mapping(probe: ProbeResult) -> list[str]:
    """Map the real video stream and every audio stream, by index.

    Explicit mapping rather than FFmpeg's default stream selection: a recording
    with embedded cover art has two video streams, and the default choice
    between them is not the one anybody wants.
    """
    mapping: list[str] = []
    video = probe.video_tracks
    if video:
        mapping += ["-map", f"0:{video[0].index}"]
    for track in probe.audio_tracks:
        mapping += ["-map", f"0:{track.index}"]
    return mapping


def _segment_is_complete(path: Path, expected_duration: float, runner: FFmpegRunner) -> bool:
    """Whether ``path`` is a finished segment of roughly the expected length.

    Probed rather than merely present: a segment left behind by a killed
    process can be a valid-looking file with half the content.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        result = probe_media(path, runner, require_duration=True)
    except MediaError:
        path.unlink(missing_ok=True)
        return False
    # One second of slack: the last frame of a segment rarely lands exactly on
    # the boundary, and a keyframe-aligned cut can fall marginally short.
    return abs(result.duration_seconds - expected_duration) <= 1.0


def _reuse_existing(
    destination: Path, expected_duration: float, runner: FFmpegRunner
) -> ProxyResult | None:
    """Return the existing proxy if it is complete, so PROXY is idempotent.

    Re-running analysis, or resuming after a crash in a later stage, must not
    re-encode a proxy that is already correct (§47).
    """
    if not destination.is_file() or destination.stat().st_size == 0:
        return None
    try:
        result = probe_media(destination, runner, require_video=True)
    except MediaError:
        return None
    if abs(result.duration_seconds - expected_duration) > max(
        DURATION_TOLERANCE_BASE_SECONDS, expected_duration * 0.01
    ):
        return None

    logger.info(
        "Reusing existing proxy",
        extra={"path": str(destination), "duration": result.duration_seconds},
    )
    return ProxyResult(
        path=destination,
        duration_seconds=result.duration_seconds,
        width=result.metadata.width,
        height=result.metadata.height,
        fps=result.metadata.fps,
        size_bytes=destination.stat().st_size,
        segment_count=0,
        reused_segments=0,
    )


def _verify_duration(
    destination: Path, result: ProbeResult, expected: float, segments: int
) -> None:
    """Reject a proxy whose timeline does not match the source.

    A proxy short by one segment still plays. Every timestamp after the gap
    would be wrong, so every moment derived from it would point somewhere else
    in the recording -- a failure that only surfaces in the finished video.
    """
    tolerance = (
        DURATION_TOLERANCE_BASE_SECONDS + DURATION_TOLERANCE_PER_SEGMENT_SECONDS * segments
    )
    drift = abs(result.duration_seconds - expected)
    if drift > tolerance:
        destination.unlink(missing_ok=True)
        raise MediaError(
            f"The assembled proxy is {drift:.2f}s from the source duration "
            f"(tolerance {tolerance:.2f}s); its timestamps would not match the recording.",
            code=ErrorCode.PROXY_GENERATION_FAILED,
            details={
                "destination": str(destination),
                "expected_seconds": expected,
                "actual_seconds": result.duration_seconds,
                "segments": segments,
            },
        )


def _cancelled(source: Path) -> CancelledError:
    return CancelledError(details={"source": str(source), "stage": "proxy"})


def _report(callback: ProxyProgress | None, fraction: float, message: str) -> None:
    if callback is None:
        return
    callback(min(max(fraction, 0.0), 1.0), message)


__all__ = [
    "DEFAULT_SEGMENT_SECONDS",
    "SEGMENT_DIRNAME",
    "SEGMENT_LIST_FILENAME",
    "ProxyProgress",
    "ProxyResult",
    "generate_proxy",
]
