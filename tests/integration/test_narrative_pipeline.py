"""Phase 7 acceptance: a coherent edit of the requested length.

    **Acceptance: a 2-hour source becomes a coherent 20-minute edit within the
    configured tolerance.**

Run through the whole pipeline on a real recording, so the plan is built from
moments that came from events that came from detectors that decoded a file.

The clip is short — a 2-hour fixture would take an hour to transcode and prove
nothing the unit tests do not already prove about the optimiser's arithmetic.
What this file checks is the part only the pipeline can show: that the stage is
wired, reads what the earlier stages stored, and produces a plan the EDL stage
can act on. The 2-hour-to-20-minute arithmetic itself is
``test_narrative.py::TestAcceptance``, against a 200-moment session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from backend.core.models.enums import JobStage, VideoMode
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.repositories.moments import MomentRepository
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker
from tests.conftest import workers_through

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


@pytest.fixture
def ocr_provider() -> FakeOcrProvider:
    return FakeOcrProvider(default=[("VICTORY", 0.92)])


@pytest.fixture
def runner(database, paths, config, speech_provider, vision_provider, ocr_provider):
    workers = workers_through("story")
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
    workers[JobStage.VISION] = VisionWorker(vision_provider)
    workers[JobStage.OCR] = OcrWorker(ocr_provider)
    return PipelineRunner(database, paths, config, workers=workers)


def _project_with(media_service, project_manager, clip: Path, *, mode=VideoMode.STORY):
    project = project_manager.create(
        ProjectCreate(name="Narrative", target_duration_seconds=600, mode=mode)
    )
    media = media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project, media


class TestStoryStage:
    def test_the_stage_runs_and_produces_a_plan(
        self, media_service, project_manager, runner, reaction_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert JobStage.STORY in outcomes, "the runner must reach the project-wide stages"
        assert outcomes[JobStage.STORY].succeeded
        result = outcomes[JobStage.STORY].job.result
        assert result["mode"] == "story"
        assert result["clips"]

    def test_every_clip_references_the_source_non_destructively(
        self, media_service, project_manager, runner, reaction_clip: Path
    ) -> None:
        # §42: the EDL references the original recording, it never copies frames.
        project, media = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        for clip in outcomes[JobStage.STORY].result["clips"]:
            assert clip["media_id"] == media.id
            assert clip["source_end"] > clip["source_start"] >= 0.0
            assert clip["seconds"] > 0.0

    def test_the_plan_records_its_hook_and_pacing(
        self, media_service, project_manager, runner, reaction_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        result = outcomes[JobStage.STORY].result
        assert "hook" in result
        assert "pacing" in result
        assert result["pacing"]["clips"] == len(result["clips"])

    def test_the_hook_is_a_clip_that_exists_in_the_plan(
        self, media_service, project_manager, runner, reaction_clip: Path
    ) -> None:
        # §37: the system selects; it never invents.
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        result = outcomes[JobStage.STORY].result
        if result["hook"]["hook"] is None:
            pytest.skip("no moment in this clip was strong enough to open on")
        opening = [clip for clip in result["clips"] if clip["role"] == "hook"]
        assert len(opening) == 1
        assert opening[0]["index"] == 0

    def test_the_plan_never_exceeds_the_available_moments(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        stored = MomentRepository(database).count_for_media(media.id)
        result = outcomes[JobStage.STORY].result
        assert result["moments_considered"] == stored
        # The hook may be replayed in the body, so the plan can hold one extra.
        assert len(result["clips"]) <= stored + 1

    def test_a_short_source_reports_missing_the_target_rather_than_faking_it(
        self, media_service, project_manager, runner, reaction_clip: Path
    ) -> None:
        # A 40-second clip cannot fill a 10-minute request, and saying it did
        # would be worse than saying it cannot.
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        result = outcomes[JobStage.STORY].result
        assert result["within_target"] is False
        assert result["notes"]
        assert result["total_seconds"] < result["target_seconds"]

    @pytest.mark.parametrize(
        "mode", [VideoMode.STORY, VideoMode.BEST_MOMENTS, VideoMode.COMPILATION]
    )
    def test_every_mode_runs_through_the_pipeline(
        self, media_service, project_manager, runner, reaction_clip: Path, mode
    ) -> None:
        project, _ = _project_with(media_service, project_manager, reaction_clip, mode=mode)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert outcomes[JobStage.STORY].succeeded
        assert outcomes[JobStage.STORY].job.result["mode"] == mode.value

    def test_re_running_the_stage_is_cheap_and_repeatable(
        self, media_service, project_manager, runner, reaction_clip: Path
    ) -> None:
        # §127: changing the target re-runs this stage against stored moments,
        # and never re-analyses the source.
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)

        job = next(
            j for j in runner.jobs.list_jobs(project.id) if j.stage is JobStage.STORY
        )
        first = job.result
        runner.jobs.requeue(job.id)
        outcome = runner.run_job(job.id)

        assert outcome.succeeded
        assert outcome.job.result["clips"] == first["clips"]

    def test_the_runner_stops_at_the_frontier(
        self, media_service, project_manager, runner, frontier_check, reaction_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)
        frontier_check(runner, project.id)
