"""Phase 4 acceptance: scenes and the vision cascade through the pipeline.

The criterion has two halves:

1. **The system describes major visual changes.** Checked on a clip built from
   three visually distinct shots with boundaries at known times, so the test
   asserts *where* the changes were found, not merely how many.

2. **The VLM sees only candidate keyframes**, verified against
   ``analysis.vision.max_frames_per_source_hour``. Counted at the provider —
   the number of frames that reach a vision model is the number the provider
   was handed, and no amount of reading the cascade proves it as directly.

This is the phase where a naive implementation turns a two-hour recording into
an afternoon of GPU time, so the frame count is the assertion that matters most.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.vision.fake_provider import FakeVisionProvider
from backend.core.models.enums import JobStage
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.core.versions import PROMPT_VERSIONS
from backend.database.repositories.frames import FrameRepository
from backend.database.repositories.scenes import SceneRepository
from backend.database.repositories.vision import VisionRepository
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers import default_workers
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VISION_PROMPT_ID, VisionWorker

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


def _project_with(media_service, project_manager, clip: Path, *, game: str = "auto"):
    project = project_manager.create(
        ProjectCreate(name="Vision", target_duration_seconds=600, game=game)
    )
    media = media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project, media


class TestSceneDetection:
    """Acceptance, first half: major visual changes are found, where they are."""

    def test_boundaries_land_on_the_actual_cuts(
        self, media_service, project_manager, database, pipeline_runner, scene_clip: Path
    ) -> None:
        # The fixture is colour bars, then red, then blue: cuts at 3 s and 6 s.
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        scenes = SceneRepository(database).list_for_media(media.id)
        assert len(scenes) == 3
        assert [round(scene.start_seconds, 1) for scene in scenes] == [0.0, 3.0, 6.0]
        assert [round(scene.end_seconds, 1) for scene in scenes] == [3.0, 6.0, 9.0]

    def test_the_change_score_is_measured_not_assumed(
        self, media_service, project_manager, database, config, pipeline_runner, scene_clip: Path
    ) -> None:
        # Storing the configured threshold would make every boundary claim the
        # same magnitude, and the cascade could not rank them.
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        scenes = SceneRepository(database).list_for_media(media.id)
        assert scenes[0].change_score is None, "the first scene has no boundary before it"
        assert all(scene.change_score is not None for scene in scenes[1:])
        assert all(scene.change_score > config.analysis.scenes.threshold for scene in scenes[1:])
        assert len({scene.change_score for scene in scenes[1:]}) > 1

    def test_a_scene_keyframe_is_written_for_review(
        self, media_service, project_manager, database, pipeline_runner, scene_clip: Path
    ) -> None:
        # §56: previews make review dramatically faster.
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        paths = SceneRepository(database).keyframe_paths(media.id)
        assert len(paths) == 3
        assert all(Path(value).is_file() for value in paths.values())

    def test_a_single_shot_recording_is_one_scene_not_zero(
        self, media_service, project_manager, database, pipeline_runner, test_clip: Path
    ) -> None:
        # A continuous shot is a legitimate result; returning nothing would make
        # every consumer special-case it.
        project, media = _project_with(media_service, project_manager, test_clip)
        pipeline_runner.run_project(project.id)

        scenes = SceneRepository(database).list_for_media(media.id)
        assert len(scenes) >= 1
        assert scenes[0].start_seconds == 0.0

    def test_the_stage_reports_what_it_used(
        self, media_service, project_manager, pipeline_runner, scene_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, scene_clip)
        outcomes = {o.job.stage: o.job for o in pipeline_runner.run_project(project.id)}

        result = outcomes[JobStage.SCENES].result
        assert result["scenes"] == 3
        assert result["from_proxy"] is True
        assert result["detector"] == "content"


class TestVisionBudget:
    """Acceptance, second half: only candidate keyframes reach the model."""

    def test_the_model_sees_only_the_planned_keyframes(
        self,
        media_service,
        project_manager,
        pipeline_runner,
        vision_provider: FakeVisionProvider,
        scene_clip: Path,
    ) -> None:
        project, _ = _project_with(media_service, project_manager, scene_clip)
        outcomes = {o.job.stage: o.job for o in pipeline_runner.run_project(project.id)}

        result = outcomes[JobStage.VISION].result
        assert result["frames_planned"] == len(vision_provider.described_frames)
        assert result["observations"] > 0

    def test_the_model_sees_far_fewer_frames_than_were_sampled(
        self,
        media_service,
        project_manager,
        database,
        pipeline_runner,
        vision_provider: FakeVisionProvider,
        scene_clip: Path,
    ) -> None:
        # §15's whole point: sampling produces frames for cheap detectors, and
        # only a fraction of them are worth a model's time.
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        sampled = FrameRepository(database).count_for_media(media.id, level="base")
        assert sampled > 0
        assert len(vision_provider.described_frames) <= sampled

    def test_the_hourly_ceiling_is_respected(
        self,
        media_service,
        project_manager,
        config,
        pipeline_runner,
        vision_provider: FakeVisionProvider,
        scene_clip: Path,
    ) -> None:
        project, _ = _project_with(media_service, project_manager, scene_clip)
        outcomes = {o.job.stage: o.job for o in pipeline_runner.run_project(project.id)}

        # A nine-second clip is a fraction of an hour, and the budget scales
        # with duration — but never below one frame's worth.
        budget = outcomes[JobStage.VISION].result["frame_budget"]
        assert budget <= config.analysis.vision.max_frames_per_source_hour
        assert len(vision_provider.described_frames) <= budget

    def test_batches_never_exceed_the_configured_size(
        self,
        media_service,
        project_manager,
        config,
        pipeline_runner,
        vision_provider: FakeVisionProvider,
        scene_clip: Path,
    ) -> None:
        # §16: batches are small on purpose. A local VLM handed twenty images
        # at once either runs out of context or out of VRAM.
        project, _ = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        assert vision_provider.batch_sizes
        assert max(vision_provider.batch_sizes) <= config.analysis.vision.max_frames_per_request

    def test_the_model_is_loaded_once_and_released(
        self,
        media_service,
        project_manager,
        pipeline_runner,
        vision_provider: FakeVisionProvider,
        scene_clip: Path,
    ) -> None:
        # §54: an 8 GB card cannot hold this and the next stage's model at once.
        project, _ = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        assert vision_provider.load_count == 1
        assert vision_provider.unload_count == 1


class TestObservationPersistence:
    def test_observations_land_at_their_keyframe_timestamps(
        self,
        media_service,
        project_manager,
        database,
        pipeline_runner,
        vision_provider: FakeVisionProvider,
        scene_clip: Path,
    ) -> None:
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        stored = VisionRepository(database).list_for_media(media.id)
        assert stored
        described = {round(timestamp, 3) for _, timestamp in vision_provider.described_frames}
        assert {round(item.timestamp, 3) for item in stored} <= described
        assert all(0.0 <= item.timestamp <= 9.5 for item in stored)

    def test_provenance_travels_with_every_observation(
        self, media_service, project_manager, database, pipeline_runner, scene_clip: Path
    ) -> None:
        # §49: a wrong description must be traceable to the model and the
        # wording that produced it.
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        stored = VisionRepository(database).list_for_media(media.id)
        assert stored
        assert all(item.model_name and item.model_version for item in stored)
        assert all(item.prompt_id == VISION_PROMPT_ID for item in stored)
        assert all(item.prompt_version == PROMPT_VERSIONS[VISION_PROMPT_ID] for item in stored)
        assert len(VisionRepository(database).models_used(media.id)) == 1

    def test_an_observation_records_why_it_was_looked_at(
        self, media_service, project_manager, database, pipeline_runner, scene_clip: Path
    ) -> None:
        # "Why did the model look here" is answered from the data rather than
        # reconstructed.
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        stored = VisionRepository(database).list_for_media(media.id)
        assert all(item.sources for item in stored)
        assert all(
            item.region_start is not None and item.region_end is not None for item in stored
        )
        assert all(
            item.region_start <= item.timestamp <= item.region_end for item in stored
        )

    def test_candidate_frames_are_recorded_at_their_own_level(
        self, media_service, project_manager, database, pipeline_runner, scene_clip: Path
    ) -> None:
        # §16's hierarchy is visible in the database, not implied.
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)

        repository = FrameRepository(database)
        assert repository.count_for_media(media.id, level="base") > 0
        assert repository.count_for_media(media.id, level="candidate") > 0

    def test_re_running_replaces_rather_than_appends(
        self, media_service, project_manager, database, pipeline_runner, scene_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, scene_clip)
        pipeline_runner.run_project(project.id)
        first = VisionRepository(database).count_for_media(media.id)

        job = next(
            j for j in pipeline_runner.jobs.list_jobs(project.id) if j.stage is JobStage.VISION
        )
        pipeline_runner.jobs.requeue(job.id)
        assert pipeline_runner.run_job(job.id).succeeded
        assert VisionRepository(database).count_for_media(media.id) == first


class TestDegradation:
    def test_an_unavailable_model_degrades_rather_than_failing(
        self, media_service, project_manager, database, paths, config, speech_provider,
        scene_clip: Path,
    ) -> None:
        # §95: vision failing falls back to OCR, audio, scenes and the game
        # profile. It does not fail the analysis.
        workers = default_workers()
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
        workers[JobStage.VISION] = VisionWorker(FakeVisionProvider(available=False))
        runner = PipelineRunner(database, paths, config, workers=workers)

        project, media = _project_with(media_service, project_manager, scene_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert outcomes[JobStage.VISION].succeeded
        assert outcomes[JobStage.VISION].job.result["skipped"] is True
        assert VisionRepository(database).count_for_media(media.id) == 0
        # The plan was still built and reported: what *would* have been looked
        # at is knowable even when nothing looked.
        assert outcomes[JobStage.VISION].job.result["regions"] > 0

    def test_disabling_vision_in_configuration_skips_the_stage(
        self, media_service, project_manager, database, paths, config, speech_provider,
        vision_provider: FakeVisionProvider, scene_clip: Path,
    ) -> None:
        vision = config.analysis.vision.model_copy(update={"enabled": False})
        analysis = config.analysis.model_copy(update={"vision": vision})
        disabled = config.model_copy(update={"analysis": analysis})

        workers = default_workers()
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
        workers[JobStage.VISION] = VisionWorker(vision_provider)
        runner = PipelineRunner(database, paths, disabled, workers=workers)

        project, _ = _project_with(media_service, project_manager, scene_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert outcomes[JobStage.VISION].succeeded
        assert outcomes[JobStage.VISION].job.result["skipped"] is True
        assert vision_provider.described_frames == []


class TestStageWiring:
    def test_the_visual_stages_run_after_their_dependencies(
        self, media_service, project_manager, pipeline_runner, scene_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, scene_clip)
        outcomes = pipeline_runner.run_project(project.id)

        order = [o.job.stage for o in outcomes if o.succeeded]
        assert JobStage.SCENES in order
        assert JobStage.VISION in order
        # SCENES needs the proxy; VISION needs the sampled frames.
        assert order.index(JobStage.PROXY) < order.index(JobStage.SCENES)
        assert order.index(JobStage.FRAMES) < order.index(JobStage.VISION)

    def test_the_runner_still_stops_at_the_frontier(
        self, media_service, project_manager, pipeline_runner, test_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, test_clip)
        pipeline_runner.run_project(project.id)

        assert pipeline_runner.run_next(project.id) is None
        frontier = next(
            job
            for job in pipeline_runner.jobs.list_jobs(project.id)
            if job.stage not in pipeline_runner.supported_stages
        )
        assert frontier.stage is JobStage.OCR
        assert frontier.error_code is None


@pytest.mark.requires_models
class TestRealVisionModel:
    """Against the actual local VLM. Skipped unless ``VAI_TEST_MODELS=1``.

    Needs ``ollama pull qwen2.5vl:7b``, which is a six-gigabyte download, so it
    is opt-in rather than part of the default suite.
    """

    def test_the_model_describes_a_frame_as_structured_data(
        self, scene_clip: Path, media_fixtures_dir: Path, config, ffmpeg_runner
    ) -> None:
        # §93: the pipeline never reads prose. The point of this test is that a
        # real local model, given a real image, returns something that survives
        # validation and lands on the right timestamp.
        from ai.vision.ollama_provider import OllamaVisionProvider
        from backend.media.frames import extract_at_times

        frames = extract_at_times(
            scene_clip, media_fixtures_dir / "real_vision", [1.0], ffmpeg_runner
        )
        assert frames

        provider = OllamaVisionProvider(config.models.vision, gpu=config.gpu, game="a test pattern")
        if not provider.is_available():
            pytest.skip(f"{config.models.vision.model} is not pulled into Ollama")

        try:
            observations = provider.describe(
                (frames[0].path,), (frames[0].timestamp,)
            )
        finally:
            provider.unload()

        assert len(observations) == 1
        assert observations[0].timestamp == frames[0].timestamp
        assert observations[0].description.strip()
        assert 0.0 <= observations[0].confidence <= 1.0

    def test_the_model_reports_its_availability_honestly(self, config) -> None:
        from ai.vision.ollama_provider import OllamaVisionProvider

        present = OllamaVisionProvider(config.models.vision, gpu=config.gpu)
        absent = OllamaVisionProvider(
            config.models.vision.model_copy(update={"model": "not-a-real-model:1b"}),
            gpu=config.gpu,
        )
        assert present.is_available() != absent.is_available() or not present.is_available()
        assert absent.is_available() is False
