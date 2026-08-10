"""Audio extraction for analysis (SPEC sections 18, 19, 65, 7).

The analysis stages do not read audio out of the source container. They read a
normalised stream produced here: 16 kHz mono PCM by default
(``config/audio.yaml: analysis``). Every downstream consumer -- Whisper (§14),
the RMS/LUFS/transient detectors (§18), the reaction detector (§20) -- expects
the same rate, layout and sample format, so the conversion happens once instead
of inside each of them.

§19 is why extraction is per track rather than per file: when a recording kept
the player's microphone on its own stream, a scream and an explosion must be
analysed as two signals. Mixing them down first destroys exactly the
distinction the moment detector depends on.

Extraction streams: FFmpeg decodes and writes progressively, so an eight-hour
recording costs one buffer, not eight hours of PCM in RAM (§7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend.config.schema import AnalysisAudioConfig
from backend.core.errors import ErrorCode, MediaError
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.media.ffmpeg import (
    CancelCheck,
    FFmpegRunner,
    ProgressReport,
    progress_arguments,
)
from backend.media.probe import ProbeResult, TrackInfo, probe_media

logger = get_logger("media.audio", LogChannel.PIPELINE)

#: Filename of the primary analysis stream inside ``projects/<id>/audio/``.
PRIMARY_FILENAME_STEM: Final[str] = "analysis"

#: WAV stores its payload size in 32 bits, so a stream cannot exceed 4 GiB. At
#: the default 16 kHz mono 16-bit that is about 37 hours -- far beyond the 8 h
#: §7 requires, but the ceiling is real and is checked rather than discovered
#: when a file silently truncates.
WAV_MAX_BYTES: Final[int] = 4 * 1024**3

AudioProgress = Callable[[float, str], None]


@dataclass(frozen=True, slots=True)
class AudioStream:
    """One extracted analysis stream."""

    path: Path
    #: Index of the source stream it came from, so a result can be traced back
    #: to the microphone rather than the game mix (§19).
    source_track_index: int | None
    duration_seconds: float
    sample_rate: int
    channels: int
    size_bytes: int
    #: True for the stream the single-track analysers should use by default.
    is_primary: bool = False


def extract_analysis_audio(
    source: Path,
    destination: Path,
    runner: FFmpegRunner,
    config: AnalysisAudioConfig,
    *,
    track_index: int | None = None,
    duration_seconds: float | None = None,
    is_primary: bool = False,
    on_progress: AudioProgress | None = None,
    should_cancel: CancelCheck | None = None,
    timeout_seconds: int | None = None,
) -> AudioStream:
    """Extract one audio stream from ``source`` in the analysis format.

    Args:
        source: the recording. Only read (§42).
        destination: where the stream is written. Produced atomically, so an
            interrupted run never leaves a truncated WAV that later stages
            would read as a shorter recording.
        config: the ``analysis`` section of ``config/audio.yaml``.
        track_index: absolute stream index to extract. ``None`` takes FFmpeg's
            default audio stream.
        duration_seconds: source length, for progress reporting.
        is_primary: mark this as the stream single-track analysers default to.

    Raises:
        MediaError: extraction failed, or produced nothing.
    """
    source_path = Path(source)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)

    _check_wav_capacity(target, duration_seconds, config)

    partial = target.with_name(f"{target.stem}.part{target.suffix}")
    partial.unlink(missing_ok=True)

    mapping = ["-map", f"0:{track_index}"] if track_index is not None else ["-map", "0:a:0"]
    argv = [
        *runner.base_arguments(),
        *runner.input_arguments(source_path),
        *mapping,
        # -vn matters on a gameplay recording: without it FFmpeg would decode
        # every video frame it walks past to produce audio nobody asked it to
        # look at.
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        str(config.channels),
        "-ar",
        str(config.sample_rate),
        "-c:a",
        config.codec,
        "-f",
        config.format,
        *progress_arguments(),
        str(partial),
    ]

    def relay(report: ProgressReport) -> None:
        if on_progress is not None:
            fraction = report.fraction if report.fraction is not None else 0.0
            on_progress(fraction, "Extracting audio")

    with log_duration(
        logger,
        "Extracted analysis audio",
        source=str(source_path),
        destination=str(target),
        track_index=track_index,
    ) as fields:
        try:
            runner.run_with_progress(
                argv,
                total_seconds=duration_seconds,
                on_progress=relay,
                should_cancel=should_cancel,
                timeout_seconds=timeout_seconds,
                error_code=ErrorCode.AUDIO_EXTRACTION_FAILED,
                error_message=f"Audio extraction failed for {source_path.name}.",
                details={"source": str(source_path), "track_index": track_index},
            )
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

        if not partial.is_file() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise MediaError(
                f"Audio extraction produced no data for {source_path.name}.",
                code=ErrorCode.AUDIO_EXTRACTION_FAILED,
                details={"source": str(source_path), "track_index": track_index},
            )
        partial.replace(target)

        result = probe_media(target, runner)
        fields["duration"] = result.duration_seconds
        fields["size_bytes"] = target.stat().st_size

    # Report what the file actually is, not what was requested: if FFmpeg
    # resampled differently the analysers need to know the real rate.
    written = result.audio_tracks[0] if result.audio_tracks else None
    return AudioStream(
        path=target,
        source_track_index=track_index,
        duration_seconds=result.duration_seconds,
        sample_rate=(written.sample_rate if written else None) or config.sample_rate,
        channels=(written.channels if written else None) or config.channels,
        size_bytes=target.stat().st_size,
        is_primary=is_primary,
    )


def extract_all_tracks(
    source: Path,
    output_dir: Path,
    runner: FFmpegRunner,
    config: AnalysisAudioConfig,
    *,
    probe: ProbeResult,
    on_progress: AudioProgress | None = None,
    should_cancel: CancelCheck | None = None,
    timeout_seconds: int | None = None,
) -> list[AudioStream]:
    """Extract every audio stream of ``source`` as its own analysis file (§19).

    The first stream becomes the primary. A recording with a second stream gets
    it extracted alongside rather than mixed in, because that second stream is
    usually the microphone, and §19 requires the player's voice to be analysed
    independently of the game.

    Returns an empty list for a source with no audio -- silent gameplay is a
    real recording, not an error.
    """
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    tracks = probe.audio_tracks
    if not tracks:
        logger.warning(
            "Source has no audio track",
            extra={"source": str(source), "path": str(source)},
        )
        return []

    streams: list[AudioStream] = []
    for position, track in enumerate(tracks):
        if should_cancel is not None and should_cancel():
            break
        is_primary = position == 0
        target = destination / f"{_stem_for(track, is_primary=is_primary)}.{config.format}"
        streams.append(
            extract_analysis_audio(
                source,
                target,
                runner,
                config,
                track_index=track.index,
                duration_seconds=probe.duration_seconds,
                is_primary=is_primary,
                on_progress=_scaled(on_progress, position, len(tracks)),
                should_cancel=should_cancel,
                timeout_seconds=timeout_seconds,
            )
        )
    return streams


def _stem_for(track: TrackInfo, *, is_primary: bool) -> str:
    """Name a track's output file.

    The primary keeps a fixed name so consumers can find it without a database
    lookup; the rest carry their source stream index, which is what makes a
    result traceable to the microphone.
    """
    if is_primary:
        return PRIMARY_FILENAME_STEM
    suffix = f"_{track.language}" if track.language else ""
    return f"track{track.index:02d}{suffix}"


def _scaled(
    callback: AudioProgress | None, position: int, total: int
) -> AudioProgress | None:
    """Map one track's progress onto its share of the whole stage."""
    if callback is None or total <= 0:
        return None

    def relay(fraction: float, message: str) -> None:
        callback((position + min(max(fraction, 0.0), 1.0)) / total, message)

    return relay


def _check_wav_capacity(
    target: Path, duration_seconds: float | None, config: AnalysisAudioConfig
) -> None:
    """Refuse a WAV that would exceed the format's 4 GiB payload limit."""
    if config.format != "wav" or not duration_seconds:
        return
    bytes_per_second = config.sample_rate * config.channels * 2  # pcm_s16le
    if duration_seconds * bytes_per_second > WAV_MAX_BYTES:
        raise MediaError(
            "The analysis stream would exceed the 4 GiB WAV limit; lower "
            "audio.analysis.sample_rate or use a different container.",
            code=ErrorCode.AUDIO_EXTRACTION_FAILED,
            details={
                "destination": str(target),
                "duration_seconds": duration_seconds,
                "sample_rate": config.sample_rate,
                "channels": config.channels,
            },
            recoverable=False,
        )


__all__ = [
    "PRIMARY_FILENAME_STEM",
    "WAV_MAX_BYTES",
    "AudioProgress",
    "AudioStream",
    "extract_all_tracks",
    "extract_analysis_audio",
]
