"""Media ingestion tests (SPEC sections 4, 42, 48)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.errors import ErrorCode, MediaError
from backend.core.models.enums import MediaRole, MediaState, ProjectStatus, VideoMode
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.services.job_manager import PER_MEDIA_STAGES, JobManager
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager

pytestmark = pytest.mark.unit


@pytest.fixture
def project_id(project_manager: ProjectManager) -> str:
    return project_manager.create(
        ProjectCreate(
            name="Ranked",
            target_duration_seconds=1200,
            mode=VideoMode.STORY,
            game="generic",
        )
    ).id


class TestImport:
    def test_registers_the_file(
        self, media_service: MediaIngestionService, project_id: str, sample_video: Path
    ) -> None:
        media = media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        assert media.id.startswith("media-")
        assert media.state is MediaState.REGISTERED
        assert media.container == ".mp4"
        assert media.size_bytes == sample_video.stat().st_size

    def test_computes_a_checksum(
        self, media_service: MediaIngestionService, project_id: str, sample_video: Path
    ) -> None:
        # The checksum is the video_hash component of every cache key (§48).
        media = media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        assert len(media.checksum) == 64

    def test_references_the_original_by_default(
        self, media_service: MediaIngestionService, project_id: str, sample_video: Path
    ) -> None:
        media = media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        assert media.source_path == str(sample_video)
        assert sample_video.exists()

    def test_can_copy_into_the_project(
        self,
        media_service: MediaIngestionService,
        project_manager: ProjectManager,
        project_id: str,
        sample_video: Path,
    ) -> None:
        media = media_service.import_media(
            project_id, MediaImport(path=str(sample_video), copy_into_project=True)
        )
        source_dir = project_manager.paths_for(project_id).source
        assert Path(media.source_path).parent == source_dir
        assert sample_video.exists(), "the original must remain untouched (§42)"

    def test_queues_the_analysis_chain(
        self,
        media_service: MediaIngestionService,
        job_manager: JobManager,
        project_id: str,
        sample_video: Path,
    ) -> None:
        media = media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        stages = {
            job.stage for job in job_manager.list_jobs(project_id) if job.media_id == media.id
        }
        assert stages == set(PER_MEDIA_STAGES)

    def test_moves_the_project_to_importing(
        self,
        media_service: MediaIngestionService,
        project_manager: ProjectManager,
        project_id: str,
        sample_video: Path,
    ) -> None:
        media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        assert project_manager.get(project_id).status is ProjectStatus.IMPORTING


class TestValidation:
    def test_missing_file(
        self, media_service: MediaIngestionService, project_id: str, tmp_path: Path
    ) -> None:
        # Derived from tmp_path rather than written literally: "/nowhere/clip.mp4"
        # is drive-relative on Windows, so it would be rejected as non-absolute
        # before the existence check ever runs.
        absent = tmp_path / "nowhere" / "clip.mp4"
        with pytest.raises(MediaError) as exc_info:
            media_service.import_media(project_id, MediaImport(path=str(absent)))
        assert exc_info.value.code is ErrorCode.MEDIA_NOT_FOUND

    def test_relative_path_is_rejected(
        self, media_service: MediaIngestionService, project_id: str
    ) -> None:
        with pytest.raises(MediaError) as exc_info:
            media_service.import_media(project_id, MediaImport(path="clip.mp4"))
        assert exc_info.value.code is ErrorCode.PATH_NOT_ALLOWED

    def test_directory_is_rejected(
        self, media_service: MediaIngestionService, project_id: str, tmp_path: Path
    ) -> None:
        with pytest.raises(MediaError):
            media_service.import_media(project_id, MediaImport(path=str(tmp_path)))

    def test_unsupported_container(
        self, media_service: MediaIngestionService, project_id: str, tmp_path: Path
    ) -> None:
        bad = tmp_path / "notes.txt"
        bad.write_text("not a video", encoding="utf-8")
        with pytest.raises(MediaError) as exc_info:
            media_service.import_media(project_id, MediaImport(path=str(bad)))
        assert exc_info.value.code is ErrorCode.UNSUPPORTED_CONTAINER

    def test_empty_file(
        self, media_service: MediaIngestionService, project_id: str, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty.mp4"
        empty.touch()
        with pytest.raises(MediaError) as exc_info:
            media_service.import_media(project_id, MediaImport(path=str(empty)))
        assert exc_info.value.code is ErrorCode.MEDIA_CORRUPTED

    @pytest.mark.parametrize("extension", [".mp4", ".mov", ".mkv", ".webm", ".avi"])
    def test_all_spec_containers_are_accepted(
        self,
        media_service: MediaIngestionService,
        project_id: str,
        tmp_path: Path,
        extension: str,
    ) -> None:
        path = tmp_path / f"clip{extension}"
        path.write_bytes(b"\x00" * 256)
        assert media_service.import_media(project_id, MediaImport(path=str(path)))

    def test_audio_only_accepted_for_microphone_role(
        self, media_service: MediaIngestionService, project_id: str, tmp_path: Path
    ) -> None:
        mic = tmp_path / "mic.wav"
        mic.write_bytes(b"RIFF" + b"\x00" * 256)
        media = media_service.import_media(
            project_id, MediaImport(path=str(mic), role=MediaRole.MICROPHONE)
        )
        assert media.role is MediaRole.MICROPHONE

    def test_audio_only_rejected_for_gameplay_role(
        self, media_service: MediaIngestionService, project_id: str, tmp_path: Path
    ) -> None:
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF" + b"\x00" * 256)
        with pytest.raises(MediaError) as exc_info:
            media_service.import_media(
                project_id, MediaImport(path=str(audio), role=MediaRole.GAMEPLAY)
            )
        assert exc_info.value.code is ErrorCode.UNSUPPORTED_CONTAINER


class TestDeduplication:
    def test_same_file_twice_is_rejected(
        self, media_service: MediaIngestionService, project_id: str, sample_video: Path
    ) -> None:
        media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        with pytest.raises(MediaError) as exc_info:
            media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        assert exc_info.value.code is ErrorCode.MEDIA_ALREADY_IMPORTED

    def test_identical_content_under_another_name_is_rejected(
        self, media_service: MediaIngestionService, project_id: str, sample_video: Path
    ) -> None:
        copy = sample_video.parent / "same_content.mp4"
        copy.write_bytes(sample_video.read_bytes())
        media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        with pytest.raises(MediaError):
            media_service.import_media(project_id, MediaImport(path=str(copy)))

    def test_different_projects_may_import_the_same_file(
        self,
        media_service: MediaIngestionService,
        project_manager: ProjectManager,
        project_id: str,
        sample_video: Path,
    ) -> None:
        other = project_manager.create(
            ProjectCreate(name="Other", target_duration_seconds=900, game="generic")
        )
        media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        assert media_service.import_media(other.id, MediaImport(path=str(sample_video)))


class TestRemoval:
    def test_referenced_original_is_never_deleted(
        self, media_service: MediaIngestionService, project_id: str, sample_video: Path
    ) -> None:
        media = media_service.import_media(project_id, MediaImport(path=str(sample_video)))
        media_service.remove_media(media.id, delete_file=True)
        assert sample_video.exists(), "a referenced original must survive removal (§42)"

    def test_project_copy_can_be_deleted(
        self, media_service: MediaIngestionService, project_id: str, sample_video: Path
    ) -> None:
        media = media_service.import_media(
            project_id, MediaImport(path=str(sample_video), copy_into_project=True)
        )
        copied = Path(media.source_path)
        media_service.remove_media(media.id, delete_file=True)
        assert not copied.exists()
        assert sample_video.exists()
