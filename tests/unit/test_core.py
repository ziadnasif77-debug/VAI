"""Core utility tests: errors, logging, ids, filesystem safety, cache keys."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from backend.core.cache_keys import CacheNamespace, build_cache_key
from backend.core.errors import (
    ErrorCode,
    GamingEditorError,
    MediaError,
    ValidationError,
    http_status_for,
    is_recoverable,
)
from backend.core.fs import (
    atomic_write_json,
    file_checksum,
    is_within,
    read_json,
    sanitize_filename,
    slugify,
)
from backend.core.ids import is_valid_id, new_id, sequence_id
from backend.core.logging import (
    LogChannel,
    configure_logging,
    get_logger,
    log_context,
    log_duration,
    shutdown_logging,
)
from backend.core.versions import PROMPT_VERSIONS, prompt_version, version_manifest

pytestmark = pytest.mark.unit


class TestErrors:
    def test_carries_a_typed_code(self) -> None:
        error = MediaError("gone", code=ErrorCode.MEDIA_NOT_FOUND, details={"path": "/x"})
        assert error.code is ErrorCode.MEDIA_NOT_FOUND
        assert error.to_dict()["error_code"] == "MEDIA_NOT_FOUND"
        assert error.to_dict()["details"] == {"path": "/x"}

    def test_default_code_per_subclass(self) -> None:
        assert ValidationError("x").code is ErrorCode.SCHEMA_VALIDATION_FAILED

    def test_recoverability_follows_the_code(self) -> None:
        assert is_recoverable(ErrorCode.GPU_OUT_OF_MEMORY) is True
        assert is_recoverable(ErrorCode.MEDIA_CORRUPTED) is False
        assert is_recoverable(ErrorCode.UNSUPPORTED_CONTAINER) is False

    def test_unconfigured_publish_target_is_not_retried(self) -> None:
        # Retrying a missing implementation would just loop.
        assert is_recoverable(ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED) is False

    def test_recoverability_can_be_narrowed(self) -> None:
        error = GamingEditorError(
            "no more attempts", code=ErrorCode.GPU_OUT_OF_MEMORY, recoverable=False
        )
        assert error.recoverable is False

    def test_http_status_mapping(self) -> None:
        assert http_status_for(ErrorCode.PROJECT_NOT_FOUND) == 404
        assert http_status_for(ErrorCode.INVALID_TARGET_DURATION) == 422
        assert http_status_for(ErrorCode.MEDIA_ALREADY_IMPORTED) == 409
        assert http_status_for(ErrorCode.INTERNAL_ERROR) == 500

    def test_every_code_has_a_unique_value(self) -> None:
        values = [code.value for code in ErrorCode]
        assert len(values) == len(set(values))


class TestLogging:
    def test_writes_json_with_the_required_fields(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, console=False)
        get_logger("test.component").info("hello")
        shutdown_logging()

        lines = (tmp_path / "application.log").read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[-1])
        for field in ("timestamp", "level", "component", "job_id", "project_id", "message"):
            assert field in record
        assert record["component"] == "test.component"
        assert record["message"] == "hello"

    def test_creates_all_five_channel_files(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, console=False)
        for channel in LogChannel:
            get_logger("c", channel).info("x")
        shutdown_logging()

        for channel in LogChannel:
            assert (tmp_path / channel.filename).is_file()

    def test_channels_do_not_leak_into_each_other(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, console=False)
        get_logger("render.ffmpeg", LogChannel.RENDERING).info("rendering only")
        get_logger("qa.technical", LogChannel.QA).info("qa only")
        shutdown_logging()

        rendering_log = (tmp_path / "rendering.log").read_text(encoding="utf-8")
        qa_log = (tmp_path / "qa.log").read_text(encoding="utf-8")
        assert "rendering only" in rendering_log
        assert "rendering only" not in qa_log
        assert "qa only" in qa_log
        assert "qa only" not in rendering_log

    def test_context_attaches_ids(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, console=False)
        with log_context(project_id="proj-1", job_id="job-1"):
            get_logger("test").info("inside")
        shutdown_logging()

        record = json.loads(
            (tmp_path / "application.log").read_text(encoding="utf-8").splitlines()[-1]
        )
        assert record["project_id"] == "proj-1"
        assert record["job_id"] == "job-1"

    def test_context_is_restored_after_the_block(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, console=False)
        with log_context(project_id="proj-1"):
            pass
        get_logger("test").info("outside")
        shutdown_logging()

        record = json.loads(
            (tmp_path / "application.log").read_text(encoding="utf-8").splitlines()[-1]
        )
        assert record["project_id"] is None

    def test_log_duration_records_elapsed_time(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, console=False)
        with log_duration(get_logger("test"), "did work") as fields:
            fields["items"] = 3
        shutdown_logging()

        record = json.loads(
            (tmp_path / "application.log").read_text(encoding="utf-8").splitlines()[-1]
        )
        assert record["duration"] is not None
        assert record["context"]["items"] == 3

    def test_log_duration_records_the_error_code_on_failure(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, console=False)
        with pytest.raises(MediaError), log_duration(get_logger("test"), "failed work"):
            raise MediaError("bad", code=ErrorCode.MEDIA_CORRUPTED)
        shutdown_logging()

        record = json.loads(
            (tmp_path / "application.log").read_text(encoding="utf-8").splitlines()[-1]
        )
        assert record["level"] == "ERROR"
        assert record["error_code"] == "MEDIA_CORRUPTED"

    def test_configure_is_idempotent(self, tmp_path: Path) -> None:
        configure_logging(tmp_path, console=False)
        configure_logging(tmp_path, console=False)
        channel_logger = logging.getLogger("vai.application")
        assert len(channel_logger.handlers) == 1
        shutdown_logging()


class TestIds:
    def test_prefix_and_shape(self) -> None:
        assert new_id("project").startswith("proj-")
        assert is_valid_id(new_id("moment"))

    def test_ids_are_unique(self) -> None:
        assert len({new_id("game_event") for _ in range(500)}) == 500

    def test_unknown_entity_is_refused(self) -> None:
        with pytest.raises(KeyError, match="Register its prefix"):
            new_id("not_an_entity")

    def test_sequence_id_is_stable(self) -> None:
        assert sequence_id("evt", 1) == "evt-001"
        assert sequence_id("evt", 42) == "evt-042"

    def test_sequence_id_rejects_negatives(self) -> None:
        with pytest.raises(ValueError):
            sequence_id("evt", -1)


class TestFilesystemSafety:
    """The target platform is Windows; the build host is Linux (§R-8)."""

    @pytest.mark.parametrize(
        ("raw", "forbidden"),
        [
            ('clip<>:"/\\|?*.mp4', '<>:"/\\|?*'),
            ("a\x00b", "\x00"),
        ],
    )
    def test_strips_characters_windows_rejects(self, raw: str, forbidden: str) -> None:
        cleaned = sanitize_filename(raw)
        assert not any(character in cleaned for character in forbidden)

    @pytest.mark.parametrize("name", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9"])
    def test_avoids_reserved_device_names(self, name: str) -> None:
        assert sanitize_filename(name) != name

    def test_trailing_dots_and_spaces_are_trimmed(self) -> None:
        assert sanitize_filename("clip.  ") == "clip"

    def test_blank_falls_back(self) -> None:
        assert sanitize_filename("   ") == "untitled"

    def test_length_is_bounded(self) -> None:
        assert len(sanitize_filename("x" * 500)) <= 120

    def test_unicode_survives(self) -> None:
        assert sanitize_filename("لقطة رائعة") == "لقطة رائعة"

    def test_slugify(self) -> None:
        assert slugify("Valorant: Ranked Session #3") == "valorant-ranked-session-3"

    def test_is_within_detects_traversal(self, tmp_path: Path) -> None:
        assert is_within(tmp_path / "a" / "b", tmp_path) is True
        assert is_within(tmp_path.parent, tmp_path) is False

    def test_atomic_write_leaves_no_temp_files(self, tmp_path: Path) -> None:
        target = tmp_path / "project.json"
        atomic_write_json(target, {"a": 1})
        assert read_json(target) == {"a": 1}
        assert list(tmp_path.glob(".*tmp")) == []

    def test_atomic_write_overwrites(self, tmp_path: Path) -> None:
        target = tmp_path / "project.json"
        atomic_write_json(target, {"v": 1})
        atomic_write_json(target, {"v": 2})
        assert read_json(target) == {"v": 2}

    def test_checksum_is_content_based(self, tmp_path: Path) -> None:
        first = tmp_path / "a.bin"
        second = tmp_path / "b.bin"
        first.write_bytes(b"same")
        second.write_bytes(b"same")
        assert file_checksum(first) == file_checksum(second)

        second.write_bytes(b"different")
        assert file_checksum(first) != file_checksum(second)


class TestCacheKeys:
    """§48: video_hash + model_version + analysis_version decide reuse."""

    def test_is_deterministic(self) -> None:
        first = build_cache_key(CacheNamespace.SCENES, video_hash="abc")
        second = build_cache_key(CacheNamespace.SCENES, video_hash="abc")
        assert first == second

    def test_video_hash_changes_the_key(self) -> None:
        assert build_cache_key(CacheNamespace.SCENES, video_hash="a") != build_cache_key(
            CacheNamespace.SCENES, video_hash="b"
        )

    def test_analysis_version_changes_the_key(self) -> None:
        assert build_cache_key(
            CacheNamespace.SCENES, video_hash="a", analysis_version=1
        ) != build_cache_key(CacheNamespace.SCENES, video_hash="a", analysis_version=2)

    def test_model_version_changes_the_key(self) -> None:
        base = {
            "video_hash": "a",
            "prompt_id": "vision_v1",
            "prompt_version": 1,
        }
        assert build_cache_key(
            CacheNamespace.VISION, model_version="m1", **base
        ) != build_cache_key(CacheNamespace.VISION, model_version="m2", **base)

    def test_prompt_version_changes_the_key(self) -> None:
        base = {"video_hash": "a", "model_version": "m1", "prompt_id": "vision_v1"}
        assert build_cache_key(
            CacheNamespace.VISION, prompt_version=1, **base
        ) != build_cache_key(CacheNamespace.VISION, prompt_version=2, **base)

    def test_chunks_cache_independently(self) -> None:
        # §7: a failure in one chunk must not invalidate the others.
        assert build_cache_key(
            CacheNamespace.TRANSCRIPT, video_hash="a", model_version="m", chunk_index=0
        ) != build_cache_key(
            CacheNamespace.TRANSCRIPT, video_hash="a", model_version="m", chunk_index=1
        )

    def test_parameter_order_does_not_matter(self) -> None:
        first = build_cache_key(
            CacheNamespace.SCENES, video_hash="a", params={"x": 1, "y": 2}
        )
        second = build_cache_key(
            CacheNamespace.SCENES, video_hash="a", params={"y": 2, "x": 1}
        )
        assert first == second

    def test_parameter_change_changes_the_key(self) -> None:
        assert build_cache_key(
            CacheNamespace.SCENES, video_hash="a", params={"threshold": 27}
        ) != build_cache_key(
            CacheNamespace.SCENES, video_hash="a", params={"threshold": 30}
        )

    def test_model_backed_namespace_demands_a_model_version(self) -> None:
        with pytest.raises(ValueError, match="model-backed"):
            build_cache_key(CacheNamespace.TRANSCRIPT, video_hash="a")

    def test_prompt_backed_namespace_demands_a_prompt_version(self) -> None:
        with pytest.raises(ValueError, match="prompt-backed"):
            build_cache_key(CacheNamespace.MOMENTS, video_hash="a", model_version="m")

    def test_key_is_namespaced(self) -> None:
        assert build_cache_key(CacheNamespace.PROXY, video_hash="a").startswith("proxy:")


class TestVersions:
    def test_manifest_has_every_field(self) -> None:
        manifest = version_manifest()
        assert {
            "application_version",
            "analysis_version",
            "schema_version",
            "prompt_versions",
        } <= set(manifest)

    def test_unregistered_prompt_is_refused(self) -> None:
        # An unversioned prompt would silently poison the analysis cache.
        assert "not_a_prompt" not in PROMPT_VERSIONS
        with pytest.raises(KeyError, match="Register it"):
            prompt_version("not_a_prompt")
