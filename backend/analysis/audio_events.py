"""Audio event detection (SPEC sections 18, 19, 26).

§18 asks for RMS, LUFS, peak, silence, speech, noise, transients and spectral
activity, and for the events those measurements imply: spikes, sudden silence,
shots, explosions, shouting, laughter.

What this module does and does not claim is worth stating plainly, because the
difference decides how much later stages should trust it.

**Measured.** Level, dynamics, silence, onsets, spectral change. These come
from arithmetic on the waveform and are as reliable as the recording.

**Inferred.** Whether an onset is a gunshot or a door, whether raised voice is
excitement or anger. Nothing here can know that from a level curve, and §26
does not ask it to: an audio event is *one detector's observation*, and a
gameplay event only exists once several sources agree (§27). So the events
produced here carry a type that describes the **signal** -- a spike, a
transient, a sustained loud passage -- with confidence derived from how far the
evidence sits above the recording's own baseline, and the semantic reading is
left to correlation.

Every threshold is relative to a rolling baseline rather than absolute.
Recordings differ by twenty decibels depending on capture setup, and a fixed
"-20 dBFS is loud" rule would find everything in one recording and nothing in
the next.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np

from backend.analysis.signal import (
    AudioFeatures,
    find_runs,
    rolling_baseline,
    seconds_to_windows,
)
from backend.config.schema import AudioAnalysisConfig
from backend.core.errors import ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import AudioEventType
from backend.media.ffmpeg import FFmpegRunner

logger = get_logger("analysis.audio_events", LogChannel.PIPELINE)

#: Which stream an event came from. §19: a scream and an explosion are not the
#: same evidence, and the only thing that distinguishes them at this level is
#: which track they were heard on.
TrackRole = str
GAMEPLAY: Final[TrackRole] = "gameplay"
MICROPHONE: Final[TrackRole] = "microphone"

#: Window count for the rolling baseline. Sixty seconds is long enough that a
#: firefight does not become its own baseline, and short enough to follow a
#: recording whose level changes between menus and gameplay.
BASELINE_SECONDS: Final[float] = 60.0

#: A spike this far above baseline is as confident as this detector gets.
#: Beyond it the extra decibels say nothing more about whether it is an event.
CONFIDENCE_SATURATION_DB: Final[float] = 24.0

#: Speech-band bounds, used to separate voice from game rumble and hiss.
#: Deliberately wide: this is a hint for the reaction detector (§20), not a
#: classifier.
_SPEECH_CENTROID_HZ: Final[tuple[float, float]] = (150.0, 4000.0)

#: EBU R128 summary lines from FFmpeg's ebur128 filter.
_LOUDNESS_FIELDS: Final[dict[str, str]] = {
    "I": "integrated_lufs",
    "LRA": "loudness_range",
    "Peak": "true_peak_db",
}
_SUMMARY_LINE = re.compile(r"^\s*(I|LRA|Peak):\s*(-?\d+(?:\.\d+)?)\s")


@dataclass(frozen=True, slots=True)
class AudioEvent:
    """One observation on one audio track (§45 ``audio_events``)."""

    event_type: AudioEventType
    start_seconds: float
    end_seconds: float
    track_role: TrackRole = GAMEPLAY
    confidence: float = 1.0
    rms_db: float | None = None
    peak_db: float | None = None
    lufs: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0


@dataclass(frozen=True, slots=True)
class LoudnessSummary:
    """Programme loudness for one stream, from FFmpeg's EBU R128 meter."""

    integrated_lufs: float | None = None
    loudness_range: float | None = None
    true_peak_db: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "integrated_lufs": self.integrated_lufs,
            "loudness_range": self.loudness_range,
            "true_peak_db": self.true_peak_db,
        }


def detect_audio_events(
    features: AudioFeatures,
    config: AudioAnalysisConfig,
    *,
    track_role: TrackRole = GAMEPLAY,
    loudness: LoudnessSummary | None = None,
) -> list[AudioEvent]:
    """Run every enabled §18 detector over one stream's features.

    Returns events in chronological order. An empty list is a valid result:
    silent gameplay is a real recording, not a failure.
    """
    if features.is_empty:
        return []

    events: list[AudioEvent] = []
    detect = config.detect
    baseline_windows = seconds_to_windows(BASELINE_SECONDS, features.hop_seconds)
    baseline = rolling_baseline(features.rms_db, baseline_windows)

    if detect.silence:
        events += _detect_silence(features, config, track_role)
    if detect.rms or detect.peak:
        events += _detect_spikes(features, config, track_role, baseline)
    if detect.transients:
        events += _detect_transients(features, config, track_role, baseline)
    if detect.spectral_activity:
        events += _detect_speech(features, config, track_role, baseline)

    events.sort(key=lambda event: (event.start_seconds, event.event_type.value))

    if loudness is not None and loudness.integrated_lufs is not None:
        events = [
            AudioEvent(
                event_type=event.event_type,
                start_seconds=event.start_seconds,
                end_seconds=event.end_seconds,
                track_role=event.track_role,
                confidence=event.confidence,
                rms_db=event.rms_db,
                peak_db=event.peak_db,
                # Programme loudness, not per-event loudness: EBU R128 is a
                # measure over a whole stream, and attaching it to an event is
                # context, not a claim about that moment.
                lufs=loudness.integrated_lufs,
                metadata=event.metadata,
            )
            for event in events
        ]

    logger.info(
        "Detected audio events",
        extra={
            "path": str(features.path),
            "track_role": track_role,
            "events": len(events),
            "by_type": _counts(events),
        },
    )
    return events


def measure_loudness(path: Path, runner: FFmpegRunner) -> LoudnessSummary:
    """Measure EBU R128 programme loudness with FFmpeg (§18, §74).

    Delegated rather than reimplemented: R128 is a specified filter chain with
    K-weighting and gating, FFmpeg ships the reference implementation, and it
    streams -- so an eight-hour stream costs one pass and no memory (§7).

    Returns an empty summary rather than raising. Loudness is context for the
    mix (§74); failing an entire analysis stage over it would be out of
    proportion.
    """
    try:
        result = runner.run(
            [
                # The meter prints its summary at `info`; at the configured
                # `error` level it would measure and report nothing.
                *runner.base_arguments(loglevel="info"),
                "-i",
                str(path),
                "-filter_complex",
                "ebur128=peak=true",
                "-f",
                "null",
                "-",
            ],
            error_code=ErrorCode.AUDIO_ANALYSIS_FAILED,
            check=False,
        )
    except Exception as exc:  # the meter is optional context, never fatal
        logger.warning(
            "Loudness measurement failed", extra={"path": str(path), "error": str(exc)}
        )
        return LoudnessSummary()

    # The summary block is the last occurrence of each field; earlier lines are
    # the running meter.
    values: dict[str, float] = {}
    for line in result.stderr.splitlines():
        match = _SUMMARY_LINE.match(line)
        if match:
            values[_LOUDNESS_FIELDS[match.group(1)]] = float(match.group(2))
    return LoudnessSummary(**values)


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------


def _detect_silence(
    features: AudioFeatures, config: AudioAnalysisConfig, track_role: TrackRole
) -> list[AudioEvent]:
    """Sustained quiet (§18, and the raw material for §30 dead time).

    Absolute rather than relative, and deliberately so: silence is the one
    audio property with a meaningful absolute definition, and a rolling
    baseline would make a quiet passage "not silent" simply because everything
    around it was quiet too.
    """
    minimum = seconds_to_windows(config.min_silence_seconds, features.hop_seconds)
    quiet = features.rms_db <= config.silence_threshold_db
    return [
        AudioEvent(
            event_type=AudioEventType.SILENCE,
            start_seconds=start,
            end_seconds=end,
            track_role=track_role,
            confidence=1.0,
            rms_db=float(np.mean(features.rms_db[first : last + 1])),
            metadata={"threshold_db": config.silence_threshold_db},
        )
        for first, last in find_runs(quiet, min_length=minimum)
        for start, end in [features.window_span(first, last)]
    ]


def _detect_spikes(
    features: AudioFeatures,
    config: AudioAnalysisConfig,
    track_role: TrackRole,
    baseline: np.ndarray,
) -> list[AudioEvent]:
    """Level rising sharply above the recording's own baseline (§18).

    This is the detector that nominates candidate regions for the vision
    cascade (§16): something loud happened here, look at it.
    """
    excess = features.rms_db - baseline
    loud = excess >= config.spike_threshold_db
    events: list[AudioEvent] = []
    for first, last in find_runs(loud, min_length=1):
        start, end = features.window_span(first, last)
        peak_index = first + int(np.argmax(features.rms_db[first : last + 1]))
        above = float(excess[peak_index])
        events.append(
            AudioEvent(
                event_type=AudioEventType.SPIKE,
                start_seconds=start,
                end_seconds=end,
                track_role=track_role,
                confidence=_confidence(above, config.spike_threshold_db),
                rms_db=float(features.rms_db[peak_index]),
                peak_db=float(np.max(features.peak_db[first : last + 1])),
                metadata={
                    "above_baseline_db": round(above, 2),
                    "baseline_db": round(float(baseline[peak_index]), 2),
                    "peak_seconds": features.time_of(peak_index),
                },
            )
        )
    return events


def _detect_transients(
    features: AudioFeatures,
    config: AudioAnalysisConfig,
    track_role: TrackRole,
    baseline: np.ndarray,
) -> list[AudioEvent]:
    """Sharp onsets: the spectral discontinuity of a shot or an explosion (§18).

    Separate from spikes because the two disagree usefully. A gunshot is a
    transient without necessarily being a sustained spike; a shouted sentence
    is a spike without a sharp onset. Later correlation (§27) benefits from
    both being reported rather than merged.
    """
    flux = features.spectral_flux
    if flux.size == 0:
        return []

    # A relative threshold again: what counts as a sharp onset depends on how
    # busy the recording's spectrum normally is.
    window = seconds_to_windows(BASELINE_SECONDS, features.hop_seconds)
    flux_baseline = rolling_baseline(flux, window)
    deviation = float(np.std(flux)) or 1e-6
    onset = (flux - flux_baseline) >= 2.0 * deviation

    # Only where the level also rose: a spectral change during a quiet passage
    # is a change of texture, not an impact.
    louder = (features.rms_db - baseline) >= (config.spike_threshold_db / 2.0)

    events: list[AudioEvent] = []
    for first, last in find_runs(onset & louder, min_length=1):
        start, end = features.window_span(first, last)
        strongest = first + int(np.argmax(flux[first : last + 1]))
        strength = float((flux[strongest] - flux_baseline[strongest]) / deviation)
        events.append(
            AudioEvent(
                event_type=AudioEventType.TRANSIENT,
                start_seconds=start,
                end_seconds=end,
                track_role=track_role,
                confidence=min(strength / 6.0, 1.0),
                rms_db=float(features.rms_db[strongest]),
                peak_db=float(np.max(features.peak_db[first : last + 1])),
                metadata={
                    "flux_sigma": round(strength, 2),
                    "onset_seconds": features.time_of(strongest),
                },
            )
        )
    return _merge_touching(events)


def _detect_speech(
    features: AudioFeatures,
    config: AudioAnalysisConfig,
    track_role: TrackRole,
    baseline: np.ndarray,
) -> list[AudioEvent]:
    """Passages whose level and spectrum look like voice (§18).

    A hint, not a transcript. Whisper decides what was said (§14); this exists
    so the reaction detector has candidate regions on the microphone track
    before any model has run, and so silence-versus-activity is known even when
    transcription is unavailable (§95).
    """
    low, high = _SPEECH_CENTROID_HZ
    voiced = (
        (features.spectral_centroid >= low)
        & (features.spectral_centroid <= high)
        & (features.rms_db > config.silence_threshold_db)
        & (features.rms_db >= baseline - 3.0)
        # Voice is not noise: a high crossing rate is hiss, fans or static.
        & (features.zero_crossing_rate < 0.35)
    )
    minimum = seconds_to_windows(0.4, features.hop_seconds)

    events: list[AudioEvent] = []
    for first, last in find_runs(voiced, min_length=minimum):
        start, end = features.window_span(first, last)
        events.append(
            AudioEvent(
                event_type=AudioEventType.SPEECH,
                start_seconds=start,
                end_seconds=end,
                track_role=track_role,
                # Modest by construction. This is an energy-and-spectrum
                # heuristic, and reporting it as certain would let it outvote
                # the model that actually reads speech (§27).
                confidence=0.5,
                rms_db=float(np.mean(features.rms_db[first : last + 1])),
                metadata={
                    "median_centroid_hz": round(
                        float(np.median(features.spectral_centroid[first : last + 1])), 1
                    )
                },
            )
        )
    return events


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _merge_touching(events: list[AudioEvent]) -> list[AudioEvent]:
    """Collapse events whose spans touch or overlap into one.

    Analysis windows overlap by design, so two adjacent windows share samples
    and cannot have heard independent onsets. Without this, one explosion
    arrives as two or three transients, and every downstream count -- candidate
    regions, correlation weight, moment scoring -- is inflated by an artefact
    of the window layout.

    The merged event keeps the strongest constituent's confidence and levels,
    because that is the window that actually detected it.
    """
    if len(events) < 2:
        return events

    ordered = sorted(events, key=lambda event: event.start_seconds)
    merged: list[AudioEvent] = [ordered[0]]
    for event in ordered[1:]:
        previous = merged[-1]
        if event.start_seconds > previous.end_seconds:
            merged.append(event)
            continue
        strongest = max(previous, event, key=lambda item: item.confidence)
        merged[-1] = AudioEvent(
            event_type=previous.event_type,
            start_seconds=previous.start_seconds,
            end_seconds=max(previous.end_seconds, event.end_seconds),
            track_role=previous.track_role,
            confidence=strongest.confidence,
            rms_db=strongest.rms_db,
            peak_db=max(
                (value for value in (previous.peak_db, event.peak_db) if value is not None),
                default=None,
            ),
            lufs=previous.lufs,
            metadata={**strongest.metadata, "merged_windows": True},
        )
    return merged


def _confidence(above_baseline_db: float, threshold_db: float) -> float:
    """Map "how far above the baseline" onto 0-1.

    Zero at the threshold, one at :data:`CONFIDENCE_SATURATION_DB` above it.
    A linear ramp rather than anything cleverer, because the input is a level
    difference and a fitted curve would imply a precision the measurement does
    not have.
    """
    span = max(CONFIDENCE_SATURATION_DB - threshold_db, 1.0)
    return float(min(max((above_baseline_db - threshold_db) / span, 0.0), 1.0))


def _counts(events: list[AudioEvent]) -> str:
    tally: dict[str, int] = {}
    for event in events:
        tally[event.event_type.value] = tally.get(event.event_type.value, 0) + 1
    return json.dumps(tally, sort_keys=True)


__all__ = [
    "BASELINE_SECONDS",
    "GAMEPLAY",
    "MICROPHONE",
    "AudioEvent",
    "LoudnessSummary",
    "TrackRole",
    "detect_audio_events",
    "measure_loudness",
]
