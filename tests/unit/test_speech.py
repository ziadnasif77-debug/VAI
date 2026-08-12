"""Speech provider behaviour (Phase 3, SPEC §13, §14, §52, §95).

None of this loads a model. What is worth testing without one is the part that
is ours: the offset arithmetic that puts a chunk's words on the source
timeline, the device and compute-type resolution that decides whether the CPU
fallback actually works, and the factory's refusal to silently substitute a
provider.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from ai.providers.base import SpeechProvider
from ai.speech import create_speech_provider
from ai.speech.fake_provider import FakeSpeechProvider
from ai.speech.faster_whisper_provider import (
    FasterWhisperProvider,
    _confidence_from,
    _to_segment,
)
from backend.core.errors import ErrorCode, ModelError


def _silence(path: Path, seconds: float = 20.0, sample_rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return path


class TestFakeProvider:
    def test_it_satisfies_the_provider_protocol(self) -> None:
        # The protocol is the contract every stage above depends on (§13).
        assert isinstance(FakeSpeechProvider(), SpeechProvider)

    def test_the_same_file_always_yields_the_same_transcript(self, tmp_path: Path) -> None:
        path = _silence(tmp_path / "a.wav", 10.0)
        first = FakeSpeechProvider().transcribe(path)
        second = FakeSpeechProvider().transcribe(path)
        assert [s.text for s in first] == [s.text for s in second]
        assert [s.start for s in first] == [s.start for s in second]

    def test_segments_stay_inside_the_audio(self, tmp_path: Path) -> None:
        segments = FakeSpeechProvider().transcribe(_silence(tmp_path / "a.wav", 10.0))
        assert segments
        assert all(0.0 <= s.start < s.end <= 10.0 for s in segments)

    def test_words_stay_inside_their_segment(self, tmp_path: Path) -> None:
        # Word timings that escape their segment break caption timing (§71).
        for segment in FakeSpeechProvider().transcribe(_silence(tmp_path / "a.wav", 10.0)):
            assert segment.words
            assert segment.words[0].start >= segment.start - 1e-9
            assert segment.words[-1].end <= segment.end + 1e-9
            for earlier, later in zip(segment.words, segment.words[1:], strict=False):
                assert earlier.end <= later.start + 1e-9

    def test_the_offset_moves_every_timestamp(self, tmp_path: Path) -> None:
        # §7: a chunk's results must land on the source timeline, not on the
        # slice's own.
        path = _silence(tmp_path / "a.wav", 10.0)
        base = FakeSpeechProvider().transcribe(path)
        shifted = FakeSpeechProvider().transcribe(path, start_offset=600.0)
        assert [s.start for s in shifted] == [pytest.approx(s.start + 600.0) for s in base]
        assert all(w.start >= 600.0 for s in shifted for w in s.words)

    def test_a_silent_provider_returns_nothing(self, tmp_path: Path) -> None:
        assert FakeSpeechProvider(silent=True).transcribe(_silence(tmp_path / "a.wav")) == ()

    def test_an_unreadable_file_returns_nothing_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "not.wav"
        path.write_bytes(b"nope")
        assert FakeSpeechProvider().transcribe(path) == ()

    def test_it_records_what_it_was_asked_to_do(self, tmp_path: Path) -> None:
        provider = FakeSpeechProvider()
        provider.load()
        provider.transcribe(_silence(tmp_path / "a.wav"), start_offset=5.0)
        provider.unload()
        assert provider.load_count == 1
        assert provider.unload_count == 1
        assert provider.transcribe_calls[0][1] == 5.0


class TestDeviceResolution:
    """§52: "CPU fallback must exist where technically practical"."""

    @staticmethod
    def _provider(config, **overrides) -> FasterWhisperProvider:
        speech = config.models.speech.model_copy(update=overrides)
        return FasterWhisperProvider(speech, gpu=config.gpu)

    def test_cpu_is_honoured_when_configured(self, config) -> None:
        assert self._provider(config, device="cpu")._resolve_device() == "cpu"

    def test_a_disabled_gpu_forces_cpu(self, config) -> None:
        speech = config.models.speech.model_copy(update={"device": "cuda"})
        provider = FasterWhisperProvider(
            speech, gpu=config.gpu.model_copy(update={"enabled": False})
        )
        assert provider._resolve_device() == "cpu"

    def test_cpu_cannot_run_float16(self, config) -> None:
        # CTranslate2 rejects it outright, so a "CPU fallback" that kept the
        # configured GPU compute type would fail on load rather than fall back.
        provider = self._provider(config, compute_type="float16")
        assert provider._resolve_compute_type("cpu") == "int8"
        assert provider._resolve_compute_type("cuda") == "float16"

    def test_a_cpu_compatible_compute_type_is_left_alone(self, config) -> None:
        provider = self._provider(config, compute_type="int8")
        assert provider._resolve_compute_type("cpu") == "int8"

    def test_a_model_too_large_for_the_card_is_refused_before_loading(self, config) -> None:
        # §54: discovering this as an out-of-memory error mid-analysis wastes
        # everything the stage had already done.
        speech = config.models.speech.model_copy(
            update={"estimated_vram_mb": 999_999, "device": "cuda"}
        )
        provider = FasterWhisperProvider(speech, gpu=config.gpu)
        with pytest.raises(ModelError) as exc_info:
            provider._preflight_vram("cuda")
        assert exc_info.value.code is ErrorCode.GPU_OUT_OF_MEMORY

    def test_the_preflight_check_does_not_apply_to_cpu(self, config) -> None:
        speech = config.models.speech.model_copy(update={"estimated_vram_mb": 999_999})
        FasterWhisperProvider(speech, gpu=config.gpu)._preflight_vram("cpu")

    def test_provenance_survives_into_the_model_info(self, config) -> None:
        # §49: a wrong transcript must be traceable to the model that made it.
        info = self._provider(config).info()
        assert info.version == config.models.speech.version
        assert info.provider == "faster_whisper"


class TestSegmentConversion:
    class _Word:
        def __init__(self, word, start, end, probability=None):
            self.word, self.start, self.end, self.probability = word, start, end, probability

    class _Segment:
        def __init__(self, start, end, text, words=(), avg_logprob=None):
            self.start, self.end, self.text = start, end, text
            self.words, self.avg_logprob = words, avg_logprob

    def test_offsets_apply_to_segments_and_words(self) -> None:
        segment = self._Segment(
            1.0, 2.0, " hello ", words=(self._Word(" hello", 1.0, 2.0, 0.8),)
        )
        converted = _to_segment(segment, 600.0)
        assert converted.start == 601.0
        assert converted.end == 602.0
        assert converted.text == "hello"
        assert converted.words[0].start == 601.0
        assert converted.words[0].confidence == 0.8

    def test_a_segment_without_words_still_converts(self) -> None:
        assert _to_segment(self._Segment(0.0, 1.0, "x"), 0.0).words == ()

    def test_log_probability_becomes_a_bounded_confidence(self) -> None:
        assert _confidence_from(0.0) == 1.0
        assert 0.0 < _confidence_from(-0.5) < 1.0
        assert _confidence_from(-100.0) == pytest.approx(0.0, abs=1e-6)
        assert _confidence_from(None) is None


class TestProviderFactory:
    def test_the_configured_provider_is_built(self, config) -> None:
        assert isinstance(create_speech_provider(config), FasterWhisperProvider)

    def test_the_fake_can_be_selected_by_configuration(self, config) -> None:
        models = config.models.model_copy(
            update={"speech": config.models.speech.model_copy(update={"provider": "fake"})}
        )
        assert isinstance(
            create_speech_provider(config.model_copy(update={"models": models})),
            FakeSpeechProvider,
        )

    def test_an_unknown_provider_fails_loudly(self, config) -> None:
        # A typo must not quietly transcribe nothing.
        models = config.models.model_copy(
            update={"speech": config.models.speech.model_copy(update={"provider": "whisper.cpp"})}
        )
        with pytest.raises(ModelError) as exc_info:
            create_speech_provider(config.model_copy(update={"models": models}))
        assert exc_info.value.code is ErrorCode.PROVIDER_NOT_REGISTERED
        assert exc_info.value.recoverable is False


class TestTranscribeOptions:
    """The fast-local-Whisper playbook, wired rather than pasted.

    batch_size sat in the config for months with nothing reading it;
    beam_size was hardcoded; condition_on_previous_text rode Whisper's
    hallucination-friendly default. One testable dict now carries the surface.
    """

    def test_the_config_reaches_the_call(self, config) -> None:
        from ai.speech.faster_whisper_provider import transcribe_options

        options = transcribe_options(config.models.speech, None)

        assert options["beam_size"] == 5
        assert options["condition_on_previous_text"] is False
        assert options["vad_parameters"] == {
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 200,
        }

    def test_word_timestamps_stay_on(self, config) -> None:
        # §71 times captions from the transcript. Words are the product.
        from ai.speech.faster_whisper_provider import transcribe_options

        assert transcribe_options(config.models.speech, None)["word_timestamps"] is True

    def test_an_explicit_language_wins_over_the_config(self, config) -> None:
        from ai.speech.faster_whisper_provider import transcribe_options

        assert transcribe_options(config.models.speech, "ar")["language"] == "ar"

    def test_the_batched_pipeline_is_importable_here(self) -> None:
        # The batch_size knob promises the batched path; this machine must be
        # able to keep the promise (fw >= 1.0).
        from faster_whisper import BatchedInferencePipeline  # noqa: F401
