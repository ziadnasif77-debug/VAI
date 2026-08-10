"""Frame sampling and extraction (SPEC sections 16, 15, 56).

§16 defines a hierarchy: a sparse base rate over the whole recording, denser
sampling where something is happening, denser still around a candidate event
with pre-roll and post-roll. The reason is §15 -- the vision model must not see
every frame -- and the consequence is that sampling density is a budget, not a
preference.

That budget is enforced here, and the enforcement runs the right way round. On
a two-hour recording an interval of one frame per second means 7 200 files;
truncating the list at ``max_frames_per_hour`` would analyse the first forty
minutes densely and abandon the rest. So the ceiling **widens the interval**
instead, keeping coverage uniform across the whole source. A plan reports when
that happened, so a stage can log that it sampled more sparsely than asked
rather than quietly doing so.

Planning is pure and testable without FFmpeg; extraction is separate.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final, Literal

from backend.config.schema import FrameSamplingConfig
from backend.core.errors import ErrorCode, MediaError
from backend.core.logging import LogChannel, get_logger
from backend.media.ffmpeg import (
    CancelCheck,
    FFmpegRunner,
    ProgressCallback,
    format_seconds,
    progress_arguments,
)

logger = get_logger("media.frames", LogChannel.PIPELINE)

SamplingLevel = Literal["base", "high_activity", "candidate"]

SECONDS_PER_HOUR: Final[float] = 3600.0

#: Filename pattern for a uniform pass. Zero-padded so lexical order is
#: chronological order, which is what every later stage assumes when it globs.
FRAME_PATTERN: Final[str] = "%06d.jpg"
FRAME_SUFFIX: Final[str] = ".jpg"


@dataclass(frozen=True, slots=True)
class ExtractedFrame:
    """One frame written to disk, with the source timestamp it came from."""

    timestamp: float
    path: Path
    level: SamplingLevel = "base"


@dataclass(frozen=True, slots=True)
class SamplingPlan:
    """Which timestamps to sample, and what the budget did to the request."""

    level: SamplingLevel
    #: Interval actually used, after the ceiling was applied.
    interval_seconds: float
    #: Interval the configuration asked for.
    requested_interval_seconds: float
    timestamps: tuple[float, ...]
    #: How many frames the requested interval would have produced.
    requested_count: int

    @property
    def count(self) -> int:
        return len(self.timestamps)

    @property
    def was_capped(self) -> bool:
        """Whether the frame budget forced a sparser interval than configured."""
        return self.interval_seconds > self.requested_interval_seconds + 1e-9

    def summary(self) -> dict[str, object]:
        """Structured fields for the stage log (§81)."""
        return {
            "level": self.level,
            "frames": self.count,
            "interval_seconds": round(self.interval_seconds, 3),
            "requested_interval_seconds": round(self.requested_interval_seconds, 3),
            "requested_frames": self.requested_count,
            "capped": self.was_capped,
        }


def plan_base_sampling(
    duration_seconds: float,
    config: FrameSamplingConfig,
    *,
    start: float = 0.0,
) -> SamplingPlan:
    """Plan the sparse pass over a whole recording (§16).

    The interval is the configured base rate or the rate the hourly budget
    allows, whichever is sparser. With the shipped defaults -- 3 s base,
    1 800 frames per hour -- the budget permits one frame per 2 s, so the
    configured 3 s wins and the ceiling is slack. Tighten the interval below
    2 s and the ceiling starts binding, uniformly.
    """
    if duration_seconds <= 0:
        return SamplingPlan(
            level="base",
            interval_seconds=config.base_interval_seconds,
            requested_interval_seconds=config.base_interval_seconds,
            timestamps=(),
            requested_count=0,
        )

    requested = config.base_interval_seconds
    budget_interval = SECONDS_PER_HOUR / config.max_frames_per_hour
    interval = max(requested, budget_interval)

    timestamps = _uniform_timestamps(start, start + duration_seconds, interval)
    return SamplingPlan(
        level="base",
        interval_seconds=interval,
        requested_interval_seconds=requested,
        timestamps=timestamps,
        requested_count=_count_for(duration_seconds, requested),
    )


def plan_candidate_sampling(
    start: float,
    end: float,
    config: FrameSamplingConfig,
    *,
    duration_seconds: float | None = None,
    high_activity: bool = False,
) -> SamplingPlan:
    """Plan the dense pass around a candidate region (§16).

    The region is widened by the configured pre-roll and post-roll: an event is
    rarely legible from its own instant, and the seconds leading up to it are
    what make it a moment rather than a spike.

    Args:
        start, end: the candidate region, before roll is applied.
        config: frame sampling configuration.
        duration_seconds: source length, used to clamp the widened region.
        high_activity: use the intermediate interval rather than the densest
            one, for regions merely flagged as busy rather than nominated as
            events.
    """
    level: SamplingLevel = "high_activity" if high_activity else "candidate"
    requested = (
        config.high_activity_interval_seconds
        if high_activity
        else config.candidate_interval_seconds
    )

    window_start = max(start - config.candidate_pre_roll_seconds, 0.0)
    window_end = end + config.candidate_post_roll_seconds
    if duration_seconds is not None:
        window_end = min(window_end, duration_seconds)
    span = max(window_end - window_start, 0.0)
    if span <= 0:
        return SamplingPlan(
            level=level,
            interval_seconds=requested,
            requested_interval_seconds=requested,
            timestamps=(),
            requested_count=0,
        )

    requested_count = _count_for(span, requested)
    # Same rule as the hourly budget: too many frames widens the interval so the
    # whole region stays covered, instead of sampling its opening densely and
    # its resolution not at all.
    interval = requested
    if requested_count > config.max_frames_per_candidate:
        interval = span / config.max_frames_per_candidate

    timestamps = _uniform_timestamps(window_start, window_end, interval)
    if len(timestamps) > config.max_frames_per_candidate:
        timestamps = timestamps[: config.max_frames_per_candidate]
    return SamplingPlan(
        level=level,
        interval_seconds=interval,
        requested_interval_seconds=requested,
        timestamps=timestamps,
        requested_count=requested_count,
    )


def merge_regions(
    regions: Iterable[tuple[float, float]], *, gap_seconds: float = 0.0
) -> list[tuple[float, float]]:
    """Merge overlapping or adjacent candidate regions.

    Two detectors nominating the same explosion must not cause it to be
    extracted twice, and two events four seconds apart share their pre-roll and
    post-roll. Merging before planning is what keeps the frame budget honest.
    """
    ordered = sorted((float(a), float(b)) for a, b in regions if b > a)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start - merged[-1][1] <= gap_seconds:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def extract_uniform(
    source: Path,
    output_dir: Path,
    plan: SamplingPlan,
    runner: FFmpegRunner,
    *,
    jpeg_quality: int = 4,
    duration_seconds: float | None = None,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    timeout_seconds: int | None = None,
) -> list[ExtractedFrame]:
    """Extract evenly spaced frames in a single decode pass.

    One pass, not one process per frame. Seeking to 2 400 timestamps
    individually means 2 400 process starts and 2 400 seeks; the ``fps`` filter
    walks the file once and writes what it passes, which is the difference
    between minutes and an afternoon on a two-hour source -- and it holds one
    frame in memory at a time, never the file (§7).

    Timestamps are computed, not parsed: output frame *n* carries the presentation
    time ``n * interval`` by construction, so the mapping back to source time is
    exact and needs no second probe.
    """
    if not plan.timestamps:
        return []

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    argv = [
        *runner.base_arguments(),
        *runner.input_arguments(Path(source)),
        "-vf",
        f"fps={_fps_expression(plan.interval_seconds)}",
        "-q:v",
        str(jpeg_quality),
        # The hard stop. The filter alone would emit whatever the source length
        # implies; this is the ceiling the plan committed to.
        "-frames:v",
        str(len(plan.timestamps)),
        "-an",
        "-sn",
        *progress_arguments(),
        str(destination / FRAME_PATTERN),
    ]

    runner.run_with_progress(
        argv,
        total_seconds=duration_seconds,
        on_progress=on_progress,
        should_cancel=should_cancel,
        timeout_seconds=timeout_seconds,
        error_code=ErrorCode.FRAME_EXTRACTION_FAILED,
        error_message=f"Frame extraction failed for {Path(source).name}.",
        details={"source": str(source), "output_dir": str(destination)},
    )

    written = sorted(destination.glob(f"*{FRAME_SUFFIX}"))
    if not written:
        raise MediaError(
            f"Frame extraction produced no frames for {Path(source).name}.",
            code=ErrorCode.FRAME_EXTRACTION_FAILED,
            details={"source": str(source), "output_dir": str(destination),
                     "expected": len(plan.timestamps)},
        )

    # FFmpeg stops at the end of the source, so fewer frames than planned is
    # normal on a file slightly shorter than its reported duration. More would
    # mean the pattern collided with an earlier run.
    frames = [
        ExtractedFrame(timestamp=plan.timestamps[index], path=path, level=plan.level)
        for index, path in enumerate(written)
        if index < len(plan.timestamps)
    ]
    logger.info(
        "Extracted frames",
        extra={"source": str(source), "written": len(frames), **plan.summary()},
    )
    return frames


def extract_at_times(
    source: Path,
    output_dir: Path,
    timestamps: Sequence[float],
    runner: FFmpegRunner,
    *,
    level: SamplingLevel = "candidate",
    jpeg_quality: int = 4,
    width: int | None = None,
    prefix: str = "t",
    should_cancel: CancelCheck | None = None,
    timeout_seconds: int = 120,
) -> list[ExtractedFrame]:
    """Extract one frame at each of ``timestamps``.

    A seek per frame, which is the right shape for scattered targets: candidate
    regions (§16), scene keyframes (§17) and preview thumbnails (§56). For dense
    uniform coverage use :func:`extract_uniform` instead -- thousands of seeks
    cost far more than one decode pass.

    Frames already on disk are kept, so a re-run after an interrupted stage
    resumes rather than repeats (§47).
    """
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    scale = ["-vf", f"scale={width}:-2"] if width else []
    frames: list[ExtractedFrame] = []
    for timestamp in timestamps:
        if should_cancel is not None and should_cancel():
            break
        # Zero-padded and dot-free: lexical order stays chronological, and the
        # timestamp is recoverable from the name if the database is ever lost.
        stem = f"{prefix}{timestamp:012.3f}".replace(".", "_")
        path = destination / f"{stem}{FRAME_SUFFIX}"
        if path.exists():
            frames.append(ExtractedFrame(timestamp=timestamp, path=path, level=level))
            continue

        runner.run(
            [
                *runner.base_arguments(),
                *runner.input_arguments(Path(source), start=timestamp),
                "-frames:v",
                "1",
                *scale,
                "-q:v",
                str(jpeg_quality),
                "-an",
                "-sn",
                str(path),
            ],
            timeout_seconds=timeout_seconds,
            error_code=ErrorCode.FRAME_EXTRACTION_FAILED,
            error_message=f"Could not extract a frame at {format_seconds(timestamp)}s.",
            details={"source": str(source), "timestamp": timestamp},
        )
        if path.exists():
            frames.append(ExtractedFrame(timestamp=timestamp, path=path, level=level))
    return frames


def clear_frames(output_dir: Path) -> int:
    """Delete a frame directory and report how many files went with it (§84).

    Frame directories are the largest intermediate a project produces; a stage
    that re-runs must not leave the previous run's frames behind to be globbed
    alongside the new ones.
    """
    destination = Path(output_dir)
    if not destination.exists():
        return 0
    count = sum(1 for entry in destination.rglob(f"*{FRAME_SUFFIX}") if entry.is_file())
    shutil.rmtree(destination, ignore_errors=True)
    return count


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _uniform_timestamps(start: float, end: float, interval: float) -> tuple[float, ...]:
    """Timestamps from ``start``, spaced by ``interval``, strictly below ``end``."""
    if interval <= 0 or end <= start:
        return ()
    count = math.floor((end - start) / interval) + 1
    stamps = [round(start + index * interval, 6) for index in range(count)]
    # `end` is exclusive: there is no frame at the instant a source stops.
    return tuple(stamp for stamp in stamps if stamp < end)


def _count_for(span: float, interval: float) -> int:
    if interval <= 0 or span <= 0:
        return 0
    return math.floor(span / interval) + 1


def _fps_expression(interval_seconds: float) -> str:
    """Render an interval as an exact FFmpeg ``fps`` rational.

    Passed as a ratio of integers rather than a decimal so FFmpeg computes the
    frame times exactly: ``fps=1/3`` lands on 0, 3, 6...; ``fps=0.333333``
    drifts, and over two hours that drift is the difference between a moment's
    timestamp and its neighbour's.
    """
    rate = Fraction(1) / Fraction(interval_seconds).limit_denominator(1_000_000)
    return f"{rate.numerator}/{rate.denominator}"


__all__ = [
    "FRAME_PATTERN",
    "FRAME_SUFFIX",
    "SECONDS_PER_HOUR",
    "ExtractedFrame",
    "SamplingLevel",
    "SamplingPlan",
    "clear_frames",
    "extract_at_times",
    "extract_uniform",
    "merge_regions",
    "plan_base_sampling",
    "plan_candidate_sampling",
]
