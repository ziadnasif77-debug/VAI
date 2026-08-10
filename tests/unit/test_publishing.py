"""Publishing-layer tests.

The MVP publishes to a local file. YouTube auto-publish is planned, so these
tests pin the seam: a declared-but-unimplemented target must fail clearly, and
the metadata model must already satisfy the constraints a real upload imposes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from backend.core.errors import ErrorCode
from backend.core.models.enums import PublishStatus, PublishTarget, PublishVisibility
from backend.core.models.publishing import (
    MAX_TITLE_LENGTH,
    Chapter,
    PublishRequest,
    VideoMetadata,
)
from backend.publishing.base import PublisherRegistry, PublishError
from backend.publishing.local_file import LocalFilePublisher

pytestmark = pytest.mark.unit


@pytest.fixture
def render_file(tmp_path: Path) -> Path:
    path = tmp_path / "render.mp4"
    path.write_bytes(b"\x00" * 1024)
    return path


class TestRegistry:
    def test_local_file_is_registered(self) -> None:
        registry = PublisherRegistry()
        registry.register(LocalFilePublisher())
        assert registry.is_available(PublishTarget.LOCAL_FILE)

    def test_unimplemented_target_fails_clearly(self) -> None:
        registry = PublisherRegistry()
        registry.register(LocalFilePublisher())
        with pytest.raises(PublishError) as exc_info:
            registry.get(PublishTarget.YOUTUBE)
        assert exc_info.value.code is ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED
        assert exc_info.value.recoverable is False

    def test_double_registration_is_refused(self) -> None:
        registry = PublisherRegistry()
        registry.register(LocalFilePublisher())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(LocalFilePublisher())

    def test_replace_is_explicit(self) -> None:
        registry = PublisherRegistry()
        registry.register(LocalFilePublisher())
        registry.register(LocalFilePublisher(), replace=True)
        assert registry.available() == (PublishTarget.LOCAL_FILE,)


class TestLocalFilePublisher:
    def test_writes_to_an_explicit_path(self, tmp_path: Path, render_file: Path) -> None:
        destination = tmp_path / "out" / "final.mp4"
        result = LocalFilePublisher().publish(
            PublishRequest(project_id="proj-1", render_id="rnd-1", destination=str(destination)),
            render_file,
        )
        assert result.status is PublishStatus.COMPLETED
        assert destination.is_file()

    def test_names_the_file_from_the_title(self, tmp_path: Path, render_file: Path) -> None:
        out_dir = tmp_path / "exports"
        out_dir.mkdir()
        result = LocalFilePublisher().publish(
            PublishRequest(
                project_id="proj-1",
                render_id="rnd-1",
                destination=str(out_dir),
                metadata=VideoMetadata(title="INSANE 1v5 Clutch"),
            ),
            render_file,
        )
        assert Path(result.output_path or "").name == "INSANE 1v5 Clutch.mp4"

    def test_title_is_sanitised_for_windows(self, tmp_path: Path, render_file: Path) -> None:
        out_dir = tmp_path / "exports"
        out_dir.mkdir()
        result = LocalFilePublisher().publish(
            PublishRequest(
                project_id="proj-1",
                render_id="rnd-1",
                destination=str(out_dir),
                metadata=VideoMetadata(title='clip: "best" 1/5'),
            ),
            render_file,
        )
        name = Path(result.output_path or "").name
        assert not any(character in name for character in '<>:"/\\|?*')

    def test_leaves_the_render_in_place(self, tmp_path: Path, render_file: Path) -> None:
        LocalFilePublisher().publish(
            PublishRequest(
                project_id="proj-1",
                render_id="rnd-1",
                destination=str(tmp_path / "copy.mp4"),
            ),
            render_file,
        )
        assert render_file.exists()

    def test_missing_render_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(PublishError) as exc_info:
            LocalFilePublisher().publish(
                PublishRequest(
                    project_id="proj-1",
                    render_id="rnd-1",
                    destination=str(tmp_path / "out.mp4"),
                ),
                tmp_path / "missing.mp4",
            )
        assert exc_info.value.code is ErrorCode.MEDIA_NOT_FOUND

    def test_no_destination_and_no_default_is_reported(self, render_file: Path) -> None:
        with pytest.raises(PublishError) as exc_info:
            LocalFilePublisher().publish(
                PublishRequest(project_id="proj-1", render_id="rnd-1"), render_file
            )
        assert exc_info.value.code is ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED

    def test_default_directory_is_used(self, tmp_path: Path, render_file: Path) -> None:
        publisher = LocalFilePublisher(default_directory=tmp_path / "default")
        result = publisher.publish(
            PublishRequest(
                project_id="proj-1", render_id="rnd-1", metadata=VideoMetadata(title="Clip")
            ),
            render_file,
        )
        assert Path(result.output_path or "").parent == tmp_path / "default"

    def test_publishing_onto_itself_is_refused(self, render_file: Path) -> None:
        with pytest.raises(PublishError, match="render itself"):
            LocalFilePublisher().publish(
                PublishRequest(
                    project_id="proj-1", render_id="rnd-1", destination=str(render_file)
                ),
                render_file,
            )


class TestVideoMetadata:
    """Limits are YouTube's, applied to every target so nothing becomes invalid
    the day a real destination is enabled."""

    def test_defaults_to_private(self) -> None:
        assert VideoMetadata().visibility is PublishVisibility.PRIVATE

    def test_title_length_is_bounded(self) -> None:
        with pytest.raises(PydanticValidationError):
            VideoMetadata(title="x" * (MAX_TITLE_LENGTH + 1))

    def test_description_length_is_bounded(self) -> None:
        with pytest.raises(PydanticValidationError):
            VideoMetadata(description="x" * 5001)

    def test_tag_budget_is_enforced(self) -> None:
        with pytest.raises(PydanticValidationError, match="character budget"):
            VideoMetadata(tags=["x" * 100 for _ in range(10)])

    def test_blank_tags_are_dropped(self) -> None:
        assert VideoMetadata(tags=["valorant", "  ", ""]).tags == ["valorant"]

    def test_chapters_must_start_at_zero(self) -> None:
        with pytest.raises(PydanticValidationError, match="must start at 0"):
            VideoMetadata(chapters=[Chapter(title="Intro", start_seconds=12)])

    def test_chapters_must_be_ordered(self) -> None:
        with pytest.raises(PydanticValidationError, match="ordered"):
            VideoMetadata(
                chapters=[
                    Chapter(title="Intro", start_seconds=0),
                    Chapter(title="Late", start_seconds=300),
                    Chapter(title="Early", start_seconds=120),
                ]
            )

    def test_duplicate_chapter_timestamps_are_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="same timestamp"):
            VideoMetadata(
                chapters=[
                    Chapter(title="A", start_seconds=0),
                    Chapter(title="B", start_seconds=0),
                ]
            )

    def test_valid_chapters_are_accepted(self) -> None:
        metadata = VideoMetadata(
            chapters=[
                Chapter(title="Warm-up", start_seconds=0),
                Chapter(title="First clutch", start_seconds=180),
            ]
        )
        assert len(metadata.chapters) == 2
