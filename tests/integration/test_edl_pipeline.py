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

import re
from itertools import pairwise
from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from ai.speech.fake_provider import FakeSpeechProvider
from backend.core.errors import ErrorCode
from backend.core.models.enums import JobStage, JobStatus, VideoMode
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers.edl_worker import EdlWorker
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
def config(config):
    """The shared config with the screen guard off: this file tests §40-§42.

    The guard's whole job is to move and drop clip boundaries -- advance dead
    openings, split slabs at seams, drop slivers shorter than a shot -- which
    by design breaks the plan↔timeline parity these tests assert. On this
    fixture's seconds-long recording the 4 s recording-start guard swallows a
    real clip whole, so with it on, every test here would be asserting on the
    guard, not on the EDL. Same precedent as the narration/Director/Critic
    switches in the shared fixture: a component that edits the clip list is
    exercised in its own tests (``test_screen_guard.py``), and
    ``TestScreenGuardWire`` below keeps the worker-side wire honest.
    """
    guard = config.narrative.screen_guard.model_copy(update={"enabled": False})
    narrative = config.narrative.model_copy(update={"screen_guard": guard})
    return config.model_copy(update={"narrative": narrative})


@pytest.fixture
def runner(database, paths, config, speech_provider, vision_provider, ocr_provider):
    workers = workers_through("edl")
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
    workers[JobStage.VISION] = VisionWorker(vision_provider)
    workers[JobStage.OCR] = OcrWorker(ocr_provider)
    return PipelineRunner(database, paths, config, workers=workers)


def _project_with(media_service, project_manager, clip: Path, *, mode=VideoMode.STORY):
    project = project_manager.create(
        # Captions became opt-in at import (owner: an unticked box writes
        # nothing on the frame). This file's caption tests are about §71
        # timing, so the project opts in the way a captioned project does.
        ProjectCreate(
            name="EDL", target_duration_seconds=600, mode=mode, captions_enabled=True
        )
    )
    media = media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project, media


@pytest.fixture
def edited(media_service, project_manager, runner, database, config, reaction_clip: Path):
    """A project run all the way to a stored timeline, with at least two clips.

    The hook is requested explicitly. Chronological became the product default,
    and on this recording the two moments are continuous footage -- the
    refinement rightly merges them into one clip, but a one-clip timeline
    cannot exercise "delete clip 1", caption movement, or id stability across a
    rebuild. The hook order (teaser first, body second) yields two clips whose
    ids survive an EDL rebuild, which is the shape every test below leans on.

    The media is re-read afterwards: the record handed back by ``import_media``
    predates PROBE, so its duration is still unknown at that point.
    """
    from backend.interaction.service import InteractionService

    project, media = _project_with(media_service, project_manager, reaction_clip)
    InteractionService(database, config).handle(project.id, "use a hook")
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
        # However many clips the trimmed plan holds is however many the EDL
        # lays out -- the assertion is fidelity, not a magic number.
        assert outcome.job.result["clips"] == len(trimmed["clips"])

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
class TestScreenGuardWire:
    """The guard, re-enabled: the EDL worker really consults it (§77).

    The guard's arithmetic lives in ``test_screen_guard.py``; what only the
    pipeline can show is that the worker feeds it the stored states and seams
    and honours its verdict. On a recording this short the recording-start
    guard dominates: every surviving clip opens at or past it, or nothing
    survives and the stage says so in as many words.
    """

    def test_no_stored_clip_opens_inside_the_start_guard(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, reaction_clip: Path,
    ) -> None:
        from backend.interaction.service import InteractionService

        guard = config.narrative.screen_guard.model_copy(update={"enabled": True})
        narrative = config.narrative.model_copy(update={"screen_guard": guard})
        guarded = config.model_copy(update={"narrative": narrative})

        workers = workers_through("edl")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
        workers[JobStage.VISION] = VisionWorker(vision_provider)
        workers[JobStage.OCR] = OcrWorker(ocr_provider)
        runner = PipelineRunner(database, paths, guarded, workers=workers)

        project, _ = _project_with(media_service, project_manager, reaction_clip)
        InteractionService(database, guarded).handle(project.id, "use a hook")
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert JobStage.EDL in outcomes
        if not outcomes[JobStage.EDL].succeeded:
            # [P0.2.2] the guard refusing everything is a failure that says so
            assert outcomes[JobStage.EDL].error_code is ErrorCode.INVALID_EDL
            assert "dead screen time" in (outcomes[JobStage.EDL].job.error_message or "")
            return
        clips = TimelineRepository(database).list_clips(project.id)
        assert clips
        floor = guard.recording_start_guard_seconds
        for clip in clips:
            assert clip.source_in >= floor - 1e-6, (
                f"a clip opens at {clip.source_in:.2f}s, inside the "
                f"{floor:.0f}s recording-start guard"
            )


class TestPlannedFrameReads:
    """P0.2.2: the EDL stage reads the base frames the edit will use.

    The pass is off in the shared config, for the reason every model is: it
    runs whatever OCR engine is installed. Here it runs with the fake
    injected, and what the test proves is the wire -- the frames inside the
    planned clips are read, their reads land in ``ocr_results`` beside the
    stage's own, and the frames are marked so a second run reads nothing.
    """

    @pytest.fixture
    def reading(self, database, paths, config, speech_provider, vision_provider):
        reads = config.narrative.planned_frame_reads.model_copy(
            update={"enabled": True, "margin_seconds": 3.0, "min_gap_seconds": 0.0}
        )
        narrative = config.narrative.model_copy(update={"planned_frame_reads": reads})
        enabled = config.model_copy(update={"narrative": narrative})
        planned = FakeOcrProvider(default=[("PLANNED FRAME", 0.99)])
        workers = workers_through("edl")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
        workers[JobStage.VISION] = VisionWorker(vision_provider)
        workers[JobStage.OCR] = OcrWorker(FakeOcrProvider(default=[("VICTORY", 0.92)]))
        workers[JobStage.EDL] = EdlWorker(ocr_provider=planned)
        return PipelineRunner(database, paths, enabled, workers=workers), planned

    def test_the_planned_base_frames_are_read_and_marked(
        self, media_service, project_manager, reading, database, reaction_clip: Path
    ) -> None:
        from backend.database.repositories.frames import FrameRepository
        from backend.database.repositories.gaming import OcrRepository

        runner, planned = reading
        project, media = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}
        assert outcomes[JobStage.EDL].succeeded, outcomes[JobStage.EDL].job.error_message

        result = outcomes[JobStage.EDL].job.result
        summary = result["planned_frame_reads"]
        assert summary["frames"] > 0, "the fixture recording has base frames inside its clips"
        assert planned.load_count == 1 and planned.unload_count == 1

        clips = TimelineRepository(database).list_clips(project.id)
        frames = FrameRepository(database).list_for_media(media.id, level="base")
        inside = [
            frame
            for frame in frames
            if any(c.source_in - 3.0 <= frame.timestamp <= c.source_out + 3.0 for c in clips)
        ]
        assert inside and all(frame.analyzed for frame in inside)
        assert not any(frame.analyzed for frame in frames if frame not in inside)

        reads = OcrRepository(database).list_for_media(media.id)
        assert any(item.text == "PLANNED FRAME" for item in reads), "the pass appended its reads"
        assert any(item.text == "VICTORY" for item in reads), "and kept the stage's own"

    def test_a_second_run_reads_nothing_new(
        self, media_service, project_manager, reading, database, reaction_clip: Path
    ) -> None:
        runner, planned = reading
        project, _ = _project_with(media_service, project_manager, reaction_clip)
        runner.run_project(project.id)
        first = len(planned.read_paths)
        assert first > 0

        edl = next(
            job for job in runner.jobs.list_jobs(project.id) if job.stage is JobStage.EDL
        )
        runner.jobs.requeue(edl.id)
        outcome = runner.run_job(edl.id)
        assert outcome.succeeded
        assert outcome.job.result["planned_frame_reads"]["frames"] == 0
        assert len(planned.read_paths) == first, "the marked frames were not read again"

    def test_every_clip_refused_fails_the_stage_by_name(
        self, media_service, project_manager, database, paths, config, speech_provider,
        vision_provider, reaction_clip: Path
    ) -> None:
        # [P0.2.2] The pass reads RESUME off every base frame: the whole
        # recording is a pause menu and every planned clip is refused. The
        # stage FAILS with INVALID_EDL and a message that carries the tally
        # and the builder's notes. The first cut returned a "skipped" result
        # instead, every later stage completed on nothing, and the interface
        # showed a green pipeline with no video behind it.
        reads = config.narrative.planned_frame_reads.model_copy(
            update={"enabled": True, "margin_seconds": 3.0, "min_gap_seconds": 0.0}
        )
        narrative = config.narrative.model_copy(update={"planned_frame_reads": reads})
        enabled = config.model_copy(update={"narrative": narrative})
        workers = workers_through("edl")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
        workers[JobStage.VISION] = VisionWorker(vision_provider)
        workers[JobStage.OCR] = OcrWorker(FakeOcrProvider(default=[("VICTORY", 0.92)]))
        workers[JobStage.EDL] = EdlWorker(ocr_provider=FakeOcrProvider(default=[("RESUME", 1.0)]))
        runner = PipelineRunner(database, paths, enabled, workers=workers)

        project, _ = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}
        edl = outcomes[JobStage.EDL]
        assert not edl.succeeded
        assert edl.error_code is ErrorCode.INVALID_EDL
        message = edl.job.error_message or ""
        assert re.search(r"Every one of the \d+ planned clips was refused", message), message
        assert "pause: " in message, "the tally names what was on screen"
        # Nothing after the EDL ran: the dependents are not completed.
        later = [
            job
            for job in runner.jobs.list_jobs(project.id)
            if job.stage in (JobStage.CRITIQUE, JobStage.RENDER, JobStage.QA)
        ]
        assert all(job.status is not JobStatus.COMPLETED for job in later)

    def test_a_guard_that_keeps_nothing_fails_the_stage_by_name(
        self, media_service, project_manager, database, paths, config, speech_provider,
        vision_provider, ocr_provider, reaction_clip: Path
    ) -> None:
        # [P0.2.2] A recording-start guard longer than the recording: every
        # planned clip opens inside it and the guard keeps nothing. Same
        # failure, same visibility, its own wording.
        guard = config.narrative.screen_guard.model_copy(
            update={"enabled": True, "recording_start_guard_seconds": 100000.0}
        )
        narrative = config.narrative.model_copy(update={"screen_guard": guard})
        guarded = config.model_copy(update={"narrative": narrative})
        workers = workers_through("edl")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
        workers[JobStage.VISION] = VisionWorker(vision_provider)
        workers[JobStage.OCR] = OcrWorker(ocr_provider)
        runner = PipelineRunner(database, paths, guarded, workers=workers)

        project, _ = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}
        edl = outcomes[JobStage.EDL]
        assert not edl.succeeded
        assert edl.error_code is ErrorCode.INVALID_EDL
        assert "dead screen time" in (edl.job.error_message or "")
        assert "recording-start guard" in (edl.job.error_message or "")
