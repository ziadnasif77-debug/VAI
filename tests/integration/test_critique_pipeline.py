"""Phase E acceptance: the pipeline reviews its own edit before rendering it.

    **Acceptance: the assembled edit is read clip by clip, a model says what is
    wrong with it, and what it asks for either changes the timeline or is
    recorded as refused.**

The unit tests own the rules -- which notes are honoured, which are capped,
which are thrown away. What only a pipeline run can show is that the stage is
wired: that it reads the timeline the EDL stage wrote, hands the model evidence
built from analysis that already exists, writes the result back where the
render stage will read it, and unloads the model before the render wants the
card.

The critical property is the last one in that list and the easiest to get
wrong: the RENDER stage must see the *revised* timeline. A review that produces
a beautiful report and a video cut exactly as it was before is worse than no
review at all, because it looks like it worked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.llm.fake_provider import FakeLLMProvider
from ai.ocr.fake_provider import FakeOcrProvider
from backend.core.models.enums import JobStage, VideoMode
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.critic.service import MAX_TRIM_FRACTION
from backend.database.repositories.timeline import TimelineRepository
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers.critique_worker import CritiqueWorker
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker
from tests.conftest import workers_through

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]

PROMPT_ID = "critique.edit_review"


@pytest.fixture
def ocr_provider() -> FakeOcrProvider:
    return FakeOcrProvider(default=[("VICTORY", 0.92)])


@pytest.fixture
def reviewed(database, paths, config, speech_provider, vision_provider, ocr_provider):
    """Run the pipeline as far as CRITIQUE, with the Critic on and a fake model."""

    def build(provider: FakeLLMProvider, **overrides):
        critique = config.critique.model_copy(update={"enabled": True, **overrides})
        with_critic = config.model_copy(update={"critique": critique})
        workers = workers_through("critique")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
        workers[JobStage.VISION] = VisionWorker(vision_provider)
        workers[JobStage.OCR] = OcrWorker(ocr_provider)
        workers[JobStage.CRITIQUE] = CritiqueWorker(provider)
        return PipelineRunner(database, paths, config=with_critic, workers=workers)

    return build


def _project(media_service, project_manager, clip: Path):
    project = project_manager.create(
        ProjectCreate(name="Critique", target_duration_seconds=600, mode=VideoMode.STORY)
    )
    media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project


def _model(notes: list[dict], verdict: str = "it plays fine") -> FakeLLMProvider:
    return FakeLLMProvider(responses={PROMPT_ID: {"verdict": verdict, "notes": notes}})


class TestTheStage:
    def test_it_runs_between_the_edl_and_the_render(
        self, media_service, project_manager, reviewed, reaction_clip: Path
    ) -> None:
        provider = _model([])
        project = _project(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o for o in reviewed(provider).run_project(project.id)}

        assert outcomes[JobStage.EDL].succeeded
        assert outcomes[JobStage.CRITIQUE].succeeded
        assert JobStage.RENDER not in outcomes, "the run was asked to stop at the critique"

    def test_the_model_is_shown_the_edit_and_then_released(
        self, media_service, project_manager, reviewed, reaction_clip: Path
    ) -> None:
        provider = _model([])
        project = _project(media_service, project_manager, reaction_clip)
        reviewed(provider).run_project(project.id)

        prompt_id, prompt = provider.calls[-1]
        assert prompt_id == PROMPT_ID
        # Numbered rows of the finished edit, not of the recording.
        assert "0. [" in prompt
        # §54: the render stages want the card next.
        assert provider.unload_count == 1

    def test_the_result_carries_the_verdict_and_what_was_done(
        self, media_service, project_manager, reviewed, reaction_clip: Path
    ) -> None:
        provider = _model([{"clip": 0, "action": "keep", "reason": "opens well"}], "tight")
        project = _project(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in reviewed(provider).run_project(project.id)}

        result = outcomes[JobStage.CRITIQUE].result
        assert result["verdict"] == "tight"
        assert result["reviewed_clips"] >= 1
        assert result["notes"][0]["action"] == "keep"
        assert result["applied"] == []


class TestTheEditActuallyChanges:
    def test_a_trim_reaches_the_stored_timeline(
        self, media_service, project_manager, reviewed, database, reaction_clip: Path
    ) -> None:
        project = _project(media_service, project_manager, reaction_clip)
        # Run once with nothing to do, to learn what the edit looks like.
        reviewed(_model([])).run_project(project.id)
        before = TimelineRepository(database).load(project.id).video_clips()

        # Then again, asking for a second off the front of the first clip.
        requeue = reviewed(
            _model([{"clip": 0, "action": "trim_start", "seconds": 1.0, "reason": "dead air"}])
        )
        job = next(j for j in requeue.jobs.list_jobs(project.id) if j.stage is JobStage.CRITIQUE)
        requeue.jobs.requeue(job.id)
        outcome = requeue.run_job(job.id)

        after = TimelineRepository(database).load(project.id).video_clips()
        assert outcome.succeeded
        assert outcome.job.result["applied"], outcome.job.result["refused"]
        # A trim is capped at half a shot (MAX_TRIM_FRACTION), and V2-P1's
        # walking splitter made the opening shot short enough for that cap to
        # bite: this fixture's first piece is 1.75s, so a second off the front
        # becomes 0.875s and the note records "asked for 1.0s; capped". The
        # rule is the Critic's, tested in tests/unit/test_critic.py; what this
        # test is about is whether the trim reaches the *stored* timeline.
        landed = min(1.0, before[0].duration * MAX_TRIM_FRACTION)
        assert after[0].source_in == pytest.approx(
            before[0].source_in + landed, abs=0.01
        )
        # And only that clip moved.
        assert after[0].source_out == pytest.approx(before[0].source_out, abs=0.01)

    def test_reporting_without_applying_leaves_the_edit_alone(
        self, media_service, project_manager, reviewed, database, reaction_clip: Path
    ) -> None:
        # §78 as a setting: the second opinion without the second editor.
        project = _project(media_service, project_manager, reaction_clip)
        reviewed(_model([])).run_project(project.id)
        before = TimelineRepository(database).load(project.id).video_clips()

        runner = reviewed(
            _model([{"clip": 0, "action": "trim_start", "seconds": 1.0}]), apply=False
        )
        job = next(j for j in runner.jobs.list_jobs(project.id) if j.stage is JobStage.CRITIQUE)
        runner.jobs.requeue(job.id)
        outcome = runner.run_job(job.id)

        after = TimelineRepository(database).load(project.id).video_clips()
        assert outcome.job.result["applied"] == []
        assert any("not applied" in note for note in outcome.job.result["refused"])
        assert after[0].source_in == pytest.approx(before[0].source_in, abs=0.001)


class TestItNeverBlocksTheVideo:
    def test_a_model_that_will_not_answer_still_leaves_a_renderable_edit(
        self, media_service, project_manager, reviewed, database, reaction_clip: Path
    ) -> None:
        # §95: the Critic is an improvement on a working default, never a
        # dependency of it.
        broken = FakeLLMProvider(fail_times=99)
        project = _project(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in reviewed(broken).run_project(project.id)}

        assert outcomes[JobStage.CRITIQUE].result["skipped"] is True
        assert broken.unload_count == 1
        assert TimelineRepository(database).clip_count(project.id) > 0

    def test_a_review_naming_a_clip_that_does_not_exist_changes_nothing(
        self, media_service, project_manager, reviewed, database, reaction_clip: Path
    ) -> None:
        project = _project(media_service, project_manager, reaction_clip)
        reviewed(_model([])).run_project(project.id)
        before = TimelineRepository(database).load(project.id).video_clips()

        runner = reviewed(_model([{"clip": 99, "action": "drop"}]))
        job = next(j for j in runner.jobs.list_jobs(project.id) if j.stage is JobStage.CRITIQUE)
        runner.jobs.requeue(job.id)
        outcome = runner.run_job(job.id)

        assert outcome.succeeded
        assert outcome.job.result["skipped"] is True
        after = TimelineRepository(database).load(project.id).video_clips()
        assert [clip.source_in for clip in after] == [clip.source_in for clip in before]

    def test_the_stage_is_a_no_op_when_the_critic_is_off(
        self,
        database,
        paths,
        config,
        speech_provider,
        vision_provider,
        ocr_provider,
        media_service,
        project_manager,
        reaction_clip: Path,
    ) -> None:
        # The shipped test configuration has it off, which is the path every
        # other integration test in this repository runs through.
        workers = workers_through("critique")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
        workers[JobStage.VISION] = VisionWorker(vision_provider)
        workers[JobStage.OCR] = OcrWorker(ocr_provider)
        runner = PipelineRunner(database, paths, config, workers=workers)

        project = _project(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        assert outcomes[JobStage.CRITIQUE].result["skipped"] is True
        assert outcomes[JobStage.CRITIQUE].result["reviewed_clips"] == 0
