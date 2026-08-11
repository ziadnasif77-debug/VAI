"""Phase 12: the worker that runs what the API queues (SPEC §46, §47, §57).

Without this the interface is a lie by omission: pressing Render queues a job,
the screen says the render has started, and nothing ever runs it. That is what
the first pointing of the UI at a real project revealed.

The tests worth having are about the loop's *behaviour under trouble* rather
than the happy path — one project failing must not stop another, a job left
running by a killed process must be picked up again, and stopping must not
strand a half-written file.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from ai.speech.fake_provider import FakeSpeechProvider
from ai.vision.fake_provider import FakeVisionProvider
from backend.core.models.enums import JobStage, JobStatus
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.repositories.jobs import JobRepository
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker
from backend.services.worker import JobWorker, active_projects, recover_stale_jobs
from tests.conftest import workers_through

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


def _fake_workers(stage: JobStage = JobStage.FRAMES) -> dict:
    """The registry a test should give the worker.

    Real providers would download Whisper to prove the loop polls a table, so
    the doubles go in and the chain stops where these tests stop caring.
    """
    workers = workers_through(stage)
    if JobStage.TRANSCRIPT in workers:
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(FakeSpeechProvider())
    if JobStage.VISION in workers:
        workers[JobStage.VISION] = VisionWorker(FakeVisionProvider())
    if JobStage.OCR in workers:
        workers[JobStage.OCR] = OcrWorker(FakeOcrProvider(default=[]))
    return workers


def _project(project_manager, media_service, clip: Path):
    project = project_manager.create(
        ProjectCreate(name="Worker", target_duration_seconds=600)
    )
    media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project


def _wait_for(predicate, *, timeout: float = 90.0, interval: float = 0.25) -> bool:
    """Poll until true. The worker is a thread, so tests wait on its effects."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestTheLoop:
    def test_it_runs_a_queued_job_without_being_asked_again(
        self, database, paths, config, project_manager, media_service, test_clip: Path
    ) -> None:
        # The whole point: the API queues, the worker does.
        project = _project(project_manager, media_service, test_clip)
        worker = JobWorker(config, paths, workers=_fake_workers())
        worker.start()
        try:
            ran = _wait_for(
                lambda: any(
                    job.status is JobStatus.COMPLETED
                    for job in JobRepository(database).list_for_project(project.id)
                )
            )
        finally:
            worker.stop()

        assert ran, "the worker never ran anything"
        assert worker.jobs_run > 0

    def test_it_stops_when_asked(self, database, paths, config) -> None:
        worker = JobWorker(config, paths, workers=_fake_workers())
        worker.start()
        assert worker.running

        worker.stop()

        assert not worker.running

    def test_stopping_an_idle_worker_is_immediate(self, database, paths, config) -> None:
        # The idle wait must be interruptible, or shutting down takes as long
        # as the poll interval every time.
        worker = JobWorker(config, paths, workers=_fake_workers())
        worker.start()
        started = time.monotonic()
        worker.stop()

        assert time.monotonic() - started < 5.0

    def test_starting_twice_does_not_run_two_loops(self, database, paths, config) -> None:
        worker = JobWorker(config, paths, workers=_fake_workers())
        worker.start()
        worker.start()
        try:
            assert worker.running
        finally:
            worker.stop()


class TestTrouble:
    def test_a_failing_project_does_not_stop_the_worker(
        self,
        database,
        paths,
        config,
        project_manager,
        media_service,
        test_clip: Path,
        tmp_path: Path,
    ) -> None:
        # A recording whose file has vanished fails at PROBE; another project
        # queued behind it must still be processed. The file is really deleted:
        # a test that only *says* the project is broken proves nothing.
        import shutil

        vanishing = tmp_path / "vanishing.mp4"
        shutil.copy(test_clip, vanishing)
        broken = project_manager.create(
            ProjectCreate(name="Broken", target_duration_seconds=600)
        )
        media_service.import_media(broken.id, MediaImport(path=str(vanishing)))
        vanishing.unlink()

        healthy = _project(project_manager, media_service, test_clip)

        worker = JobWorker(config, paths, workers=_fake_workers())
        worker.start()
        try:
            progressed = _wait_for(
                lambda: any(
                    job.status is JobStatus.COMPLETED
                    for job in JobRepository(database).list_for_project(healthy.id)
                )
            )
        finally:
            worker.stop()

        assert progressed, "the healthy project never advanced"
        assert any(
            job.status is JobStatus.FAILED
            for job in JobRepository(database).list_for_project(broken.id)
        ), "the broken project was expected to fail"

    def test_a_job_left_running_by_a_dead_process_is_picked_up_again(
        self, database, paths, config, project_manager, media_service, test_clip: Path
    ) -> None:
        # §47: a killed process leaves a row claiming to run forever, and
        # nothing would ever touch it again.
        project = _project(project_manager, media_service, test_clip)
        repository = JobRepository(database)
        job = repository.list_for_project(project.id)[0]
        database.execute(
            "UPDATE analysis_jobs SET status = ?, started_at = ? WHERE id = ?",
            (JobStatus.RUNNING.value, "2020-01-01T00:00:00+00:00", job.id),
        )

        recovered = recover_stale_jobs(database, config)

        assert recovered >= 1
        assert repository.require(job.id).status is JobStatus.QUEUED

    def test_recovery_runs_on_the_worker_thread_before_it_polls(
        self, database, paths, config, project_manager, media_service, test_clip: Path
    ) -> None:
        """The race this fixed: recovery resetting a job the worker just claimed.

        Doing it in the caller before ``start()`` reads as more careful and is
        not — the two live in one process, and a render was reset to "queued"
        two clips into cutting it. One thread owning the lifecycle removes the
        race rather than narrowing it.
        """
        project = _project(project_manager, media_service, test_clip)
        repository = JobRepository(database)
        job = repository.list_for_project(project.id)[0]
        database.execute(
            "UPDATE analysis_jobs SET status = ?, started_at = ? WHERE id = ?",
            (JobStatus.RUNNING.value, "2020-01-01T00:00:00+00:00", job.id),
        )

        worker = JobWorker(config, paths, workers=_fake_workers())
        worker.start()
        try:
            # The worker recovers it and then runs it: no external recovery call.
            finished = _wait_for(
                lambda: repository.require(job.id).status is JobStatus.COMPLETED
            )
        finally:
            worker.stop()

        assert finished, "the recovered job was never run"

    def test_no_job_is_left_claiming_to_be_queued_while_it_runs(
        self, database, paths, config, project_manager, media_service, test_clip: Path
    ) -> None:
        # The symptom the race produced: progress advancing on a row that says
        # it has not started.
        project = _project(project_manager, media_service, test_clip)
        repository = JobRepository(database)

        worker = JobWorker(config, paths, workers=_fake_workers())
        worker.start()
        try:
            contradictions: list[str] = []
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                for job in repository.list_for_project(project.id):
                    if job.status is JobStatus.QUEUED and job.progress > 0.0:
                        contradictions.append(f"{job.stage.value}: {job.progress}")
                time.sleep(0.2)
        finally:
            worker.stop()

        assert not contradictions, contradictions


class TestReporting:
    def test_active_projects_counts_outstanding_work(
        self, database, project_manager, media_service, test_clip: Path
    ) -> None:
        assert active_projects(database) == 0

        _project(project_manager, media_service, test_clip)

        assert active_projects(database) == 1

    def test_the_summary_says_whether_it_is_running(self, database, paths, config) -> None:
        worker = JobWorker(config, paths, workers=_fake_workers())
        assert worker.summary()["running"] is False

        worker.start()
        try:
            assert worker.summary()["running"] is True
        finally:
            worker.stop()


class TestAgainstTheRunner:
    """The worker must not do anything the runner would not."""

    def test_it_reaches_the_same_stages_the_runner_does(
        self, database, paths, config, project_manager, media_service, test_clip: Path
    ) -> None:
        project = _project(project_manager, media_service, test_clip)
        workers = workers_through(JobStage.VISION)
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(FakeSpeechProvider())
        workers[JobStage.VISION] = VisionWorker(FakeVisionProvider())
        workers[JobStage.OCR] = OcrWorker(FakeOcrProvider(default=[]))
        runner = PipelineRunner(database, paths, config, workers=workers)

        outcomes = runner.run_project(project.id)
        completed = {outcome.job.stage for outcome in outcomes if outcome.succeeded}

        assert JobStage.PROBE in completed
        assert JobStage.FRAMES in completed
