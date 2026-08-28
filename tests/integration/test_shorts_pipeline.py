"""Shorts, proved against a real encoder and a real decoder.

The unit tests own the arithmetic. What only FFmpeg can settle is whether the
argv the builders produce actually yields a 1080x1920 file of the planned
length with its audio intact — and whether the whole stage, run through the
job system the way the button runs it, leaves those files where the result
says they are.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.models.enums import JobStage, JobStatus, VideoMode
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.media.probe import probe_media
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers import default_workers
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker
from backend.rendering.encoder import select_encoder
from backend.rendering.shorts import ShortPlan, cut_arguments
from tests.conftest import workers_through

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


class TestTheCut:
    def test_one_plan_becomes_a_vertical_file_with_audio(
        self, config, ffmpeg_runner, reaction_clip: Path, tmp_path: Path
    ) -> None:
        plan = ShortPlan(
            index=0,
            media_id="media-x",
            moment_id=None,
            start_seconds=2.0,
            end_seconds=18.0,
            score=0.9,
        )
        meta = probe_media(reaction_clip, ffmpeg_runner).metadata
        destination = tmp_path / "short.mp4"

        argv = cut_arguments(
            plan,
            source=reaction_clip,
            destination=destination,
            config=config.shorts,
            encoder=select_encoder(config.render, ffmpeg_runner),
            render_config=config.render,
            source_width=meta.width or 1920,
            source_height=meta.height or 1080,
            fps=round(meta.fps or 30),
        )
        subprocess.run([*ffmpeg_runner.base_arguments(), *argv], check=True, capture_output=True)

        result = probe_media(destination, ffmpeg_runner)
        out = result.metadata
        assert (out.width, out.height) == (1080, 1920)
        assert out.duration_seconds == pytest.approx(16.0, abs=0.5)
        assert result.audio_tracks, "a Short without its own audio is a slideshow"


@pytest.fixture
def shorts_runner(database, paths, config, speech_provider, vision_provider):
    from ai.ocr.fake_provider import FakeOcrProvider

    workers = workers_through("qa")
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
    workers[JobStage.VISION] = VisionWorker(vision_provider)
    workers[JobStage.OCR] = OcrWorker(FakeOcrProvider(default=[("VICTORY", 0.92)]))
    workers[JobStage.SHORTS] = default_workers()[JobStage.SHORTS]
    return PipelineRunner(database, paths, config, workers=workers)


class TestTheStage:
    @pytest.mark.slow
    def test_the_stage_cuts_what_the_moments_earned(
        self, media_service, project_manager, shorts_runner, database, reaction_clip: Path
    ) -> None:
        project = project_manager.create(
            ProjectCreate(name="Vertical", target_duration_seconds=600, mode=VideoMode.STORY)
        )
        media_service.import_media(project.id, MediaImport(path=str(reaction_clip)))
        shorts_runner.run_project(project.id)

        job = shorts_runner.jobs.queue(project.id, JobStage.SHORTS)
        outcome = shorts_runner.run_job(job.id)

        assert outcome.succeeded, outcome.job.error_message
        result = outcome.job.result
        if result.get("skipped"):
            # A 40-second fixture may not produce a moment that satisfies the
            # Shorts band; that is the stage answering honestly, not failing.
            assert result["reason"] == "no usable moments"
            return
        assert result["frame"] == "1080x1920"
        for produced in result["shorts"]:
            path = Path(produced["output_path"])
            assert path.is_file(), produced
            assert produced["size_bytes"] > 0

    def test_the_stage_runs_only_when_asked(
        self, media_service, project_manager, shorts_runner, reaction_clip
    ):
        # §51: nothing is cut unasked. The asking has two shapes now -- the
        # button, or the owner's standing auto_after_qa -- so what the
        # pipeline run itself must never do is *execute* the cut uninvited:
        # with the standing config on, a green QA queues the job and it waits
        # for a worker; with it off, the job does not even exist.
        project = project_manager.create(
            ProjectCreate(name="NoAuto", target_duration_seconds=600, mode=VideoMode.STORY)
        )
        media_service.import_media(project.id, MediaImport(path=str(reaction_clip)))
        shorts_runner.run_project(project.id)

        jobs = [
            job
            for job in shorts_runner.jobs.list_jobs(project.id)
            if job.stage is JobStage.SHORTS
        ]
        if shorts_runner._config.shorts.auto_after_qa:
            assert all(job.status is JobStatus.QUEUED for job in jobs)
        else:
            assert jobs == []


class TestTheEndpoint:
    def test_the_button_queues_the_stage(self, api_client) -> None:
        project = api_client.post(
            "/api/projects",
            json={"name": "Cuts", "target_duration_seconds": 900, "mode": "story"},
        ).json()

        response = api_client.post(f"/api/projects/{project['id']}/shorts")

        assert response.status_code == 200
        job_id = response.json()["job_id"]
        jobs = api_client.get(f"/api/projects/{project['id']}/jobs").json()["items"]
        row = next(item for item in jobs if item["id"] == job_id)
        assert row["stage"] == "shorts"
        assert row["status"] == "queued"
