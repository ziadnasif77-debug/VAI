"""The PUBLISH stage worker (§50, §51, §76, §81).

What only the worker owns: honouring the QA verdict, resolving which render
goes out, and writing the person's instruction into history exactly as it was
honoured. The publisher protocol itself is tested in
``test_youtube_publisher.py``; here the publisher is a scripted fake, because
the worker must behave identically whichever destination is behind the
registry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.core.models.enums import (
    JobStage,
    JobStatus,
    PublishStatus,
    PublishTarget,
    VideoMode,
)
from backend.core.models.project import ProjectCreate
from backend.core.models.publishing import PublishResult
from backend.database.repositories.renders import RenderRepository
from backend.publishing.base import PublisherRegistry
from backend.services.job_manager import JobManager

pytestmark = pytest.mark.unit


class FakePublisher:
    target = PublishTarget.YOUTUBE

    def __init__(self, *, configured: bool = True):
        self.configured = configured
        self.requests: list = []

    def is_configured(self) -> bool:
        return self.configured

    def publish(self, request, render_path: Path) -> PublishResult:
        self.requests.append((request, render_path))
        return PublishResult(
            status=PublishStatus.COMPLETED,
            target=self.target,
            external_id="vid123",
            external_url="https://youtu.be/vid123",
            completed_at=datetime.now(timezone.utc),
        )


@pytest.fixture
def project_id(project_manager) -> str:
    return project_manager.create(
        ProjectCreate(name="Publish", target_duration_seconds=900, mode=VideoMode.STORY)
    ).id


@pytest.fixture
def rendered(database, config, paths, project_id, tmp_path) -> str:
    """A finished render row pointing at a real file, with its stages done."""
    output = tmp_path / "final.mp4"
    output.write_bytes(b"mp4")
    renders = RenderRepository(database)
    render_id = renders.start(project_id, resolution=1080, fps=60, encoder="h264_nvenc")
    renders.complete(
        render_id,
        output_path=str(output),
        duration_seconds=600.0,
        size_bytes=3,
        video_codec="h264",
        audio_codec="aac",
        render_seconds=12.0,
    )
    _stage_done(database, config, project_id, JobStage.RENDER)
    _stage_done(database, config, project_id, JobStage.QA, {"blocks_export": False})
    return render_id


def _stage_done(database, config, project_id, stage: JobStage, result=None) -> None:
    """A completed stage row, written as history rather than executed.

    The dependency gate is the thing under test elsewhere; here it must be
    satisfied, and running a real render to satisfy it would test the render.
    """
    manager = JobManager(database, config)
    job = manager.queue(project_id, stage)
    manager._jobs.update(
        job.model_copy(
            update={
                "status": JobStatus.COMPLETED,
                "completed_at": datetime.now(timezone.utc),
                "result": dict(result or {}),
            }
        )
    )


def _run(pipeline_runner, database, config, project_id, publisher, payload):
    from backend.pipeline.workers.publish_worker import PublishWorker

    registry = PublisherRegistry()
    registry.register(publisher)
    pipeline_runner._workers = {
        **pipeline_runner._workers,
        JobStage.PUBLISH: PublishWorker(registry),
    }
    manager = JobManager(database, config)
    job = manager.queue(project_id, JobStage.PUBLISH, payload=payload)
    return pipeline_runner.run_job(job.id)


class TestDelivery:
    def test_the_instruction_reaches_the_publisher_verbatim(
        self, pipeline_runner, database, config, project_id, rendered
    ) -> None:
        publisher = FakePublisher()
        outcome = _run(
            pipeline_runner,
            database,
            config,
            project_id,
            publisher,
            {
                "target": "youtube",
                "metadata": {"title": "ليلة في Grounded", "visibility": "unlisted"},
            },
        )

        assert outcome.succeeded
        request, path = publisher.requests[0]
        assert request.metadata.title == "ليلة في Grounded"
        assert request.metadata.visibility.value == "unlisted"
        assert path.is_file()
        # §81: the job result is the publication history.
        result = outcome.job.result
        assert result["external_url"] == "https://youtu.be/vid123"
        assert result["metadata_snapshot"]["title"] == "ليلة في Grounded"
        assert result["render_id"] == rendered

    def test_without_a_render_there_is_nothing_to_publish(
        self, pipeline_runner, database, config, project_id
    ) -> None:
        _stage_done(database, config, project_id, JobStage.RENDER)
        _stage_done(database, config, project_id, JobStage.QA)
        outcome = _run(
            pipeline_runner, database, config, project_id, FakePublisher(), {"target": "youtube"}
        )

        assert not outcome.succeeded
        assert "Render the project first" in (outcome.job.error_message or "")

    def test_an_unconnected_target_is_a_clear_refusal(
        self, pipeline_runner, database, config, project_id, rendered
    ) -> None:
        outcome = _run(
            pipeline_runner,
            database,
            config,
            project_id,
            FakePublisher(configured=False),
            {"target": "youtube"},
        )

        assert not outcome.succeeded
        assert "not connected" in (outcome.job.error_message or "")


class TestTheQaVerdict:
    def test_a_blocking_qa_verdict_stops_the_upload(
        self, pipeline_runner, database, config, project_id, tmp_path
    ) -> None:
        output = tmp_path / "blocked.mp4"
        output.write_bytes(b"mp4")
        renders = RenderRepository(database)
        renders.complete(
            renders.start(project_id, resolution=1080, fps=60, encoder="h264_nvenc"),
            output_path=str(output),
            duration_seconds=600.0,
            size_bytes=3,
            video_codec="h264",
            audio_codec="aac",
            render_seconds=12.0,
        )
        _stage_done(database, config, project_id, JobStage.RENDER)
        _stage_done(database, config, project_id, JobStage.QA, {"blocks_export": True})
        publisher = FakePublisher()

        outcome = _run(
            pipeline_runner, database, config, project_id, publisher, {"target": "youtube"}
        )

        assert not outcome.succeeded
        assert publisher.requests == []
        assert "QA" in (outcome.job.error_message or "")

    def test_warnings_do_not_stop_a_person_who_confirmed(
        self, pipeline_runner, database, config, project_id, rendered
    ) -> None:
        # §78 gave the human the last word; pressing publish was it. The
        # `rendered` fixture's QA row completed with blocks_export false.
        outcome = _run(
            pipeline_runner, database, config, project_id, FakePublisher(), {"target": "youtube"}
        )

        assert outcome.succeeded
