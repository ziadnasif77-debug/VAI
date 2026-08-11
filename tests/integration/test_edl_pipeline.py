"""Phase 8 acceptance: the generated EDL reproduces the planned video.

    **Acceptance: the generated EDL reproduces the planned video exactly.**

Run through the whole pipeline on a real recording, so the timeline is built
from a plan that came from moments that came from events that came from
detectors that decoded a file.

What only the pipeline can show, and what this file is therefore for: the stage
reads the plan the STORY stage *stored* rather than recomputing one, the clips
it writes reference the original recording (§42), the captions it times come
from real transcript timestamps (§71), and a re-run leaves the user's decisions
intact (§78). The arithmetic of laying clips out is ``test_timeline.py``.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from ai.speech.fake_provider import FakeSpeechProvider
from backend.core.models.enums import JobStage, VideoMode
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker
from backend.timeline import operations, validation
from tests.conftest import workers_through

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


@pytest.fixture
def ocr_provider() -> FakeOcrProvider:
    return FakeOcrProvider(default=[("VICTORY", 0.92)])


@pytest.fixture
def runner(database, paths, config, speech_provider, vision_provider, ocr_provider):
    workers = workers_through("edl")
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
    workers[JobStage.VISION] = VisionWorker(vision_provider)
    workers[JobStage.OCR] = OcrWorker(ocr_provider)
    return PipelineRunner(database, paths, config, workers=workers)


def _project_with(media_service, project_manager, clip: Path, *, mode=VideoMode.STORY):
    project = project_manager.create(
        ProjectCreate(name="EDL", target_duration_seconds=600, mode=mode)
    )
    media = media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project, media


@pytest.fixture
def edited(media_service, project_manager, runner, database, reaction_clip: Path):
    """A project run all the way to a stored timeline.

    The media is re-read afterwards: the record handed back by ``import_media``
    predates PROBE, so its duration is still unknown at that point.
    """
    project, media = _project_with(media_service, project_manager, reaction_clip)
    outcomes = {outcome.job.stage: outcome for outcome in runner.run_project(project.id)}
    probed = MediaRepository(database).require(media.id)
    return project, probed, outcomes


class TestEdlStage:
    def test_the_stage_runs_and_stores_a_timeline(self, edited, database) -> None:
        project, _, outcomes = edited

        assert JobStage.EDL in outcomes, "the runner must reach the EDL stage"
        assert outcomes[JobStage.EDL].succeeded, outcomes[JobStage.EDL].job.error_message
        assert TimelineRepository(database).clip_count(project.id) > 0

    def test_the_edl_reproduces_the_plan_clip_for_clip(self, edited) -> None:
        # **The acceptance criterion.** Same clips, same order, same spans.
        _, _, outcomes = edited
        plan = outcomes[JobStage.STORY].job.result["clips"]
        edl = outcomes[JobStage.EDL].job.result

        assert edl["clips"] == len(plan)
        assert edl["duration_seconds"] == pytest.approx(
            sum(clip["seconds"] for clip in plan), abs=0.05
        )

    def test_the_stored_clips_reference_the_source_non_destructively(
        self, edited, database
    ) -> None:
        # §42: the EDL references the original recording; it never copies frames.
        project, media, outcomes = edited
        plan = outcomes[JobStage.STORY].job.result["clips"]
        stored = TimelineRepository(database).list_clips(project.id)

        assert len(stored) == len(plan)
        for planned, clip in zip(plan, stored, strict=True):
            assert clip.media_id == media.id
            assert clip.source_in == pytest.approx(planned["source_start"], abs=0.01)
            assert clip.source_out == pytest.approx(planned["source_end"], abs=0.01)

    def test_the_timeline_is_contiguous_and_starts_at_zero(self, edited, database) -> None:
        project, _, _ = edited
        clips = TimelineRepository(database).list_clips(project.id)

        assert clips[0].timeline_start == 0.0
        for previous, following in pairwise(clips):
            assert following.timeline_start == pytest.approx(previous.timeline_end)

    def test_the_stored_timeline_validates(self, edited, database) -> None:
        project, media, _ = edited
        timeline = TimelineRepository(database).load(project.id)

        report = validation.validate(
            timeline, media_durations={media.id: media.metadata.duration_seconds}
        )
        assert report.is_valid, [str(item) for item in report.errors]

    def test_it_reads_the_stored_plan_rather_than_recomputing_one(
        self, edited, database, runner
    ) -> None:
        # §81: the job result is the contract between stages. If this stage
        # re-derived the plan, editing the stored one would change nothing.
        project, _, _ = edited
        jobs = JobRepository(database)
        story = next(
            job for job in runner.jobs.list_jobs(project.id) if job.stage is JobStage.STORY
        )
        trimmed = {**story.result, "clips": story.result["clips"][:2]}
        jobs.update(story.model_copy(update={"result": trimmed}))

        edl = next(
            job for job in runner.jobs.list_jobs(project.id) if job.stage is JobStage.EDL
        )
        runner.jobs.requeue(edl.id)
        outcome = runner.run_job(edl.id)

        assert outcome.succeeded
        assert outcome.job.result["clips"] == 2

    def test_a_round_trip_through_the_database_preserves_the_timeline(
        self, edited, database
    ) -> None:
        project, _, outcomes = edited
        timeline = TimelineRepository(database).load(project.id)

        assert timeline.duration == pytest.approx(
            outcomes[JobStage.EDL].job.result["duration_seconds"], abs=0.01
        )
        assert [clip.clip_index for clip in timeline.video_clips()] == list(
            range(len(timeline.video_clips()))
        )


class TestCaptionsFromTranscript:
    """§71: caption timing always derives from transcript timestamps."""

    def test_captions_are_produced_from_the_real_transcript(self, edited, database) -> None:
        project, _, outcomes = edited

        assert outcomes[JobStage.EDL].job.result["captions"] > 0
        assert TimelineRepository(database).caption_count(project.id) > 0

    def test_every_caption_sits_inside_the_clip_it_belongs_to(
        self, edited, database
    ) -> None:
        project, _, _ = edited
        repository = TimelineRepository(database)
        clips = {clip.id: clip for clip in repository.list_clips(project.id)}

        for caption in repository.list_captions(project.id):
            clip = clips[caption.clip_id]
            assert clip.timeline_start - 1e-6 <= caption.timeline_start
            assert caption.timeline_end <= clip.timeline_end + 1e-6

    def test_captions_carry_word_timings_for_highlighting(self, edited, database) -> None:
        project, _, _ = edited
        captions = TimelineRepository(database).list_captions(project.id)

        assert any(caption.words for caption in captions)


class TestUserDecisionsSurviveARerun:
    """§78: the user has the last word, and re-running must not overrule it."""

    def test_a_disabled_clip_stays_disabled_across_a_rebuild(
        self, edited, database, runner
    ) -> None:
        project, _, _ = edited
        repository = TimelineRepository(database)
        target = repository.list_clips(project.id)[1]
        repository.set_enabled(project.id, target.id, enabled=False)

        edl = next(
            job for job in runner.jobs.list_jobs(project.id) if job.stage is JobStage.EDL
        )
        runner.jobs.requeue(edl.id)
        assert runner.run_job(edl.id).succeeded

        rebuilt = {clip.id: clip for clip in repository.list_clips(project.id)}
        assert target.id in rebuilt, "a rebuild must not rename the clips"
        assert rebuilt[target.id].enabled is False

    def test_re_running_the_stage_is_repeatable(self, edited, database, runner) -> None:
        # §127: a re-edit re-runs this stage against stored data, never the source.
        project, _, outcomes = edited
        first = outcomes[JobStage.EDL].job.result

        edl = next(
            job for job in runner.jobs.list_jobs(project.id) if job.stage is JobStage.EDL
        )
        runner.jobs.requeue(edl.id)
        outcome = runner.run_job(edl.id)

        assert outcome.succeeded
        assert outcome.job.result["clips"] == first["clips"]
        assert outcome.job.result["duration_seconds"] == pytest.approx(
            first["duration_seconds"]
        )


class TestEditingTheStoredTimeline:
    """The operations, applied to a timeline that came out of the database."""

    def test_deleting_a_clip_shortens_the_edit_without_losing_it(
        self, edited, database
    ) -> None:
        project, _, _ = edited
        repository = TimelineRepository(database)
        timeline = repository.load(project.id)
        target = timeline.video_clips()[0]

        after = operations.delete(timeline, target.id)
        repository.save_edit(project.id, after)

        reloaded = repository.load(project.id)
        assert reloaded.clip(target.id) is not None
        assert reloaded.clip(target.id).enabled is False
        assert reloaded.duration == pytest.approx(timeline.duration - target.duration)
        # The clips after it moved up, so the edit leaves no hole.
        report = validation.validate(reloaded)
        assert report.is_valid, [str(item) for item in report.errors]

    def test_the_edited_timeline_still_validates(self, edited, database) -> None:
        project, media, _ = edited
        repository = TimelineRepository(database)
        timeline = repository.load(project.id)

        edited_timeline = operations.move(timeline, timeline.video_clips()[-1].id, 0)
        repository.save_edit(project.id, edited_timeline)

        report = validation.validate(
            repository.load(project.id),
            media_durations={media.id: media.metadata.duration_seconds},
        )
        assert report.is_valid, [str(item) for item in report.errors]


class TestStagePipeline:
    def test_the_edl_stage_runs_after_story(self, edited, runner) -> None:
        project, _, _ = edited
        order = [
            job.stage
            for job in sorted(
                (job for job in runner.jobs.list_jobs(project.id) if job.completed_at),
                key=lambda job: job.completed_at,
            )
        ]

        assert order.index(JobStage.STORY) < order.index(JobStage.EDL)

    def test_the_runner_stops_at_the_frontier(
        self, media_service, project_manager, runner, frontier_check, reaction_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)
        frontier_check(runner, project.id)


class TestChatEditsTheTimeline:
    """The interaction layer's commands against a real, built timeline."""

    @pytest.fixture
    def service(self, database, config):
        from backend.interaction.service import InteractionService

        return InteractionService(database, config)

    def test_removing_a_clip_by_chat_shortens_the_edit(
        self, edited, database, service
    ) -> None:
        project, _, _ = edited
        repository = TimelineRepository(database)
        before = repository.duration_seconds(project.id)
        target = repository.list_clips(project.id)[1]

        service.handle(project.id, "delete clip 1")

        assert repository.duration_seconds(project.id) == pytest.approx(
            before - target.duration
        )

    def test_a_chat_removal_leaves_no_gap_in_the_timeline(
        self, edited, database, service
    ) -> None:
        # The defect this guards: a raw `enabled = 0` update leaves the clips
        # after it where they were, so the video keeps a hole the length of
        # what was removed.
        project, media, _ = edited
        service.handle(project.id, "delete clip 1")

        report = validation.validate(
            TimelineRepository(database).load(project.id),
            media_durations={media.id: media.metadata.duration_seconds},
        )
        assert report.is_valid, [str(item) for item in report.errors]

    def test_captions_move_with_the_clips_they_belong_to(
        self, edited, database, service
    ) -> None:
        # §71 has to stay true after an edit: a caption whose clip moved but
        # which did not would drift by the length of whatever was removed.
        project, _, _ = edited
        repository = TimelineRepository(database)
        service.handle(project.id, "delete clip 1")

        clips = {clip.id: clip for clip in repository.list_clips(project.id)}
        for caption in repository.list_captions(project.id):
            clip = clips[caption.clip_id]
            if not clip.enabled:
                continue
            assert clip.timeline_start - 1e-6 <= caption.timeline_start
            assert caption.timeline_end <= clip.timeline_end + 1e-6

    def test_reverting_restores_the_previous_edit(self, edited, database, service) -> None:
        # The parser wants an explicit version; a bare "undo" is not a command
        # it recognises today.
        project, _, _ = edited
        repository = TimelineRepository(database)
        before = repository.duration_seconds(project.id)

        service.handle(project.id, "delete clip 1")
        service.handle(project.id, "revert to version 1")

        assert repository.duration_seconds(project.id) == pytest.approx(before)

    def test_a_revert_leaves_the_timeline_contiguous(
        self, edited, database, service
    ) -> None:
        project, _, _ = edited
        service.handle(project.id, "delete clip 1")
        service.handle(project.id, "revert to version 1")

        report = validation.validate(TimelineRepository(database).load(project.id))
        assert report.is_valid, [str(item) for item in report.errors]


class TestNothingToEdit:
    """A recording with no moments in it must stop the pipeline, not break it."""

    @pytest.fixture
    def runner(self, database, paths, config, vision_provider):
        """A pipeline that finds nothing: silent audio, no on-screen text.

        Built rather than borrowed, because the shared fixtures deliberately
        script a detectable moment and this test needs the opposite.
        """
        workers = workers_through("edl")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(FakeSpeechProvider(silent=True))
        workers[JobStage.VISION] = VisionWorker(vision_provider)
        workers[JobStage.OCR] = OcrWorker(FakeOcrProvider(default=[]))
        return PipelineRunner(database, paths, config, workers=workers)

    def test_the_stage_skips_rather_than_failing(
        self, media_service, project_manager, runner, test_clip: Path
    ) -> None:
        # With nothing detected there are no moments, so STORY skips. It once
        # reported `"clips": 0` where the normal path reports a list, and the
        # EDL stage went down with a TypeError three frames deep (§95).
        project, _ = _project_with(media_service, project_manager, test_clip)
        outcomes = {outcome.job.stage: outcome for outcome in runner.run_project(project.id)}

        assert outcomes[JobStage.STORY].succeeded
        assert outcomes[JobStage.STORY].job.result["clips"] == []
        assert outcomes[JobStage.EDL].succeeded
        assert outcomes[JobStage.EDL].job.result["skipped"] is True

    def test_it_writes_no_timeline(
        self, media_service, project_manager, runner, database, test_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, test_clip)
        runner.run_project(project.id)

        assert TimelineRepository(database).clip_count(project.id) == 0
