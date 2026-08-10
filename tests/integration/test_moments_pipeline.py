"""Phase 6 acceptance: ranked moments with their reasoning.

    **Acceptance: ranked moments, each with a stored `score_breakdown` and
    `explanation`** — the Q&A layer already reads both and will start answering
    "why did you pick this?" for real.

Both halves matter. A ranked list without the working shown is a black box the
narrative stage cannot reason about and the user cannot argue with; §80 requires
every decision to be explainable, and §33 requires the selector to know *why* a
moment scored what it did rather than only *that* it did.

Run through the whole pipeline on real files, so the moments come from events
that came from detectors that came from a decoded recording.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from backend.core.models.enums import JobStage
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.repositories.moments import MomentRepository
from backend.moments.scoring import DIMENSIONS
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers import default_workers
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


@pytest.fixture
def ocr_provider() -> FakeOcrProvider:
    return FakeOcrProvider(default=[("VICTORY", 0.92)])


@pytest.fixture
def runner(database, paths, config, speech_provider, vision_provider, ocr_provider):
    workers = default_workers()
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
    workers[JobStage.VISION] = VisionWorker(vision_provider)
    workers[JobStage.OCR] = OcrWorker(ocr_provider)
    return PipelineRunner(database, paths, config, workers=workers)


def _project_with(media_service, project_manager, clip: Path):
    project = project_manager.create(
        ProjectCreate(name="Moments", target_duration_seconds=600)
    )
    media = media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project, media


class TestAcceptance:
    def test_moments_are_produced_and_ranked(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert outcomes[JobStage.MOMENTS].succeeded
        assert outcomes[JobStage.MOMENTS].job.result["moments"] > 0

        moments = MomentRepository(database).list_for_media(media.id)
        assert moments
        scores = [moment.score for moment in moments]
        assert scores == sorted(scores, reverse=True)
        assert all(0.0 <= score <= 1.0 for score in scores)

    def test_every_moment_stores_its_score_breakdown(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        # §32: ten dimensions, and a total nobody can check is not a score.
        project, media = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)

        for moment in MomentRepository(database).list_for_media(media.id):
            breakdown = moment.score_breakdown
            assert breakdown
            for dimension in DIMENSIONS:
                assert dimension in breakdown, f"{dimension} missing from the breakdown"
                assert 0.0 <= breakdown[dimension] <= 1.0
            # The penalties are stored separately from the dimensions, so a
            # low score can be attributed rather than guessed at.
            assert "_penalty_dead_time" in breakdown
            assert "_multiplier" in breakdown

    def test_every_moment_stores_an_explanation(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        # §80: the Q&A layer answers "why did you pick this?" from this field.
        project, media = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)

        for moment in MomentRepository(database).list_for_media(media.id):
            assert moment.explanation
            assert all(isinstance(line, str) and line.strip() for line in moment.explanation)
            # It says something about the evidence, not just the number.
            assert any("event" in line.lower() for line in moment.explanation)

    def test_every_moment_has_a_viewing_span_wider_than_its_events(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        # §29: a clip is the moment plus the context that makes it land.
        project, media = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)

        for moment in MomentRepository(database).list_for_media(media.id):
            assert moment.context_start <= moment.start_seconds
            assert moment.context_end >= moment.end_seconds
            assert moment.context_duration >= moment.duration


class TestStagePipeline:
    def test_the_stage_reports_what_it_did(
        self, media_service, project_manager, runner, reaction_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        result = outcomes[JobStage.MOMENTS].result
        assert result["formed"] >= result["moments"]
        assert "by_type" in result
        assert "variety" in result
        assert result["total_context_seconds"] > 0

    def test_moments_come_from_the_events_that_were_detected(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        from backend.database.repositories.gaming import GameEventRepository

        project, media = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)

        events = GameEventRepository(database).list_for_media(media.id)
        moments = MomentRepository(database).list_for_media(media.id)
        assert events and moments
        # Grouping, not multiplying: §28 makes story fragments out of events.
        assert len(moments) <= len(events)
        for moment in moments:
            assert moment.events

    def test_timestamps_stay_inside_the_recording(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)

        stored = media_service.get_media(media.id)
        duration = stored.metadata.duration_seconds or 0.0
        assert duration > 0
        for moment in MomentRepository(database).list_for_media(media.id):
            assert moment.context_start >= 0.0
            assert moment.context_end <= duration + 0.5

    def test_a_recording_with_no_events_is_not_a_failure(
        self, media_service, project_manager, database, runner, silent_clip: Path
    ) -> None:
        # A silent, uneventful recording produces nothing to highlight, and
        # that is a legitimate result rather than a broken stage.
        project, media = _project_with(media_service, project_manager, silent_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert outcomes[JobStage.MOMENTS].succeeded
        assert MomentRepository(database).count_for_media(media.id) >= 0

    def test_re_running_replaces_rather_than_appends(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)
        first = MomentRepository(database).count_for_media(media.id)

        job = next(
            j for j in runner.jobs.list_jobs(project.id) if j.stage is JobStage.MOMENTS
        )
        runner.jobs.requeue(job.id)
        assert runner.run_job(job.id).succeeded
        assert MomentRepository(database).count_for_media(media.id) == first

    def test_a_user_decision_survives_a_re_run(
        self, media_service, project_manager, database, runner, reaction_clip: Path
    ) -> None:
        # §78, §121: the user has the final word, and an analysis re-run must
        # not quietly revert a choice they made.
        project, media = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)

        repository = MomentRepository(database)
        moments = repository.list_for_media(media.id)
        assert moments
        target = moments[0]
        assert repository.set_user_state(target.metadata["id"], "rejected")

        job = next(
            j for j in runner.jobs.list_jobs(project.id) if j.stage is JobStage.MOMENTS
        )
        runner.jobs.requeue(job.id)
        runner.run_job(job.id)

        after = repository.list_for_media(media.id)
        preserved = [
            moment
            for moment in after
            if round(moment.start_seconds, 3) == round(target.start_seconds, 3)
        ]
        assert preserved
        assert preserved[0].metadata["user_state"] == "rejected"

    def test_the_runner_now_stops_at_story(
        self, media_service, project_manager, runner, reaction_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)

        assert runner.run_next(project.id) is None
        frontier = next(
            job
            for job in runner.jobs.list_jobs(project.id)
            if job.stage not in runner.supported_stages
        )
        assert frontier.stage is JobStage.STORY
        assert frontier.error_code is None
