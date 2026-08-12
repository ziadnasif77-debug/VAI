"""Technical QA of a finished render (SPEC §76).

Everything here is measured from the **file**, never from what the render said
it produced. That distinction is the whole point of the phase: a stage that
reports success having written a black, silent, or half-length video is exactly
the failure QA exists to catch, and asking it whether it succeeded would find
nothing.

Two passes over the video, and no more. Decoding a twenty-minute file is not
free, so the detectors that can share a pass do:

1. **Probe** — duration, resolution, frame rate, which streams exist. Cheap,
   and enough to fail early on a file that is obviously wrong.
2. **Decode** — `blackdetect`, `freezedetect`, `silencedetect` and `ebur128`
   in one filter graph, output discarded. This is also the check that the file
   decodes at all, which is what "opens in standard players" means in practice.

A/V sync is checked from stream start times rather than by correlating audio
against video: the desync this pipeline can actually produce comes from a
mistimed mux, and a cross-correlation over twenty minutes would cost more than
the render did.

One check reads a second file. A frozen picture in the render means one of two
opposite things -- the render broke, or the footage was a menu -- and the
render alone cannot tell them apart. So `frozen_frames` maps each freeze back
through the timeline and asks the *recording* whether it was holding still
there too. That is still a measurement of files rather than of anyone's report
of them; it is just that answering "did the render invent this" takes both.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend.config.schema import QaConfig
from backend.core.errors import GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.media.ffmpeg import FFmpegRunner
from backend.media.probe import ProbeResult, probe_media
from backend.qa.report import Finding, enabled_checks, failure, passed, warning

logger = get_logger("qa.technical", LogChannel.QA)

#: Checks this module knows how to run, in the order they are reported.
CHECKS: Final[tuple[str, ...]] = (
    "file_exists",
    "decodes",
    "video_stream",
    "audio_stream",
    "duration",
    "resolution",
    "fps",
    "black_frames",
    "frozen_frames",
    "av_sync",
    "loudness",
)

_BLACK_RE = re.compile(r"black_start:(?P<start>[\d.]+) black_end:(?P<end>[\d.]+)")
_FREEZE_START_RE = re.compile(r"freeze_start: (?P<start>[\d.]+)")
_FREEZE_DURATION_RE = re.compile(r"freeze_duration: (?P<duration>[\d.]+)")
_LOUDNESS_RE = re.compile(r"I:\s*(?P<lufs>-?[\d.]+) LUFS")

#: Seconds of recording read either side of a freeze when checking the source.
#: A freeze is bounded by the render's own timing; the stillness that explains
#: it usually starts a moment earlier and ends a moment later, and the seek is
#: by index so the extra costs nothing.
_SOURCE_PAD_SECONDS: Final[float] = 1.0


@dataclass(frozen=True, slots=True)
class Expected:
    """What the render was asked to produce, for comparison against what it did."""

    duration_seconds: float
    width: int
    height: int
    fps: float


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """One clip of the render, and the recording it was cut from (§42, §80).

    Enough to turn "the picture stopped at 4:12 of the video" into "was the
    recording holding still at 8:03 of that .mkv", which is the difference
    between a broken render and a menu screen someone chose to keep.

    A flat projection of the timeline rather than the timeline itself: QA needs
    the mapping, not the edit, and the caller already has both.
    """

    path: Path
    timeline_start: float
    timeline_end: float
    source_in: float
    #: 1.0 is real time; slow motion stretches the timeline span, not the source.
    speed: float = 1.0

    def window(self, start: float, end: float) -> tuple[float, float] | None:
        """The source window behind a stretch of the render, if this clip covers it."""
        first = max(start, self.timeline_start)
        last = min(end, self.timeline_end)
        if last <= first:
            return None
        return (
            self.source_in + (first - self.timeline_start) * self.speed,
            (last - first) * self.speed,
        )


@dataclass(frozen=True, slots=True)
class DecodeMeasurements:
    """What one decoding pass observed."""

    decoded: bool
    error: str = ""
    black_runs: tuple[tuple[float, float], ...] = ()
    freeze_runs: tuple[tuple[float, float], ...] = ()
    integrated_lufs: float | None = None

    @property
    def longest_black(self) -> float:
        return max((end - start for start, end in self.black_runs), default=0.0)

    @property
    def longest_freeze(self) -> float:
        return max((duration for _, duration in self.freeze_runs), default=0.0)


def inspect(
    path: Path,
    expected: Expected,
    *,
    runner: FFmpegRunner,
    config: QaConfig,
    sources: Sequence[SourceSpan] = (),
) -> list[Finding]:
    """Run every enabled technical check against a rendered file (§76).

    ``sources`` is where each second of the render came from. Given it, a
    frozen picture can be checked against the recording and a static menu told
    apart from a stalled render. Without it every freeze is unexplained, which
    is the safe reading but a blunter one.
    """
    checks = set(enabled_checks(CHECKS, config.technical.checks))
    findings: list[Finding] = []

    if "file_exists" in checks:
        if not path.is_file() or path.stat().st_size == 0:
            return [
                failure(
                    "file_exists",
                    f"there is no rendered file at {path}",
                    remedy="Re-run the render stage; nothing downstream can proceed.",
                    path=str(path),
                )
            ]
        findings.append(
            passed("file_exists", f"{path.stat().st_size / 1e6:.1f} MB", path=str(path))
        )

    try:
        probe = probe_media(path, runner)
    except GamingEditorError as error:
        return [
            *findings,
            failure(
                "decodes",
                f"the file cannot be probed: {error}",
                remedy="The render is corrupt. Re-run it; if it fails again, check the encoder.",
                path=str(path),
            ),
        ]

    findings.extend(_stream_findings(probe, checks))
    findings.extend(_format_findings(probe, expected, config, checks))

    if _needs_decode(checks):
        measurements = decode(path, runner, config, total_seconds=probe.duration_seconds)
        findings.extend(
            _decode_findings(measurements, config, checks, runner=runner, sources=sources)
        )

    if "av_sync" in checks:
        findings.append(_av_sync(probe, config))

    logger.info(
        "Technical QA complete",
        extra={
            "path": str(path),
            "checks": len(findings),
            "failures": sum(1 for item in findings if item.failed),
        },
    )
    return findings


def decode(
    path: Path,
    runner: FFmpegRunner,
    config: QaConfig,
    *,
    total_seconds: float = 0.0,
) -> DecodeMeasurements:
    """Decode the whole file once, collecting every detector's output (§76).

    One pass, because decoding is the expensive part and the detectors are
    nearly free next to it. The output goes to the null muxer: this is about
    what the file contains, not about producing another one.
    """
    thresholds = config.technical.thresholds
    graph = (
        f"[0:v]blackdetect=d=0.5:pix_th=0.10[bd];"
        f"[bd]freezedetect=n={_noise(config)}:"
        f"d={max(0.5, thresholds.max_frozen_run_seconds):.2f}[v]"
    )
    argv = [
        *runner.base_arguments(loglevel="info"),
        "-i",
        str(path),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-f",
        "null",
        "-",
    ]
    if _has_audio(path, runner):
        argv = [
            *runner.base_arguments(loglevel="info"),
            "-i",
            str(path),
            "-filter_complex",
            graph + ";[0:a]ebur128=peak=true[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-f",
            "null",
            "-",
        ]

    result = runner.run(argv, check=False)
    if not result.ok:
        return DecodeMeasurements(decoded=False, error=_first_error(result.stderr))

    return DecodeMeasurements(
        decoded=True,
        black_runs=_parse_black(result.stderr),
        freeze_runs=_parse_freeze(result.stderr, total_seconds),
        integrated_lufs=_parse_loudness(result.stderr),
    )


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


def _stream_findings(probe: ProbeResult, checks: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    if "video_stream" in checks:
        if not probe.video_tracks:
            findings.append(
                failure(
                    "video_stream",
                    "the render has no video stream",
                    remedy="Check the render stage's filter graph and stream mapping.",
                )
            )
        else:
            findings.append(passed("video_stream", f"{len(probe.video_tracks)} video stream(s)"))

    if "audio_stream" in checks:
        if not probe.audio_tracks:
            findings.append(
                failure(
                    "audio_stream",
                    "the render has no audio stream",
                    remedy=(
                        "The audio mix produced nothing. Check that the programme "
                        "audio was assembled and mapped."
                    ),
                )
            )
        else:
            findings.append(passed("audio_stream", f"{len(probe.audio_tracks)} audio stream(s)"))
    return findings


def _format_findings(
    probe: ProbeResult, expected: Expected, config: QaConfig, checks: set[str]
) -> list[Finding]:
    thresholds = config.technical.thresholds
    metadata = probe.metadata
    findings: list[Finding] = []

    if "duration" in checks:
        actual = probe.duration_seconds
        drift = abs(actual - expected.duration_seconds)
        if drift > thresholds.duration_tolerance_seconds:
            findings.append(
                failure(
                    "duration",
                    f"the render is {actual:.1f}s but the edit is "
                    f"{expected.duration_seconds:.1f}s ({drift:.1f}s out)",
                    remedy=(
                        "A clip was dropped or truncated. Compare the timeline's "
                        "clips against the render's segments."
                    ),
                    actual=round(actual, 3),
                    expected=round(expected.duration_seconds, 3),
                )
            )
        else:
            findings.append(
                passed("duration", f"{actual / 60:.1f} min", drift_seconds=round(drift, 3))
            )

    if "resolution" in checks:
        if (metadata.width, metadata.height) != (expected.width, expected.height):
            findings.append(
                failure(
                    "resolution",
                    f"the render is {metadata.width}x{metadata.height}, "
                    f"not {expected.width}x{expected.height}",
                    remedy="Check the encode target and the scale filter.",
                    actual=f"{metadata.width}x{metadata.height}",
                )
            )
        else:
            findings.append(passed("resolution", f"{metadata.width}x{metadata.height}"))

    if "fps" in checks:
        actual_fps = metadata.fps or 0.0
        if abs(actual_fps - expected.fps) > thresholds.fps_tolerance:
            findings.append(
                failure(
                    "fps",
                    f"the render is {actual_fps:.3f} fps, not {expected.fps:.3f}",
                    remedy="Check the encoder's -r flag against the target.",
                    actual=actual_fps,
                )
            )
        else:
            findings.append(passed("fps", f"{actual_fps:.3f} fps"))
    return findings


def _decode_findings(
    measurements: DecodeMeasurements,
    config: QaConfig,
    checks: set[str],
    *,
    runner: FFmpegRunner,
    sources: Sequence[SourceSpan],
) -> list[Finding]:
    thresholds = config.technical.thresholds
    findings: list[Finding] = []

    if "decodes" in checks:
        if not measurements.decoded:
            return [
                failure(
                    "decodes",
                    f"the file does not decode cleanly: {measurements.error}",
                    remedy="The render is corrupt. Re-run it and keep the FFmpeg log.",
                )
            ]
        findings.append(passed("decodes", "decoded end to end without error"))

    if "black_frames" in checks:
        longest = measurements.longest_black
        if longest > thresholds.max_black_run_seconds:
            findings.append(
                failure(
                    "black_frames",
                    f"{longest:.1f}s of black video, the longest of "
                    f"{len(measurements.black_runs)} run(s)",
                    remedy=(
                        "A gap in the timeline or a clip that reads past the end of "
                        "its recording. Validate the timeline before re-rendering."
                    ),
                    longest_seconds=round(longest, 2),
                    runs=len(measurements.black_runs),
                )
            )
        else:
            findings.append(passed("black_frames", f"longest black run {longest:.2f}s"))

    if "frozen_frames" in checks:
        findings.append(_frozen_frames(measurements, config, runner, sources))

    if "loudness" in checks and measurements.integrated_lufs is not None:
        lufs = measurements.integrated_lufs
        if lufs < thresholds.min_audio_lufs:
            findings.append(
                warning(
                    "loudness",
                    f"the programme measures {lufs:.1f} LUFS, below "
                    f"{thresholds.min_audio_lufs:.1f}",
                    remedy=(
                        "The mix is very quiet. Check that the gameplay track "
                        "reached the mix and that loudnorm ran."
                    ),
                    category="technical",
                    lufs=round(lufs, 2),
                )
            )
        else:
            findings.append(passed("loudness", f"{lufs:.1f} LUFS"))
    return findings


def _frozen_frames(
    measurements: DecodeMeasurements,
    config: QaConfig,
    runner: FFmpegRunner,
    sources: Sequence[SourceSpan],
) -> Finding:
    """Did the picture stop, and if so did the *recording* stop too? (§76)

    A frozen render is two different events wearing one face. If the recording
    was holding still -- a menu, a loading screen, a paused game -- the render
    is faithful and the video plays; that is a judgement about the edit, and
    §77 gives judgements to a human. If the recording was moving, the render
    invented the freeze: a bad seek, a corrupt segment, a swallowed decode
    error. Only that is a fact about the file, and only that blocks export.

    Asking the source is also what makes the answer independent of the encoder.
    Both files are measured at the same noise floor, so what is compared is two
    pictures rather than two quantisers.
    """
    limit = config.technical.thresholds.max_frozen_run_seconds
    longest = measurements.longest_freeze
    if longest <= limit:
        return passed("frozen_frames", f"longest freeze {longest:.2f}s")

    over = [run for run in measurements.freeze_runs if run[1] > limit]
    moving: list[tuple[float, float]] = []
    unverified: list[tuple[float, float]] = []
    explained: list[tuple[float, float]] = []
    for start, seconds in over:
        held = _source_stillness(start, seconds, runner, config, sources)
        if held is None:
            unverified.append((start, seconds))
        elif held > limit:
            explained.append((start, seconds))
        else:
            moving.append((start, seconds))

    if moving:
        start, seconds = max(moving, key=lambda run: run[1])
        return failure(
            "frozen_frames",
            f"{seconds:.1f}s of frozen picture at {start:.1f}s, which the "
            f"recording does not account for",
            remedy=(
                "The recording is moving there, so the render stopped on its own: "
                "a bad seek, or a corrupt segment. Check the clip covering that "
                "timestamp and re-render."
            ),
            longest_seconds=round(seconds, 2),
            at_seconds=round(start, 2),
            runs=len(moving),
        )

    if unverified:
        # Nothing was read back, so nothing is claimed about the recording.
        start, seconds = max(unverified, key=lambda run: run[1])
        return failure(
            "frozen_frames",
            f"{seconds:.1f}s of frozen picture at {start:.1f}s",
            remedy=(
                "The source may be corrupt at that timestamp, or a segment was "
                "encoded from a bad seek. Check the clip covering it."
            ),
            longest_seconds=round(seconds, 2),
            at_seconds=round(start, 2),
            runs=len(unverified),
        )

    start, seconds = max(explained, key=lambda run: run[1])
    return warning(
        "frozen_frames",
        f"{seconds:.1f}s of the edit does not move, starting at {start:.1f}s -- "
        f"the recording is still there too",
        remedy=(
            "The render is faithful, so this is an editing call: a menu, a "
            "loading screen or a pause survived into the video. Trim that clip "
            "if it was not deliberate."
        ),
        category="content",
        longest_seconds=round(seconds, 2),
        at_seconds=round(start, 2),
        runs=len(explained),
    )


def _source_stillness(
    start: float,
    seconds: float,
    runner: FFmpegRunner,
    config: QaConfig,
    sources: Sequence[SourceSpan],
) -> float:
    """How long the recording behind this stretch of render holds still.

    Zero when there is no timeline to map through, which reads as "nothing
    accounts for this freeze" -- the safe answer, and the one that keeps a
    bare file honest.
    """
    longest = 0.0
    for span in sources:
        window = span.window(start, start + seconds)
        if window is None:
            continue
        source_start, source_seconds = window
        longest = max(longest, _freeze_in(span.path, source_start, source_seconds, runner, config))
    return longest


def _freeze_in(
    path: Path,
    start: float,
    seconds: float,
    runner: FFmpegRunner,
    config: QaConfig,
) -> float:
    """The longest still run in one window of one recording.

    Seeks by index rather than decoding from the start, so this costs a few
    seconds of decode however long the recording is.
    """
    if not path.is_file():
        return 0.0
    padded = seconds + 2 * _SOURCE_PAD_SECONDS
    argv = [
        *runner.base_arguments(loglevel="info"),
        *runner.input_arguments(path, start=max(0.0, start - _SOURCE_PAD_SECONDS), duration=padded),
        "-an",
        "-vf",
        f"freezedetect=n={_noise(config)}:d=0.5",
        "-f",
        "null",
        "-",
    ]
    result = runner.run(argv, check=False)
    if not result.ok:
        # An unreadable window is not evidence that the footage was moving, but
        # it is not evidence that it was still either. Nothing is claimed.
        logger.warning(
            "Could not read the source behind a frozen stretch",
            extra={"path": str(path), "start": round(start, 2)},
        )
        return 0.0
    runs = _parse_freeze(result.stderr, padded)
    return max((duration for _, duration in runs), default=0.0)


def _noise(config: QaConfig) -> str:
    """``freezedetect``'s noise floor, as the filter spells it."""
    return f"{config.technical.thresholds.freeze_noise_db:g}dB"


def _av_sync(probe: ProbeResult, config: QaConfig) -> Finding:
    """Compare stream start times (§76).

    The desync this pipeline can produce is a mux-level offset -- a stream that
    begins a fraction of a second after the other -- not a drift that
    accumulates. Comparing start times finds that; correlating waveforms over
    twenty minutes would cost more than the render and find the same thing.
    """
    limit_ms = config.technical.thresholds.max_av_desync_ms
    video = probe.video_tracks[0] if probe.video_tracks else None
    audio = probe.audio_tracks[0] if probe.audio_tracks else None
    if video is None or audio is None:
        return passed("av_sync", "only one stream; nothing to compare")

    offset_ms = abs((video.start_seconds or 0.0) - (audio.start_seconds or 0.0)) * 1000
    if offset_ms > limit_ms:
        return failure(
            "av_sync",
            f"audio starts {offset_ms:.0f} ms from the video (limit {limit_ms} ms)",
            remedy="Check the mux: the audio and video streams begin at different times.",
            offset_ms=round(offset_ms, 1),
        )
    return passed("av_sync", f"{offset_ms:.0f} ms offset")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _needs_decode(checks: set[str]) -> bool:
    return bool(checks & {"decodes", "black_frames", "frozen_frames", "loudness"})


def _has_audio(path: Path, runner: FFmpegRunner) -> bool:
    result = runner.run(
        [
            runner.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
    )
    return bool(result.stdout.strip())


def _parse_black(stderr: str) -> tuple[tuple[float, float], ...]:
    return tuple(
        (float(match.group("start")), float(match.group("end")))
        for match in _BLACK_RE.finditer(stderr)
    )


def _parse_freeze(stderr: str, total_seconds: float) -> tuple[tuple[float, float], ...]:
    """Pair each ``freeze_start`` with the ``freeze_duration`` that follows it.

    A freeze still running when the file ends prints a start and **no
    duration**, so it must be closed against the file's length. Zipping the two
    lists instead drops it -- and the freeze that reaches the end of the video
    is the entire-video-frozen case, which is the one that most needs catching.
    """
    runs: list[tuple[float, float]] = []
    pending: float | None = None
    for line in stderr.splitlines():
        start = _FREEZE_START_RE.search(line)
        if start is not None:
            pending = float(start.group("start"))
            continue
        duration = _FREEZE_DURATION_RE.search(line)
        if duration is not None and pending is not None:
            runs.append((pending, float(duration.group("duration"))))
            pending = None
    if pending is not None and total_seconds > pending:
        runs.append((pending, total_seconds - pending))
    return _join_runs(runs)


#: A still stretch broken by a frame or two of movement is one still stretch,
#: not two. Wider than a frame at any sane rate, narrower than anything a
#: viewer would call motion.
_FREEZE_JOIN_SECONDS: Final[float] = 0.1


def _join_runs(runs: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Merge still runs that ``freezedetect`` reported back to back.

    The filter closes a run the instant one frame differs and opens the next at
    the same timestamp, so a dialogue scene arrives as several sub-second runs
    rather than one long one. That is a measurement artefact, and an asymmetric
    one: after an encoder has quantised near-identical frames into identical
    ones the same stillness comes back as a single unbroken run.

    Leaving it unmerged let a faithful render of a cutscene be **blocked**. A
    9:58 edit froze for 3.2s at 578.1s; the recording behind it was still for
    4.95 of the 5.2 seconds either side -- but as runs of 2.17s and 2.78s,
    adjacent, and the longest of those sat under the 3.0s limit. So the check
    concluded the recording was moving and called a real dialogue scene a bad
    seek. Merging happens here rather than at either call site precisely
    because both sides must measure the same way for the comparison to mean
    anything (§76).
    """
    merged: list[list[float]] = []
    for start, duration in sorted(runs):
        if merged and start - (merged[-1][0] + merged[-1][1]) <= _FREEZE_JOIN_SECONDS:
            end = max(merged[-1][0] + merged[-1][1], start + duration)
            merged[-1][1] = end - merged[-1][0]
        else:
            merged.append([start, duration])
    return tuple((start, duration) for start, duration in merged)


def _parse_loudness(stderr: str) -> float | None:
    """The last integrated-loudness figure ebur128 printed: its final summary."""
    matches = _LOUDNESS_RE.findall(stderr)
    return float(matches[-1]) if matches else None


def _first_error(stderr: str) -> str:
    for line in stderr.splitlines():
        text = line.strip()
        if text and not text.startswith("frame=") and "Error" in text:
            return text[:200]
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


def measured_expectation(duration_seconds: float, width: int, height: int, fps: float) -> Expected:
    """Convenience for callers that already know the target."""
    return Expected(duration_seconds=duration_seconds, width=width, height=height, fps=fps)


def check_names() -> Sequence[str]:
    return CHECKS


__all__ = [
    "CHECKS",
    "DecodeMeasurements",
    "Expected",
    "SourceSpan",
    "check_names",
    "decode",
    "inspect",
    "measured_expectation",
]
