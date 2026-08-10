"""Windowed audio features (SPEC sections 18, 7).

The detectors in :mod:`backend.analysis.audio_events` do not read WAV files.
They read the feature frames produced here: one row per analysis window, with
level, dynamics and spectral columns. Separating the two means the detection
rules can be tested against synthetic feature arrays, without a file, a codec
or FFmpeg anywhere near them.

The reader streams. An eight-hour analysis stream is 460 MB of PCM at the
configured 16 kHz mono, and §7 forbids holding it -- so the file is walked one
hop at a time and only a single window is resident. What remains is the feature
table, which is small by construction: at a 0.25 s hop, eight hours is 115 200
rows of a handful of floats.

No new dependency. The analysis stream is 16-bit PCM by configuration, which
the standard library's :mod:`wave` module reads directly, and NumPy does the
arithmetic.
"""

from __future__ import annotations

import math
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np

from backend.core.errors import AnalysisError, ErrorCode
from backend.core.logging import LogChannel, get_logger

logger = get_logger("analysis.signal", LogChannel.PIPELINE)

#: Floor for decibel conversion. -120 dBFS is far below anything audible and
#: keeps log10 away from zero without distorting real quiet passages.
SILENCE_FLOOR_DB: Final[float] = -120.0
_EPSILON: Final[float] = 1e-10

#: Sample formats the analysis stream may use. Configuration produces
#: ``pcm_s16le``; the others are accepted so a hand-made file still analyses.
_SUPPORTED_SAMPLE_WIDTHS: Final[dict[int, str]] = {1: "uint8", 2: "int16", 4: "int32"}


@dataclass(frozen=True, slots=True)
class AudioWindow:
    """One analysis window of samples."""

    index: int
    start: float
    end: float
    samples: np.ndarray

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class AudioFeatures:
    """The feature table for one audio stream.

    Columns are parallel arrays rather than a list of objects: the detectors
    work on whole signals -- rolling baselines, thresholds, run detection --
    and vectorised comparisons over 115 200 rows are both faster and clearer
    than loops.
    """

    path: Path
    sample_rate: int
    duration_seconds: float
    window_seconds: float
    hop_seconds: float

    #: Start time of each window, in seconds.
    times: np.ndarray
    #: Root-mean-square level, dBFS.
    rms_db: np.ndarray
    #: Highest absolute sample in the window, dBFS.
    peak_db: np.ndarray
    #: Zero-crossing rate. High for noise and fricatives, low for tones.
    zero_crossing_rate: np.ndarray
    #: Spectral centroid in Hz -- the "brightness" of the window.
    spectral_centroid: np.ndarray
    #: Positive spectral change against the previous window. This is what makes
    #: a transient a transient: an explosion is a spectral discontinuity, not
    #: merely a loud moment.
    spectral_flux: np.ndarray

    def __len__(self) -> int:
        return int(self.times.size)

    @property
    def is_empty(self) -> bool:
        return self.times.size == 0

    def time_of(self, index: int) -> float:
        """Start time of window ``index``."""
        return float(self.times[index])

    def window_span(self, first: int, last: int) -> tuple[float, float]:
        """Source time span covered by windows ``first`` through ``last``."""
        start = float(self.times[first])
        end = min(float(self.times[last]) + self.window_seconds, self.duration_seconds)
        return start, max(end, start)

    def summary(self) -> dict[str, object]:
        """Structured fields for the stage log (§81)."""
        if self.is_empty:
            return {"windows": 0, "duration_seconds": self.duration_seconds}
        return {
            "windows": len(self),
            "duration_seconds": round(self.duration_seconds, 3),
            "median_rms_db": round(float(np.median(self.rms_db)), 2),
            "peak_db": round(float(np.max(self.peak_db)), 2),
        }


def read_windows(
    path: Path,
    *,
    window_seconds: float,
    hop_seconds: float,
    start: float = 0.0,
    end: float | None = None,
) -> Iterator[AudioWindow]:
    """Walk a mono PCM WAV one window at a time.

    Windows overlap when the hop is shorter than the window, which is the point:
    a 0.25 s hop over a 0.5 s window gives a level curve that reacts inside a
    quarter second without the variance of a quarter-second measurement.

    Args:
        start, end: restrict the walk to a span of the file, seeking rather than
            reading up to it. Reaction analysis (§20) needs a much finer hop
            than the whole-stream pass, and it only needs it over the few
            seconds a candidate occupies -- running the fine pass over eight
            hours to look at four seconds of it would be absurd.

    Timestamps are absolute in every case: a window's ``start`` is its position
    in the stream, not its offset within the requested span.

    Raises:
        AnalysisError: the file is missing, is not PCM WAV, or uses a sample
            format the analysis stream is never produced in.
    """
    source = Path(path)
    if not source.is_file():
        raise AnalysisError(
            f"Analysis audio not found: {source}",
            code=ErrorCode.AUDIO_ANALYSIS_FAILED,
            details={"path": str(source)},
            recoverable=False,
        )

    try:
        stream = wave.open(str(source), "rb")  # noqa: SIM115 - closed by the `with` below
    except (wave.Error, OSError, EOFError) as exc:
        raise AnalysisError(
            f"{source.name} is not a readable PCM WAV file.",
            code=ErrorCode.AUDIO_ANALYSIS_FAILED,
            details={"path": str(source)},
            cause=exc,
            recoverable=False,
        ) from exc

    with stream:
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        width = stream.getsampwidth()
        if width not in _SUPPORTED_SAMPLE_WIDTHS:
            raise AnalysisError(
                f"Unsupported sample width {width * 8} bit in {source.name}.",
                code=ErrorCode.AUDIO_ANALYSIS_FAILED,
                details={"path": str(source), "sample_width_bytes": width},
                recoverable=False,
            )

        window_samples = max(round(window_seconds * sample_rate), 1)
        hop_samples = max(round(hop_seconds * sample_rate), 1)

        first_sample = max(round(start * sample_rate), 0)
        if first_sample:
            try:
                stream.setpos(min(first_sample, stream.getnframes()))
            except (wave.Error, OSError) as exc:
                raise AnalysisError(
                    f"Cannot seek to {start:.3f}s in {source.name}.",
                    code=ErrorCode.AUDIO_ANALYSIS_FAILED,
                    details={"path": str(source), "start": start},
                    cause=exc,
                    recoverable=False,
                ) from exc
        last_sample = (
            None if end is None else max(round(end * sample_rate), first_sample)
        )

        buffer = np.zeros(0, dtype=np.float32)
        consumed = first_sample  # samples before the front of the buffer
        index = 0

        while True:
            if last_sample is not None and consumed + window_samples > last_sample:
                break
            needed = window_samples - buffer.size
            if needed > 0:
                block = _read_block(stream, max(needed, hop_samples), width, channels)
                if block.size == 0:
                    break
                buffer = np.concatenate((buffer, block))
                if buffer.size < window_samples:
                    # End of file inside a partial window: emit what there is
                    # rather than dropping the tail of the recording.
                    if buffer.size:
                        start = consumed / sample_rate
                        yield AudioWindow(
                            index=index,
                            start=start,
                            end=start + buffer.size / sample_rate,
                            samples=buffer,
                        )
                    break

            start = consumed / sample_rate
            yield AudioWindow(
                index=index,
                start=start,
                end=start + window_samples / sample_rate,
                samples=buffer[:window_samples],
            )
            index += 1
            buffer = buffer[hop_samples:]
            consumed += hop_samples


def analyse_stream(
    path: Path, *, window_seconds: float, hop_seconds: float
) -> AudioFeatures:
    """Compute the feature table for one audio stream (§18)."""
    if window_seconds <= 0 or hop_seconds <= 0:
        raise AnalysisError(
            "Audio analysis window and hop must both be positive.",
            code=ErrorCode.AUDIO_ANALYSIS_FAILED,
            details={"window_seconds": window_seconds, "hop_seconds": hop_seconds},
            recoverable=False,
        )

    source = Path(path)
    sample_rate = _sample_rate_of(source)

    times: list[float] = []
    rms: list[float] = []
    peak: list[float] = []
    crossings: list[float] = []
    centroid: list[float] = []
    flux: list[float] = []

    previous_spectrum: np.ndarray | None = None
    duration = 0.0

    for window in read_windows(path, window_seconds=window_seconds, hop_seconds=hop_seconds):
        samples = window.samples
        duration = max(duration, window.end)

        times.append(window.start)
        rms.append(_to_db(float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))))
        peak.append(_to_db(float(np.max(np.abs(samples))) if samples.size else 0.0))
        crossings.append(_zero_crossing_rate(samples))

        spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
        centroid.append(_spectral_centroid(spectrum, sample_rate, samples.size))
        flux.append(_spectral_flux(spectrum, previous_spectrum))
        previous_spectrum = spectrum

    features = AudioFeatures(
        path=source,
        sample_rate=sample_rate,
        duration_seconds=duration,
        window_seconds=window_seconds,
        hop_seconds=hop_seconds,
        times=np.asarray(times, dtype=np.float64),
        rms_db=np.asarray(rms, dtype=np.float64),
        peak_db=np.asarray(peak, dtype=np.float64),
        zero_crossing_rate=np.asarray(crossings, dtype=np.float64),
        spectral_centroid=np.asarray(centroid, dtype=np.float64),
        spectral_flux=np.asarray(flux, dtype=np.float64),
    )
    logger.info("Analysed audio stream", extra={"path": str(source), **features.summary()})
    return features


def rolling_baseline(values: np.ndarray, window: int) -> np.ndarray:
    """Return a centred rolling median of ``values``.

    The median, not the mean: a spike is exactly what must not move the
    baseline it is measured against, and a handful of loud windows drag a mean
    upward enough to hide the next one.
    """
    if values.size == 0:
        return values
    span = max(int(window), 1)
    if span >= values.size:
        return np.full_like(values, float(np.median(values)))

    half = span // 2
    padded = np.pad(values, (half, span - half - 1), mode="edge")
    strided = np.lib.stride_tricks.sliding_window_view(padded, span)
    return np.median(strided, axis=-1)


def find_runs(mask: np.ndarray, *, min_length: int = 1) -> list[tuple[int, int]]:
    """Return inclusive ``(first, last)`` index pairs of ``True`` runs.

    Silence, sustained speech and shouting are all runs rather than instants,
    and a run shorter than the configured minimum is not the thing being looked
    for -- a 0.3 s gap between words is not a silence (§18).
    """
    if mask.size == 0:
        return []
    padded = np.concatenate(([False], mask.astype(bool), [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    runs = [
        (int(start), int(end - 1))
        for start, end in zip(edges[0::2], edges[1::2], strict=False)
    ]
    return [run for run in runs if run[1] - run[0] + 1 >= min_length]


def seconds_to_windows(seconds: float, hop_seconds: float) -> int:
    """Convert a duration in seconds to a count of hops, rounded up."""
    if hop_seconds <= 0:
        return 0
    return max(math.ceil(seconds / hop_seconds), 1)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _read_block(
    stream: wave.Wave_read, frames: int, width: int, channels: int
) -> np.ndarray:
    """Read ``frames`` frames and return them as mono float32 in [-1, 1]."""
    raw = stream.readframes(frames)
    if not raw:
        return np.zeros(0, dtype=np.float32)

    dtype = _SUPPORTED_SAMPLE_WIDTHS[width]
    data = np.frombuffer(raw, dtype=dtype)
    if width == 1:
        # 8-bit WAV is unsigned, centred on 128.
        samples = (data.astype(np.float32) - 128.0) / 128.0
    else:
        samples = data.astype(np.float32) / float(1 << (width * 8 - 1))

    if channels > 1:
        usable = samples.size - (samples.size % channels)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32)


def _sample_rate_of(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as stream:
            return stream.getframerate()
    except (wave.Error, OSError, EOFError) as exc:
        raise AnalysisError(
            f"{path.name} is not a readable PCM WAV file.",
            code=ErrorCode.AUDIO_ANALYSIS_FAILED,
            details={"path": str(path)},
            cause=exc,
            recoverable=False,
        ) from exc


def _to_db(amplitude: float) -> float:
    """Convert a linear amplitude to dBFS, floored rather than infinite."""
    if amplitude <= _EPSILON:
        return SILENCE_FLOOR_DB
    return max(20.0 * math.log10(amplitude), SILENCE_FLOOR_DB)


def _zero_crossing_rate(samples: np.ndarray) -> float:
    if samples.size < 2:
        return 0.0
    return float(np.mean(np.abs(np.diff(np.signbit(samples).astype(np.int8)))))


def _spectral_centroid(spectrum: np.ndarray, sample_rate: int, size: int) -> float:
    total = float(np.sum(spectrum))
    if total <= _EPSILON:
        return 0.0
    frequencies = np.fft.rfftfreq(size, d=1.0 / sample_rate)
    return float(np.sum(frequencies * spectrum) / total)


def _spectral_flux(spectrum: np.ndarray, previous: np.ndarray | None) -> float:
    """Positive spectral change since the previous window.

    Only increases count. Sound stopping is not an onset, and counting it as
    one would put a transient at the end of every explosion as well as its
    beginning.
    """
    if previous is None or previous.shape != spectrum.shape:
        return 0.0
    difference = spectrum - previous
    positive = np.maximum(difference, 0.0)
    norm = float(np.sum(spectrum)) + _EPSILON
    return float(np.sum(positive) / norm)


__all__ = [
    "SILENCE_FLOOR_DB",
    "AudioFeatures",
    "AudioWindow",
    "analyse_stream",
    "find_runs",
    "read_windows",
    "rolling_baseline",
    "seconds_to_windows",
]
