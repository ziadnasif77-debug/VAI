"""Scene detection, prompt versioning and vision-response validation.

Phase 4, SPEC §17, §92, §93, §94. No model runs here: what is tested is the
part that is ours — that a boundary is found where the picture actually
changed, that a prompt cannot drift from its version, and that a malformed
model response is rejected rather than carried into the timeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai.vision import create_vision_provider
from ai.vision.fake_provider import FakeVisionProvider
from ai.vision.ollama_provider import (
    OllamaVisionProvider,
    _clock,
    _confidence,
    _to_observations,
)
from backend.analysis.scenes import Scene, SceneResult, detect_scenes, scene_change_regions
from backend.core.errors import ConfigurationError, ErrorCode, ModelError, ValidationError
from backend.core.prompts import (
    METADATA_FILENAME,
    PROMPT_FILENAME,
    available_prompts,
    clear_prompt_cache,
    load_prompt,
)
from backend.core.versions import PROMPT_VERSIONS

pytestmark = pytest.mark.unit


class TestPromptRegistry:
    """§92: a prompt whose version does not move when its wording does is a
    silent cache poisoner, so the registry and the file must agree."""

    def test_the_shipped_prompt_loads(self) -> None:
        prompt = load_prompt("vision.frame_description")
        assert prompt.version == PROMPT_VERSIONS["vision.frame_description"]
        assert prompt.text
        assert prompt.output_schema["properties"]["frames"]

    def test_every_registered_prompt_exists_on_disk(self) -> None:
        assert set(available_prompts()) == set(PROMPT_VERSIONS)

    def test_an_unregistered_prompt_is_refused(self) -> None:
        with pytest.raises(ConfigurationError) as exc_info:
            load_prompt("vision.not_registered")
        assert exc_info.value.code is ErrorCode.CONFIG_INVALID

    def test_a_version_mismatch_is_refused(self, tmp_path: Path) -> None:
        directory = tmp_path / "prompts" / "vision" / "frame_description"
        directory.mkdir(parents=True)
        (directory / PROMPT_FILENAME).write_text("hello", encoding="utf-8")
        (directory / METADATA_FILENAME).write_text(
            json.dumps({"version": 99, "purpose": "drifted"}), encoding="utf-8"
        )
        clear_prompt_cache()
        try:
            with pytest.raises(ConfigurationError, match="stale cached results"):
                load_prompt("vision.frame_description", tmp_path)
        finally:
            clear_prompt_cache()

    def test_rendering_requires_every_placeholder(self) -> None:
        # A prompt sent with a literal "{game}" in it wastes a model call and
        # answers a question nobody asked.
        prompt = load_prompt("vision.frame_description")
        with pytest.raises(ConfigurationError):
            prompt.render(game="Apex")

    def test_rendering_substitutes_the_values(self) -> None:
        rendered = load_prompt("vision.frame_description").render(
            game="Apex Legends", frame_count=2, timestamps="00:00:01.000, 00:00:02.000"
        )
        assert "Apex Legends" in rendered
        assert "{" not in rendered.split("`")[0]


class TestResponseValidation:
    """§94: reject, retry, fall back. Never carry an invalid result forward."""

    @staticmethod
    def _payload(frames: list[dict]) -> str:
        return json.dumps({"frames": frames})

    def test_a_valid_response_becomes_observations(self) -> None:
        raw = self._payload(
            [
                {"description": "a firefight", "labels": ["Combat"], "confidence": 0.8},
                {"description": "a menu", "labels": [], "confidence": 0.4, "hud": {"score": "7"}},
            ]
        )
        observations = _to_observations(raw, (10.0, 20.0))
        assert [o.timestamp for o in observations] == [10.0, 20.0]
        assert observations[0].labels == ("combat",)
        assert observations[1].hud == {"score": "7"}

    def test_a_non_json_response_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _to_observations("I looked at the frames and saw a firefight.", (1.0,))
        assert exc_info.value.code is ErrorCode.LLM_INVALID_JSON

    def test_a_response_without_frames_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _to_observations(json.dumps({"result": "ok"}), (1.0,))
        assert exc_info.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED

    def test_a_count_mismatch_is_rejected(self) -> None:
        # An observation attached to the wrong second is worse than none,
        # because it will be believed.
        with pytest.raises(ValidationError, match="cannot be matched to timestamps"):
            _to_observations(self._payload([{"description": "x", "confidence": 1}]), (1.0, 2.0))

    def test_an_empty_description_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _to_observations(self._payload([{"description": "   ", "confidence": 1}]), (1.0,))

    def test_out_of_range_confidence_is_clamped_not_rejected(self) -> None:
        # The description is still usable; the number is a hint either way.
        assert _confidence(1.7) == 1.0
        assert _confidence(-0.4) == 0.0
        assert _confidence("high") == 0.0

    def test_timestamps_are_formatted_as_clock_times_for_the_prompt(self) -> None:
        assert _clock(0.0) == "00:00:00.000"
        assert _clock(3725.25) == "01:02:05.250"


class TestVisionProviderWiring:
    def test_the_configured_provider_is_built(self, config) -> None:
        assert isinstance(create_vision_provider(config), OllamaVisionProvider)

    def test_an_unknown_game_becomes_a_neutral_phrase(self, config) -> None:
        # §23: telling the model the game is called "auto" is worse than
        # telling it nothing.
        from ai.vision import _game_phrase

        assert _game_phrase("auto") == "an unidentified game"
        assert _game_phrase("") == "an unidentified game"
        assert _game_phrase("Apex Legends") == "Apex Legends"

    def test_an_unknown_provider_fails_loudly(self, config) -> None:
        models = config.models.model_copy(
            update={"vision": config.models.vision.model_copy(update={"provider": "llava.cpp"})}
        )
        with pytest.raises(ModelError) as exc_info:
            create_vision_provider(config.model_copy(update={"models": models}))
        assert exc_info.value.code is ErrorCode.PROVIDER_NOT_REGISTERED

    def test_a_model_too_large_for_the_card_is_refused_before_loading(self, config) -> None:
        vision = config.models.vision.model_copy(update={"estimated_vram_mb": 999_999})
        provider = OllamaVisionProvider(vision, gpu=config.gpu)
        with pytest.raises(ModelError) as exc_info:
            provider._preflight_vram()
        assert exc_info.value.code is ErrorCode.GPU_OUT_OF_MEMORY

    def test_frames_without_timestamps_are_refused(self, config) -> None:
        provider = OllamaVisionProvider(config.models.vision, gpu=config.gpu)
        with pytest.raises(ValidationError):
            provider.describe((Path("a.jpg"), Path("b.jpg")), (1.0,))

    def test_an_empty_batch_is_a_no_op(self, config) -> None:
        provider = OllamaVisionProvider(config.models.vision, gpu=config.gpu)
        assert provider.describe((), ()) == ()


class TestFakeVisionProvider:
    def test_it_satisfies_the_provider_protocol(self) -> None:
        from ai.providers.base import VisionProvider

        assert isinstance(FakeVisionProvider(), VisionProvider)

    def test_the_same_frame_always_yields_the_same_observation(self, tmp_path: Path) -> None:
        path = tmp_path / "frame.jpg"
        first = FakeVisionProvider().describe((path,), (1.0,))
        second = FakeVisionProvider().describe((path,), (1.0,))
        assert first[0].description == second[0].description

    def test_it_records_every_frame_it_was_handed(self, tmp_path: Path) -> None:
        provider = FakeVisionProvider()
        provider.describe((tmp_path / "a.jpg", tmp_path / "b.jpg"), (1.0, 2.0))
        assert len(provider.described_frames) == 2
        assert provider.batch_sizes == [2]


class TestSceneModel:
    def test_boundaries_exclude_the_opening_scene(self) -> None:
        result = SceneResult(
            scenes=(
                Scene(index=0, start_seconds=0.0, end_seconds=3.0),
                Scene(index=1, start_seconds=3.0, end_seconds=6.0, change_score=40.0),
            ),
            duration_seconds=6.0,
            detector="content",
            threshold=27.0,
        )
        assert result.boundaries == (3.0,)
        assert result.keyframe_times() == (1.5, 4.5)
        assert result.scene_at(4.0).index == 1
        assert result.scene_at(99.0) is None

    def test_change_regions_apply_the_roll_and_clamp(self) -> None:
        result = SceneResult(
            scenes=(
                Scene(index=0, start_seconds=0.0, end_seconds=3.0),
                Scene(index=1, start_seconds=3.0, end_seconds=6.0, change_score=40.0),
            ),
            duration_seconds=6.0,
            detector="content",
            threshold=27.0,
        )
        assert scene_change_regions(result, pre_roll=10.0, post_roll=10.0) == [(0.0, 6.0)]

    def test_a_missing_file_is_a_typed_error(self, tmp_path: Path, config) -> None:
        from backend.core.errors import AnalysisError

        with pytest.raises(AnalysisError) as exc_info:
            detect_scenes(tmp_path / "absent.mp4", config.analysis.scenes)
        assert exc_info.value.code is ErrorCode.SCENE_DETECTION_FAILED
