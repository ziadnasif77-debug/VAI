"""Job system tests (SPEC sections 46, 47, 81, 82, 127)."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

import pytest

from backend.core.errors import ErrorCode, JobError
from backend.core.ids import new_id
from backend.core.models.enums import JobStage, JobStatus, VideoMode
from backend.core.models.jobs import (
    ANALYSIS_STAGES,
    DELIVERY_STAGES,
    EDIT_STAGES,
    MANUAL_STAGES,
    automatic_stages,
    dependencies_of,
    dependents_of,
    downstream_of,
    runnable_stages,
    stages_in_order,
    validate_stage_graph,
)
from backend.core.models.media import Media
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.database.repositories.media import MediaRepository
from backend.services.job_manager import PER_MEDIA_STAGES, JobManager
from backend.services.project_manager import ProjectManager

pytestmark = pytest.mark.unit


@pytest.fixture
def project_id(project_manager: ProjectManager) -> str:
    return project_manager.create(
        ProjectCreate(
            name="Session",
            target_duration_seconds=1200,
            mode=VideoMode.STORY,
            game="generic",
        )
    ).id


@pytest.fixture
def media_id(database: Database, project_id: str) -> str:
    """A registered media row, inserted directly.

    The repository is used rather than the ingestion service so these tests
    control exactly which jobs exist -- importing would queue a chain of its own.
    """
    return _insert_media(database, project_id, "gameplay.mp4").id


@pytest.fixture
def second_media_id(database: Database, project_id: str) -> str:
    return _insert_media(database, project_id, "gameplay_2.mp4").id


def _insert_media(database: Database, project_id: str, filename: str) -> Media:
    now = datetime.now(timezone.utc)
    media = Media(
        id=new_id("media"),
        project_id=project_id,
        source_path=f"/recordings/{filename}",
        filename=filename,
        container=".mp4",
        size_bytes=1024,
        checksum=sha256(filename.encode()).hexdigest(),
        created_at=now,
        updated_at=now,
    )
    return MediaRepository(database).create(media)


class TestStageGraph:
    def test_graph_is_valid(self) -> None:
        validate_stage_graph()

    def test_every_stage_is_classified(self) -> None:
        covered = set(ANALYSIS_STAGES) | set(EDIT_STAGES) | set(DELIVERY_STAGES)
        assert covered == set(JobStage)

    def test_import_is_the_only_root(self) -> None:
        roots = [stage for stage in JobStage if not dependencies_of(stage)]
        assert roots == [JobStage.IMPORT]

    def test_game_events_waits_for_every_detector(self) -> None:
        # §27: correlation only works if all detector outputs are present.
        required = set(dependencies_of(JobStage.GAME_EVENTS))
        assert {
            JobStage.VISION,
            JobStage.OCR,
            JobStage.AUDIO_EVENTS,
            JobStage.TRANSCRIPT,
            JobStage.SCENES,
        } <= required

    def test_downstream_of_vision_reaches_the_render(self) -> None:
        affected = downstream_of(JobStage.VISION)
        assert JobStage.GAME_EVENTS in affected
        assert JobStage.MOMENTS in affected
        assert JobStage.RENDER in affected

    def test_downstream_of_vision_excludes_transcript(self) -> None:
        # Replacing the vision model must not force a new transcription (§48).
        assert JobStage.TRANSCRIPT not in downstream_of(JobStage.VISION)

    def test_dependents_of_audio(self) -> None:
        assert set(dependents_of(JobStage.AUDIO)) == {
            JobStage.TRANSCRIPT,
            JobStage.AUDIO_EVENTS,
        }

    def test_only_import_is_runnable_initially(self) -> None:
        assert runnable_stages(set()) == (JobStage.IMPORT,)

    def test_probe_unlocks_proxy_and_audio(self) -> None:
        ready = runnable_stages({JobStage.IMPORT, JobStage.PROBE})
        assert set(ready) >= {JobStage.PROXY, JobStage.AUDIO}

    def test_delivery_stages_are_never_automatic(self) -> None:
        completed = set(JobStage) - set(DELIVERY_STAGES)
        assert runnable_stages(completed) == ()
        assert runnable_stages(completed, include_manual=True) == (JobStage.EXPORT,)

    def test_automatic_pipeline_stops_at_qa(self) -> None:
        assert automatic_stages()[-1] is JobStage.QA
        assert set(DELIVERY_STAGES) == MANUAL_STAGES

    def test_enum_order_is_topological(self) -> None:
        position = {stage: index for index, stage in enumerate(stages_in_order())}
        for stage in JobStage:
            for prerequisite in dependencies_of(stage):
                assert position[prerequisite] < position[stage]


class TestQueueing:
    def test_import_chain_queues_per_media_stages(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        jobs = job_manager.queue_import_chain(project_id, media_id)
        assert {job.stage for job in jobs} == set(PER_MEDIA_STAGES)

    def test_queueing_is_idempotent(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        first = job_manager.queue_import_chain(project_id, media_id)
        second = job_manager.queue_import_chain(project_id, media_id)
        assert [job.id for job in first] == [job.id for job in second]

    def test_project_stages_exclude_delivery(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        stages = {job.stage for job in job_manager.queue_project_stages(project_id)}
        assert not (stages & set(DELIVERY_STAGES))
        # STORY is the first stage that reasons across every file at once, so
        # it is the first that is genuinely project-wide.
        assert JobStage.STORY in stages
        assert not (stages & PER_MEDIA_STAGES)

    def test_per_media_stage_requires_a_media_id(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        with pytest.raises(JobError) as exc_info:
            job_manager.queue(project_id, JobStage.TRANSCRIPT)
        assert exc_info.value.code is ErrorCode.JOB_DEPENDENCY_FAILED

    def test_two_media_files_get_separate_jobs(
        self,
        job_manager: JobManager,
        project_id: str,
        media_id: str,
        second_media_id: str,
    ) -> None:
        first = job_manager.queue(project_id, JobStage.PROBE, media_id=media_id)
        second = job_manager.queue(project_id, JobStage.PROBE, media_id=second_media_id)
        assert first.id != second.id


class TestLifecycle:
    def test_start_complete(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        started = job_manager.start(job.id)
        assert started.status is JobStatus.RUNNING
        assert started.attempt == 1

        completed = job_manager.complete(job.id, result={"frames": 12})
        assert completed.status is JobStatus.COMPLETED
        assert completed.progress == 1.0
        assert completed.result == {"frames": 12}
        assert completed.duration_seconds is not None

    def test_cannot_start_with_unmet_dependencies(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.VISION, media_id=media_id)
        with pytest.raises(JobError) as exc_info:
            job_manager.start(job.id)
        assert exc_info.value.code is ErrorCode.JOB_DEPENDENCY_FAILED
        assert "frames" in str(exc_info.value)

    def test_failure_records_a_typed_code(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(job.id)
        failed = job_manager.fail(
            job.id, error_code=ErrorCode.GPU_OUT_OF_MEMORY, error_message="CUDA OOM"
        )
        assert failed.status is JobStatus.FAILED
        assert failed.error_code == ErrorCode.GPU_OUT_OF_MEMORY.value
        assert failed.can_retry is True

    def test_retry_count_tracks_attempts(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(job.id)
        job_manager.fail(job.id, error_code=ErrorCode.WORKER_FAILED, error_message="x")
        requeued = job_manager.queue(
            project_id, JobStage.IMPORT, media_id=media_id
        )
        assert requeued.status is JobStatus.QUEUED
        restarted = job_manager.start(requeued.id)
        assert restarted.attempt == 2
        assert restarted.retry_count == 1

    def test_progress_is_clamped(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        assert job_manager.report_progress(job.id, 5.0).progress == 1.0
        assert job_manager.report_progress(job.id, -1.0).progress == 0.0


class TestCancellation:
    def test_cancel_flags_unfinished_jobs(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job_manager.queue_import_chain(project_id, media_id)
        assert job_manager.cancel_project(project_id) == len(PER_MEDIA_STAGES)

    def test_cancelled_job_does_not_start(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.cancel_project(project_id)
        assert job_manager.start(job.id).status is JobStatus.CANCELLED

    def test_completed_jobs_are_untouched(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(job.id)
        job_manager.complete(job.id)
        assert job_manager.cancel_project(project_id) == 0


class TestReanalysis:
    """§90: a finished stage can be asked to run again over the same source."""

    def test_a_completed_stage_can_be_returned_to_the_queue(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(job.id)
        job_manager.complete(job.id, result={"size_bytes": 1})

        requeued = job_manager.requeue(job.id)

        assert requeued.status is JobStatus.QUEUED
        assert requeued.progress == 0.0
        assert requeued.completed_at is None
        # It can actually start again, which is the point.
        assert job_manager.start(job.id).status is JobStatus.RUNNING

    def test_a_re_run_gets_a_fresh_retry_budget(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        # A user asking for a re-run is starting a new run, not continuing the
        # one that already exhausted its attempts.
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(job.id)
        job_manager.fail(job.id, error_code=ErrorCode.MEDIA_CORRUPTED, error_message="bad")

        assert job_manager.requeue(job.id).attempt == 0
        assert job_manager.get_job(job.id).error_code is None

    def test_the_attempt_count_can_be_preserved(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(job.id)
        job_manager.complete(job.id)
        assert job_manager.requeue(job.id, reset_attempts=False).attempt == 1

    def test_a_running_stage_is_not_requeued_underneath_its_worker(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(job.id)
        with pytest.raises(JobError):
            job_manager.requeue(job.id)

    def test_requeueing_clears_a_cancellation_request(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.cancel_project(project_id)
        job_manager.cancel(job.id)

        requeued = job_manager.requeue(job.id)

        assert requeued.cancel_requested is False
        assert job_manager.start(job.id).status is JobStatus.RUNNING


class TestRecovery:
    def test_running_jobs_are_requeued_after_a_crash(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(job.id)

        recovered = job_manager.recover(project_id)

        assert [item.id for item in recovered] == [job.id]
        assert job_manager.get_job(job.id).status is JobStatus.QUEUED

    def test_completed_work_survives_recovery(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        # §47: a crash during VISION must not restart from IMPORT.
        done = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        job_manager.start(done.id)
        job_manager.complete(done.id)

        crashed = job_manager.queue(
            project_id, JobStage.PROBE, media_id=media_id
        )
        job_manager.start(crashed.id)

        job_manager.recover(project_id)

        assert job_manager.get_job(done.id).status is JobStatus.COMPLETED
        assert job_manager.get_job(crashed.id).status is JobStatus.QUEUED

    def test_recovery_is_a_no_op_on_a_clean_database(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job_manager.queue_import_chain(project_id, media_id)
        assert job_manager.recover(project_id) == []


class TestScheduling:
    def test_next_runnable_starts_with_import(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job_manager.queue_import_chain(project_id, media_id)
        nxt = job_manager.next_runnable(project_id)
        assert nxt is not None and nxt.stage is JobStage.IMPORT

    def test_next_runnable_advances_after_completion(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        job_manager.queue_import_chain(project_id, media_id)
        first = job_manager.next_runnable(project_id)
        assert first is not None
        job_manager.start(first.id)
        job_manager.complete(first.id)

        nxt = job_manager.next_runnable(project_id)
        assert nxt is not None and nxt.stage is JobStage.PROBE

    def test_none_when_nothing_is_queued(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        assert job_manager.next_runnable(project_id) is None


class TestStatusReport:
    def test_reports_every_stage(self, job_manager: JobManager, project_id: str) -> None:
        report = job_manager.project_status(project_id)
        assert {item.stage for item in report} == set(JobStage)

    def test_unqueued_stages_have_no_status(
        self, job_manager: JobManager, project_id: str, media_id: str
    ) -> None:
        report = {item.stage: item for item in job_manager.project_status(project_id)}
        assert report[JobStage.RENDER].status is None

    def test_failure_dominates_the_rollup(
        self,
        job_manager: JobManager,
        project_id: str,
        media_id: str,
        second_media_id: str,
    ) -> None:
        # Two recordings at the same stage: one succeeds, one fails. The stage
        # must report FAILED, not "half done".
        first = job_manager.queue(project_id, JobStage.IMPORT, media_id=media_id)
        second = job_manager.queue(project_id, JobStage.IMPORT, media_id=second_media_id)
        job_manager.start(first.id)
        job_manager.complete(first.id)
        job_manager.start(second.id)
        job_manager.fail(
            second.id, error_code=ErrorCode.MEDIA_CORRUPTED, error_message="bad file"
        )

        report = {item.stage: item for item in job_manager.project_status(project_id)}
        assert report[JobStage.IMPORT].status is JobStatus.FAILED
        assert report[JobStage.IMPORT].error_code == ErrorCode.MEDIA_CORRUPTED.value
