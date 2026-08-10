"""Audio analysis without FFmpeg (Phase 3, SPEC §18, §19, §20).

The signals are built numerically, so each test knows exactly what is in the
file it is analysing: an impact at 5.0 s is at 5.0 s, and a 5 Hz amplitude
modulation really is 5 Hz. That is what makes an assertion about a detector
meaningful rather than a description of whatever it happened to output.

Everything here uses the standard library's ``wave`` writer and NumPy, so it
runs on any machine.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from backend.analysis.audio_events import (
    GAMEPLAY,
    MICROPHONE,
    detect_audio_events,
    measure_loudness,
)
from backend.analysis.reactions import detect_reactions
from backend.analysis.signal import (
    SILENCE_FLOOR_DB,
    analyse_stream,
    find_runs,
    read_windows,
    rolling_baseline,
    seconds_to_windows,
)
from backend.core.errors import AnalysisError
from backend.core.models.enums import AudioEventType, ReactionType

SAMPLE_RATE = 16000


def _write(path: Path, samples: np.ndarray, *, sample_rate: int = SAMPLE_RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(data.tobytes())
    return path


def _tone(seconds: float, frequency: float = 220.0, amplitude: float = 0.02) -> np.ndarray:
    time = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    return amplitude * np.sin(2 * np.pi * frequency * time)


class TestStreamReading:
    def test_windows_carry_absolute_timestamps(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.wav", _tone(4.0))
        windows = list(read_windows(path, window_seconds=0.5, hop_seconds=0.25))
        assert windows[0].start == 0.0
        assert windows[1].start == pytest.approx(0.25)
        assert windows[-1].end <= 4.0 + 1e-6
        assert all(window.samples.size == 8000 for window in windows[:-1])

    def test_a_span_seeks_instead_of_reading_up_to_it(self, tmp_path: Path) -> None:
        # §20 needs a fine envelope over a few seconds of a long stream; reading
        # from the start to get there would defeat the point.
        path = _write(tmp_path / "a.wav", _tone(20.0))
        windows = list(
            read_windows(path, window_seconds=0.1, hop_seconds=0.05, start=10.0, end=11.0)
        )
        assert windows
        assert windows[0].start == pytest.approx(10.0)
        assert windows[-1].end <= 11.0 + 1e-6

    def test_a_missing_file_is_a_typed_error(self, tmp_path: Path) -> None:
        with pytest.raises(AnalysisError):
            list(read_windows(tmp_path / "absent.wav", window_seconds=0.5, hop_seconds=0.25))

    def test_a_non_wav_file_is_a_typed_error(self, tmp_path: Path) -> None:
        path = tmp_path / "not.wav"
        path.write_bytes(b"definitely not a wav")
        with pytest.raises(AnalysisError):
            list(read_windows(path, window_seconds=0.5, hop_seconds=0.25))


class TestFeatures:
    def test_level_columns_track_the_signal(self, tmp_path: Path) -> None:
        loud = _write(tmp_path / "loud.wav", _tone(2.0, amplitude=0.5))
        quiet = _write(tmp_path / "quiet.wav", _tone(2.0, amplitude=0.01))
        loud_features = analyse_stream(loud, window_seconds=0.5, hop_seconds=0.25)
        quiet_features = analyse_stream(quiet, window_seconds=0.5, hop_seconds=0.25)
        assert np.median(loud_features.rms_db) > np.median(quiet_features.rms_db) + 20

    def test_digital_silence_hits_the_floor_rather_than_negative_infinity(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "silent.wav", np.zeros(SAMPLE_RATE * 2))
        features = analyse_stream(path, window_seconds=0.5, hop_seconds=0.25)
        assert np.all(features.rms_db == SILENCE_FLOOR_DB)
        assert np.all(np.isfinite(features.rms_db))

    def test_the_spectral_centroid_follows_pitch(self, tmp_path: Path) -> None:
        low = analyse_stream(
            _write(tmp_path / "low.wav", _tone(2.0, frequency=200, amplitude=0.3)),
            window_seconds=0.5,
            hop_seconds=0.25,
        )
        high = analyse_stream(
            _write(tmp_path / "high.wav", _tone(2.0, frequency=3000, amplitude=0.3)),
            window_seconds=0.5,
            hop_seconds=0.25,
        )
        assert np.median(high.spectral_centroid) > np.median(low.spectral_centroid) * 5

    def test_memory_does_not_hold_the_stream(self, tmp_path: Path) -> None:
        # §7 at this layer: the feature table is what remains, and it is bounded
        # by duration over hop -- not by the size of the audio.
        path = _write(tmp_path / "long.wav", _tone(60.0))
        features = analyse_stream(path, window_seconds=0.5, hop_seconds=0.25)
        assert len(features) == pytest.approx(240, abs=2)
        assert features.rms_db.nbytes < 4000


class TestHelpers:
    def test_rolling_baseline_ignores_the_spike_it_measures(self) -> None:
        values = np.full(200, -40.0)
        values[100:104] = 0.0
        baseline = rolling_baseline(values, 61)
        # A mean would be dragged up by the spike and hide the next one.
        assert baseline[102] == pytest.approx(-40.0)

    def test_rolling_baseline_handles_a_short_signal(self) -> None:
        values = np.array([-30.0, -20.0, -10.0])
        assert np.all(rolling_baseline(values, 999) == -20.0)

    def test_find_runs_returns_inclusive_bounds(self) -> None:
        mask = np.array([False, True, True, False, True, False])
        assert find_runs(mask) == [(1, 2), (4, 4)]

    def test_find_runs_honours_a_minimum_length(self) -> None:
        mask = np.array([True, False, True, True, True])
        assert find_runs(mask, min_length=2) == [(2, 4)]

    def test_find_runs_on_an_empty_mask(self) -> None:
        assert find_runs(np.array([], dtype=bool)) == []

    def test_seconds_convert_to_whole_hops(self) -> None:
        assert seconds_to_windows(1.0, 0.25) == 4
        assert seconds_to_windows(0.1, 0.25) == 1
        assert seconds_to_windows(1.0, 0.0) == 0


class TestAudioEventDetection:
    @staticmethod
    def _impacts(tmp_path: Path, times=(5.0, 15.0, 25.0), seconds: float = 30.0) -> Path:
        time = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
        signal = 0.02 * np.sin(2 * np.pi * 220 * time)
        for impact in times:
            window = (time >= impact) & (time < impact + 0.4)
            signal[window] += 0.7 * np.sin(2 * np.pi * 900 * time[window])
        return _write(tmp_path / "impacts.wav", signal)

    def test_spikes_land_on_the_impacts(self, tmp_path: Path, config) -> None:
        features = analyse_stream(self._impacts(tmp_path), window_seconds=0.5, hop_seconds=0.25)
        events = detect_audio_events(features, config.analysis.audio, track_role=GAMEPLAY)
        spikes = [e for e in events if e.event_type is AudioEventType.SPIKE]
        assert len(spikes) == 3
        for spike, expected in zip(spikes, (5.0, 15.0, 25.0), strict=True):
            assert spike.start_seconds <= expected <= spike.end_seconds

    def test_one_impact_produces_one_transient(self, tmp_path: Path, config) -> None:
        # Analysis windows overlap, so adjacent windows share samples and cannot
        # have heard independent onsets. Three impacts, three transients.
        features = analyse_stream(self._impacts(tmp_path), window_seconds=0.5, hop_seconds=0.25)
        events = detect_audio_events(features, config.analysis.audio, track_role=GAMEPLAY)
        assert sum(1 for e in events if e.event_type is AudioEventType.TRANSIENT) == 3

    def test_silence_is_detected_only_when_it_lasts(self, tmp_path: Path, config) -> None:
        seconds = 20.0
        time = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
        signal = 0.05 * np.sin(2 * np.pi * 220 * time)
        signal[(time >= 5.0) & (time < 9.0)] = 0.0  # long: a real silence
        signal[(time >= 15.0) & (time < 15.3)] = 0.0  # short: a gap, not a silence
        features = analyse_stream(
            _write(tmp_path / "gaps.wav", signal), window_seconds=0.5, hop_seconds=0.25
        )
        silences = [
            e
            for e in detect_audio_events(features, config.analysis.audio)
            if e.event_type is AudioEventType.SILENCE
        ]
        assert len(silences) == 1
        assert silences[0].start_seconds == pytest.approx(5.0, abs=0.6)
        assert silences[0].duration == pytest.approx(4.0, abs=0.6)

    def test_a_steady_recording_produces_no_spikes(self, tmp_path: Path, config) -> None:
        # Thresholds are relative to the recording's own baseline, so a loud but
        # unchanging signal is not an event.
        features = analyse_stream(
            _write(tmp_path / "steady.wav", _tone(20.0, amplitude=0.4)),
            window_seconds=0.5,
            hop_seconds=0.25,
        )
        events = detect_audio_events(features, config.analysis.audio)
        assert not [e for e in events if e.event_type is AudioEventType.SPIKE]

    def test_events_carry_the_track_they_were_heard_on(self, tmp_path: Path, config) -> None:
        # §19: the same measurement on two tracks is two different pieces of
        # evidence, and the schema must never lose which was which.
        features = analyse_stream(self._impacts(tmp_path), window_seconds=0.5, hop_seconds=0.25)
        events = detect_audio_events(features, config.analysis.audio, track_role=MICROPHONE)
        assert all(event.track_role == MICROPHONE for event in events)

    def test_an_empty_stream_yields_no_events(self, tmp_path: Path, config) -> None:
        features = analyse_stream(
            _write(tmp_path / "tiny.wav", np.zeros(10)), window_seconds=0.5, hop_seconds=0.25
        )
        assert detect_audio_events(features, config.analysis.audio) == []

    def test_confidence_rises_with_the_excess_over_baseline(
        self, tmp_path: Path, config
    ) -> None:
        def peak_confidence(amplitude: float) -> float:
            seconds = 20.0
            time = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
            signal = 0.02 * np.sin(2 * np.pi * 220 * time)
            window = (time >= 10.0) & (time < 10.4)
            signal[window] += amplitude * np.sin(2 * np.pi * 900 * time[window])
            features = analyse_stream(
                _write(tmp_path / f"s{amplitude}.wav", signal),
                window_seconds=0.5,
                hop_seconds=0.25,
            )
            spikes = [
                e
                for e in detect_audio_events(features, config.analysis.audio)
                if e.event_type is AudioEventType.SPIKE
            ]
            return spikes[0].confidence if spikes else 0.0

        assert peak_confidence(0.9) >= peak_confidence(0.08)


class TestReactions:
    """§19 and §20: the microphone, read on its own and lined up with the game."""

    @staticmethod
    def _tracks(tmp_path: Path, config):
        from tests.conftest import make_reaction_audio

        gameplay, microphone = make_reaction_audio()
        audio = config.analysis.audio
        game_features = analyse_stream(
            _write(tmp_path / "game.wav", gameplay),
            window_seconds=audio.window_seconds,
            hop_seconds=audio.hop_seconds,
        )
        mic_features = analyse_stream(
            _write(tmp_path / "mic.wav", microphone),
            window_seconds=audio.window_seconds,
            hop_seconds=audio.hop_seconds,
        )
        return (
            game_features,
            detect_audio_events(game_features, audio, track_role=GAMEPLAY),
            mic_features,
            detect_audio_events(mic_features, audio, track_role=MICROPHONE),
        )

    def test_laughter_is_identified_by_its_modulation(self, tmp_path: Path, config) -> None:
        _, game_events, mic_features, mic_events = self._tracks(tmp_path, config)
        reactions = detect_reactions(
            mic_features, mic_events, config.analysis, gameplay_events=game_events
        )
        laughs = [r for r in reactions if r.reaction_type is ReactionType.LAUGH]
        assert len(laughs) == 1
        assert laughs[0].start_seconds == pytest.approx(6.0, abs=1.0)
        # The evidence, not a guess: the fixture modulates at 5 Hz.
        assert laughs[0].metadata["modulation_hz"] == pytest.approx(5.0, abs=1.0)

    def test_a_scream_is_identified_by_level_and_brightness(
        self, tmp_path: Path, config
    ) -> None:
        _, game_events, mic_features, mic_events = self._tracks(tmp_path, config)
        reactions = detect_reactions(
            mic_features, mic_events, config.analysis, gameplay_events=game_events
        )
        screams = [r for r in reactions if r.reaction_type is ReactionType.SCREAM]
        assert len(screams) == 1
        assert screams[0].start_seconds == pytest.approx(16.0, abs=1.0)

    def test_reactions_are_correlated_with_the_gameplay_event_that_caused_them(
        self, tmp_path: Path, config
    ) -> None:
        _, game_events, mic_features, mic_events = self._tracks(tmp_path, config)
        reactions = detect_reactions(
            mic_features, mic_events, config.analysis, gameplay_events=game_events
        )
        assert reactions
        assert all(r.is_correlated for r in reactions)
        # The reaction follows its cause.
        assert all(r.correlation_offset > 0 for r in reactions)
        assert all(
            r.correlation_offset <= config.analysis.reactions.correlation_window_seconds
            for r in reactions
        )

    def test_no_reaction_is_invented_where_there_was_none(
        self, tmp_path: Path, config
    ) -> None:
        # The fixture's third impact, at 25 s, has nothing after it.
        _, game_events, mic_features, mic_events = self._tracks(tmp_path, config)
        reactions = detect_reactions(
            mic_features, mic_events, config.analysis, gameplay_events=game_events
        )
        assert not [r for r in reactions if r.start_seconds > 20.0]

    def test_correlation_raises_confidence_without_being_required(
        self, tmp_path: Path, config
    ) -> None:
        _, game_events, mic_features, mic_events = self._tracks(tmp_path, config)
        alone = detect_reactions(mic_features, mic_events, config.analysis)
        correlated = detect_reactions(
            mic_features, mic_events, config.analysis, gameplay_events=game_events
        )
        assert len(alone) == len(correlated)
        assert all(not r.is_correlated for r in alone)
        assert correlated[0].confidence >= alone[0].confidence

    def test_the_detector_can_be_switched_off(self, tmp_path: Path, config) -> None:
        _, _, mic_features, mic_events = self._tracks(tmp_path, config)
        disabled = config.analysis.model_copy(
            update={"reactions": config.analysis.reactions.model_copy(update={"enabled": False})}
        )
        assert detect_reactions(mic_features, mic_events, disabled) == []

    def test_a_reaction_persists_as_an_audio_event_on_the_microphone_track(
        self, tmp_path: Path, config
    ) -> None:
        _, game_events, mic_features, mic_events = self._tracks(tmp_path, config)
        reactions = detect_reactions(
            mic_features, mic_events, config.analysis, gameplay_events=game_events
        )
        events = [reaction.as_audio_event() for reaction in reactions]
        assert all(event.track_role == MICROPHONE for event in events)
        assert {event.metadata["reaction_type"] for event in events} == {"laugh", "scream"}


@pytest.mark.requires_ffmpeg
class TestLoudness:
    def test_programme_loudness_is_measured(self, tmp_path: Path, ffmpeg_runner) -> None:
        path = _write(tmp_path / "tone.wav", _tone(5.0, amplitude=0.5))
        summary = measure_loudness(path, ffmpeg_runner)
        assert summary.integrated_lufs is not None
        assert -40.0 < summary.integrated_lufs < 0.0

    def test_a_broken_file_returns_an_empty_summary_rather_than_failing(
        self, tmp_path: Path, ffmpeg_runner
    ) -> None:
        # Loudness is context for the mix (§74); losing it must not fail a stage.
        broken = tmp_path / "broken.wav"
        broken.write_bytes(b"\x00" * 512)
        assert measure_loudness(broken, ffmpeg_runner).integrated_lufs is None
