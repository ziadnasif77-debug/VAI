"""Project manager tests (SPEC sections 42, 43, 127)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from backend.config.paths import PROJECT_SUBDIRECTORIES
from backend.core.errors import ErrorCode, NotFoundError, ValidationError
from backend.core.models.enums import JobStage, ProjectStatus, VideoMode
from backend.core.models.jobs import ANALYSIS_STAGES, EDIT_STAGES
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate, ProjectUpdate
from backend.services.job_manager import JobManager
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager

pytestmark = pytest.mark.unit

_DEFAULTS = {
    "name": "Valorant ranked session",
    "target_duration_seconds": 1200,
    "mode": VideoMode.STORY,
    "game": "valorant",
    "resolution": 1080,
    "fps": 60,
    "language": "auto",
}


def make_request(**overrides) -> ProjectCreate:
    return ProjectCreate(**{**_DEFAULTS, **overrides})


def unvalidated_request(**overrides) -> ProjectCreate:
    """Build a request that skips model validation.

    Used to prove the service validates independently: the API's model is one
    layer of defence, and a value that slips past it must still be rejected.
    """
    return ProjectCreate.model_construct(**{**_DEFAULTS, **overrides})


class TestCreate:
    def test_creates_and_persists(self, project_manager: ProjectManager) -> None:
        project = project_manager.create(make_request())
        assert project.id.startswith("proj-")
        assert project_manager.get(project.id).name == "Valorant ranked session"

    def test_creates_the_full_directory_tree(self, project_manager: ProjectManager) -> None:
        project = project_manager.create(make_request())
        paths = project_manager.paths_for(project)
        for name in PROJECT_SUBDIRECTORIES:
            assert (paths.root / name).is_dir(), f"{name}/ was not created"

    def test_writes_the_manifest(self, project_manager: ProjectManager) -> None:
        project = project_manager.create(make_request())
        manifest = project_manager.read_manifest(project.id)
        assert manifest.project.id == project.id
        assert manifest.project.analysis_version == project.analysis_version

    def test_starts_in_created_status(self, project_manager: ProjectManager) -> None:
        assert project_manager.create(make_request()).status is ProjectStatus.CREATED

    def test_records_version_provenance(self, project_manager: ProjectManager) -> None:
        project = project_manager.create(make_request())
        assert project.application_version
        assert project.analysis_version >= 1
        assert project.schema_version >= 1

    @pytest.mark.parametrize("seconds", [599, 3601])
    def test_request_model_rejects_out_of_band_duration(self, seconds: int) -> None:
        # First line of defence: the request model refuses the value outright.
        with pytest.raises(PydanticValidationError):
            make_request(target_duration_seconds=seconds)

    @pytest.mark.parametrize("seconds", [599, 3601, 10])
    def test_service_rejects_out_of_band_duration(
        self, project_manager: ProjectManager, seconds: int
    ) -> None:
        # Second line of defence: the service validates against the configured
        # policy even when the request bypasses model validation.
        with pytest.raises(ValidationError) as exc_info:
            project_manager.create(unvalidated_request(target_duration_seconds=seconds))
        assert exc_info.value.code is ErrorCode.INVALID_TARGET_DURATION

    def test_rejects_unsupported_fps(self, project_manager: ProjectManager) -> None:
        with pytest.raises(ValidationError) as exc_info:
            project_manager.create(unvalidated_request(resolution=1080, fps=24))
        assert exc_info.value.code is ErrorCode.BUSINESS_VALIDATION_FAILED

    def test_leaves_no_directory_behind_when_creation_fails(
        self, project_manager: ProjectManager
    ) -> None:
        before = list(project_manager.paths.projects_dir.iterdir())
        with pytest.raises(ValidationError):
            project_manager.create(unvalidated_request(target_duration_seconds=10))
        assert list(project_manager.paths.projects_dir.iterdir()) == before


class TestReads:
    def test_missing_project_raises_typed_error(self, project_manager: ProjectManager) -> None:
        with pytest.raises(NotFoundError) as exc_info:
            project_manager.get("proj-000000000000")
        assert exc_info.value.code is ErrorCode.PROJECT_NOT_FOUND

    def test_list_is_newest_first(self, project_manager: ProjectManager) -> None:
        first = project_manager.create(make_request(name="First"))
        second = project_manager.create(make_request(name="Second"))
        # Touch the older project so its updated_at moves ahead.
        project_manager.set_status(first.id, ProjectStatus.IMPORTING)
        listed = [item.id for item in project_manager.list()]
        assert listed[0] == first.id
        assert second.id in listed

    def test_list_filters_by_status(self, project_manager: ProjectManager) -> None:
        kept = project_manager.create(make_request(name="Kept"))
        project_manager.create(make_request(name="Other"))
        project_manager.set_status(kept.id, ProjectStatus.RENDERING)
        results = project_manager.list(status=ProjectStatus.RENDERING)
        assert [item.id for item in results] == [kept.id]

    def test_count(self, project_manager: ProjectManager) -> None:
        project_manager.create(make_request(name="A"))
        project_manager.create(make_request(name="B"))
        assert project_manager.count() == 2


class TestReEditPreservesAnalysis:
    """§127: after any edit, regenerate without re-analysing the source."""

    def _queue_everything(
        self,
        jobs: JobManager,
        media_service: MediaIngestionService,
        project_id: str,
        video: Path,
    ) -> None:
        # Import queues the per-media chain; analyze queues the project stages.
        media_service.import_media(project_id, MediaImport(path=str(video)))
        jobs.queue_project_stages(project_id)

    def test_changing_target_duration_drops_only_edit_stages(
        self,
        project_manager: ProjectManager,
        job_manager: JobManager,
        media_service: MediaIngestionService,
        sample_video: Path,
    ) -> None:
        project = project_manager.create(make_request(target_duration_seconds=1200))
        self._queue_everything(job_manager, media_service, project.id, sample_video)

        project_manager.update(
            project.id, ProjectUpdate(target_duration_seconds=900)
        )

        remaining = {job.stage for job in job_manager.list_jobs(project.id)}
        assert not (remaining & set(EDIT_STAGES)), "edit stages should be re-queued"
        analysis_present = remaining & set(ANALYSIS_STAGES)
        assert analysis_present, "analysis stages must be preserved"

    def test_changing_mode_drops_only_edit_stages(
        self,
        project_manager: ProjectManager,
        job_manager: JobManager,
        media_service: MediaIngestionService,
        sample_video: Path,
    ) -> None:
        project = project_manager.create(make_request(mode=VideoMode.STORY))
        self._queue_everything(job_manager, media_service, project.id, sample_video)

        project_manager.update(project.id, ProjectUpdate(mode=VideoMode.BEST_MOMENTS))

        remaining = {job.stage for job in job_manager.list_jobs(project.id)}
        assert JobStage.TRANSCRIPT in remaining
        assert JobStage.STORY not in remaining

    def test_edit_bumps_the_project_version(self, project_manager: ProjectManager) -> None:
        project = project_manager.create(make_request())
        updated = project_manager.update(
            project.id, ProjectUpdate(target_duration_seconds=1800)
        )
        assert updated.version == project.version + 1

    def test_renaming_does_not_invalidate_anything(
        self,
        project_manager: ProjectManager,
        job_manager: JobManager,
        media_service: MediaIngestionService,
        sample_video: Path,
    ) -> None:
        project = project_manager.create(make_request())
        self._queue_everything(job_manager, media_service, project.id, sample_video)
        before = len(job_manager.list_jobs(project.id))

        updated = project_manager.update(project.id, ProjectUpdate(name="Renamed"))

        assert updated.name == "Renamed"
        assert updated.version == project.version
        assert len(job_manager.list_jobs(project.id)) == before

    def test_invalidate_from_stage_keeps_earlier_stages(
        self,
        project_manager: ProjectManager,
        job_manager: JobManager,
        media_service: MediaIngestionService,
        sample_video: Path,
    ) -> None:
        project = project_manager.create(make_request())
        self._queue_everything(job_manager, media_service, project.id, sample_video)

        affected = project_manager.invalidate_from(project.id, JobStage.VISION)

        assert JobStage.VISION in affected
        assert JobStage.GAME_EVENTS in affected
        assert JobStage.TRANSCRIPT not in affected
        remaining = {job.stage for job in job_manager.list_jobs(project.id)}
        assert JobStage.TRANSCRIPT in remaining
        assert JobStage.VISION not in remaining


class TestUpdateValidation:
    def test_request_model_rejects_out_of_band_duration(self) -> None:
        with pytest.raises(PydanticValidationError):
            ProjectUpdate(target_duration_seconds=59)

    def test_service_rejects_out_of_band_duration(
        self, project_manager: ProjectManager
    ) -> None:
        project = project_manager.create(make_request())
        change = ProjectUpdate.model_construct(target_duration_seconds=59)
        with pytest.raises(ValidationError) as exc_info:
            project_manager.update(project.id, change)
        assert exc_info.value.code is ErrorCode.INVALID_TARGET_DURATION

    def test_empty_update_is_a_no_op(self, project_manager: ProjectManager) -> None:
        project = project_manager.create(make_request())
        assert project_manager.update(project.id, ProjectUpdate()).version == project.version


class TestDelete:
    def test_removes_the_row_but_keeps_files_by_default(
        self, project_manager: ProjectManager
    ) -> None:
        project = project_manager.create(make_request())
        directory = project_manager.paths_for(project).root

        project_manager.delete(project.id)

        with pytest.raises(NotFoundError):
            project_manager.get(project.id)
        assert directory.exists(), "recordings must not disappear implicitly"

    def test_can_delete_files_on_request(self, project_manager: ProjectManager) -> None:
        project = project_manager.create(make_request())
        directory = project_manager.paths_for(project).root
        project_manager.delete(project.id, delete_files=True)
        assert not directory.exists()


class TestRepair:
    def test_recreates_missing_directories(self, project_manager: ProjectManager) -> None:
        project = project_manager.create(make_request())
        paths = project_manager.paths_for(project)
        (paths.root / "moments").rmdir()

        project_manager.repair_directories(project.id)

        assert (paths.root / "moments").is_dir()


class TestDeliveryChoices:
    """The import screen's three delivery choices (2026-08-28).

    Captions, a destination folder and auto-publish are all opt-in: the
    resting state of a new project writes nothing on the frame, copies
    nothing anywhere, and publishes nothing on its own.
    """

    def test_everything_defaults_to_off(self, project_manager) -> None:
        project = project_manager.create(
            ProjectCreate(name="Plain", target_duration_seconds=900)
        )

        assert project.captions_enabled is False
        assert project.output_directory is None
        assert project.auto_publish is False

    def test_the_choices_survive_the_database(self, project_manager, database) -> None:
        from backend.database.repositories.projects import ProjectRepository

        created = project_manager.create(
            ProjectCreate(
                name="Chosen",
                target_duration_seconds=900,
                captions_enabled=True,
                output_directory="D:\\Videos\\VAI",
                auto_publish=True,
            )
        )

        loaded = ProjectRepository(database).require(created.id)

        assert loaded.captions_enabled is True
        assert loaded.output_directory == "D:\\Videos\\VAI"
        assert loaded.auto_publish is True

    def test_an_output_directory_on_the_system_drive_is_refused(self) -> None:
        with pytest.raises(Exception, match="system drive"):
            ProjectCreate(
                name="X",
                target_duration_seconds=900,
                output_directory="C:\\Users\\anyone\\Videos",
            )

    def test_a_relative_output_directory_is_refused(self) -> None:
        with pytest.raises(Exception, match="absolute"):
            ProjectCreate(
                name="X", target_duration_seconds=900, output_directory="videos\\out"
            )

    def test_a_blank_output_directory_means_none(self) -> None:
        created = ProjectCreate(
            name="X", target_duration_seconds=900, output_directory="  "
        )

        assert created.output_directory is None
