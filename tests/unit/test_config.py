"""Configuration loading tests (SPEC section 91)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.config.loader import CONFIG_FILES, load_config, reset_config_cache
from backend.config.schema import AppConfig
from backend.core.errors import ConfigurationError, ErrorCode
from backend.core.models.enums import HardwareProfile, PublishTarget

pytestmark = pytest.mark.unit


@pytest.fixture
def config_copy(config_dir: Path, tmp_path: Path) -> Path:
    """A writable copy of the shipped configuration."""
    destination = tmp_path / "config"
    shutil.copytree(config_dir, destination)
    return destination


class TestShippedConfiguration:
    def test_loads_and_validates(self, config: AppConfig) -> None:
        assert config.application.name
        assert config.output.min_minutes == 10
        assert config.output.max_minutes == 60

    def test_every_declared_file_exists(self, config_dir: Path) -> None:
        for filename in CONFIG_FILES:
            assert (config_dir / filename).is_file(), f"{filename} is missing"

    def test_duration_policy_matches_the_product_band(self, config: AppConfig) -> None:
        policy = config.duration_policy
        assert policy.min_seconds == 600
        assert policy.max_seconds == 3600

    def test_all_hardware_profiles_are_defined(self, config: AppConfig) -> None:
        for profile in HardwareProfile:
            assert profile in config.hardware.profiles

    def test_gpu_budget_leaves_headroom(self, config: AppConfig) -> None:
        assert config.gpu.usable_vram_mb < config.gpu.total_vram_mb

    def test_publishing_ships_both_targets_and_no_credentials(self, config: AppConfig) -> None:
        # YouTube shipped enabled on 2026-08-28 -- the auto-publish flag and
        # the Export screen's one-press button need the target to exist. What
        # must never ship is a credential: the client pair stays null, so an
        # unconfigured machine gets the remedy message, not an upload.
        assert PublishTarget.LOCAL_FILE in config.publishing.enabled_targets
        assert PublishTarget.YOUTUBE in config.publishing.enabled_targets
        assert config.publishing.youtube.enabled is True
        # The owner's client id may live in the local yaml (public by
        # design); the packager strips it, and test_package proves that.
        assert config.publishing.default_target is PublishTarget.LOCAL_FILE


class TestHardwareProfileSelection:
    def test_no_gpu_selects_low(self, config: AppConfig) -> None:
        assert config.hardware.select(None) is HardwareProfile.LOW

    def test_low_profile_disables_vision(self, config: AppConfig) -> None:
        assert config.hardware.profiles[HardwareProfile.LOW].vision_enabled is False

    def test_rtx_3070_selects_medium(self, config: AppConfig) -> None:
        assert config.hardware.select(8192) is HardwareProfile.MEDIUM

    def test_large_card_selects_high(self, config: AppConfig) -> None:
        assert config.hardware.select(24576) is HardwareProfile.HIGH

    def test_pinned_profile_overrides_detection(self, config: AppConfig) -> None:
        config.hardware.profile = "high"
        assert config.hardware.select(None) is HardwareProfile.HIGH


class TestEnvironmentOverrides:
    def test_override_applies(self, config_dir: Path) -> None:
        loaded = load_config(config_dir, env={"VAI__APPLICATION__API__PORT": "9123"})
        assert loaded.application.api.port == 9123

    def test_override_is_type_coerced(self, config_dir: Path) -> None:
        loaded = load_config(config_dir, env={"VAI__ANALYSIS__VISION__ENABLED": "false"})
        assert loaded.analysis.vision.enabled is False

    def test_numeric_override(self, config_dir: Path) -> None:
        loaded = load_config(config_dir, env={"VAI__ANALYSIS__CHUNK_SECONDS": "300"})
        assert loaded.analysis.chunk_seconds == 300.0

    def test_unrelated_variables_are_ignored(self, config_dir: Path) -> None:
        loaded = load_config(config_dir, env={"PATH": "/usr/bin", "HOME": "/root"})
        assert loaded.application.api.port == 8765

    def test_explicit_overrides_win_over_environment(self, config_dir: Path) -> None:
        loaded = load_config(
            config_dir,
            env={"VAI__APPLICATION__API__PORT": "9123"},
            overrides={"application": {"api": {"port": 7000}}},
        )
        assert loaded.application.api.port == 7000

    def test_override_that_breaks_a_rule_is_rejected(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError) as exc_info:
            load_config(config_dir, env={"VAI__OUTPUT__MIN_MINUTES": "2"})
        assert exc_info.value.code is ErrorCode.CONFIG_INVALID


class TestFailureModes:
    def test_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError) as exc_info:
            load_config(tmp_path / "nope")
        assert exc_info.value.code is ErrorCode.CONFIG_NOT_FOUND

    def test_missing_file(self, config_copy: Path) -> None:
        (config_copy / "qa.yaml").unlink()
        with pytest.raises(ConfigurationError) as exc_info:
            load_config(config_copy)
        assert exc_info.value.code is ErrorCode.CONFIG_NOT_FOUND

    def test_malformed_yaml(self, config_copy: Path) -> None:
        (config_copy / "qa.yaml").write_text("qa:\n  - [unclosed", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="Malformed YAML"):
            load_config(config_copy)

    def test_unknown_key_is_rejected_not_ignored(self, config_copy: Path) -> None:
        path = config_copy / "application.yaml"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n  typo_key: 1\n", encoding="utf-8"
        )
        with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
            load_config(config_copy)

    def test_section_in_the_wrong_file_is_rejected(self, config_copy: Path) -> None:
        path = config_copy / "qa.yaml"
        path.write_text(
            path.read_text(encoding="utf-8") + "\naudio:\n  analysis:\n    channels: 1\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigurationError, match="unexpected top-level key"):
            load_config(config_copy)

    def test_top_level_list_is_rejected(self, config_copy: Path) -> None:
        (config_copy / "qa.yaml").write_text("- one\n- two\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="mapping at the top level"):
            load_config(config_copy)


class TestSemanticValidation:
    """Rules that catch a configuration which parses but cannot work."""

    def test_chunk_overlap_must_be_smaller_than_the_chunk(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="chunk_overlap_seconds"):
            load_config(config_dir, overrides={"analysis": {"chunk_overlap_seconds": 900}})

    def test_frame_sampling_must_get_denser(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="denser"):
            load_config(
                config_dir,
                overrides={"analysis": {"frame_sampling": {"base_interval_seconds": 0.1}}},
            )

    def test_cascade_requires_candidate_detectors(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="candidate_detectors"):
            load_config(
                config_dir, overrides={"analysis": {"vision": {"candidate_detectors": []}}}
            )

    def test_default_mode_must_be_enabled(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="default_mode"):
            load_config(config_dir, overrides={"output": {"modes": ["best_moments"]}})

    def test_reserved_vram_cannot_exceed_total(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="reserved_vram_mb"):
            load_config(config_dir, overrides={"gpu": {"reserved_vram_mb": 99999}})

    def test_youtube_cannot_be_enabled_without_being_listed(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="enabled_targets"):
            load_config(
                config_dir,
                overrides={
                    "publishing": {
                        "enabled_targets": ["local_file"],
                        "youtube": {"enabled": True},
                    }
                },
            )

    def test_default_publish_target_must_be_enabled(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="default_target"):
            load_config(
                config_dir,
                overrides={
                    "publishing": {
                        "enabled_targets": ["local_file"],
                        "youtube": {"enabled": False},
                        "default_target": "youtube",
                    }
                },
            )


class TestCaching:
    def test_reset_clears_the_cache(self, config_dir: Path) -> None:
        from backend.config.loader import get_config

        reset_config_cache()
        first = get_config(config_dir)
        assert get_config(config_dir) is first
        reset_config_cache()
        assert get_config(config_dir) is not first
