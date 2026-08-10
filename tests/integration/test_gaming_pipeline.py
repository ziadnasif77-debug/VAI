"""Phase 5 acceptance: gameplay events, detected without a game profile.

    **Acceptance: gameplay moments detected with timestamps, without a game
    profile (§23). A profile improves accuracy; it is never required.**

That is one claim with two halves, and the second is what makes the first
meaningful. A pipeline that only worked with a profile would be a pipeline that
worked for the handful of games somebody wrote profiles for — and §111 says one
real game is validated before more are written, so that would be one game.

Both halves are run through the whole pipeline on real files: the same
recording, once as `game: auto` and once against a profile written for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from backend.core.models.enums import GameEventType, JobStage
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.repositories.gaming import GameEventRepository, OcrRepository
from backend.gaming.profiles import clear_profile_cache
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers import default_workers
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


def _runner(database, paths, config, speech, vision, ocr) -> PipelineRunner:
    workers = default_workers()
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech)
    workers[JobStage.VISION] = VisionWorker(vision)
    workers[JobStage.OCR] = OcrWorker(ocr)
    return PipelineRunner(database, paths, config, workers=workers)


def _project_with(media_service, project_manager, clip: Path, *, game: str = "auto"):
    project = project_manager.create(
        ProjectCreate(name="Gaming", target_duration_seconds=600, game=game)
    )
    media = media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project, media


@pytest.fixture
def ocr_provider() -> FakeOcrProvider:
    """OCR that reads "VICTORY" off every candidate frame.

    Scripted rather than real because what is under test is the pipeline's
    handling of on-screen text, not a recogniser's accuracy on a colour bar.
    """
    return FakeOcrProvider(default=[("VICTORY", 0.92)])


class TestWithoutAProfile:
    """The acceptance criterion itself."""

    def test_events_are_detected_with_timestamps_and_no_profile(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, media = _project_with(media_service, project_manager, scene_clip, game="auto")

        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}
        assert outcomes[JobStage.GAME_EVENTS].succeeded

        result = outcomes[JobStage.GAME_EVENTS].job.result
        assert result["game_profile"] == "generic"
        assert result["events"] > 0

        events = GameEventRepository(database).list_for_media(media.id)
        assert events
        # Timestamps, which is what makes an event usable at all.
        assert all(0.0 <= event.start_seconds <= event.end_seconds <= 10.0 for event in events)
        assert all(event.game_profile == "generic" for event in events)
        assert all(0.0 < event.confidence <= 1.0 for event in events)

    def test_on_screen_text_is_read_and_timestamped(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        # §25: every OCR result must have a timestamp.
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, media = _project_with(media_service, project_manager, scene_clip)
        runner.run_project(project.id)

        detections = OcrRepository(database).list_for_media(media.id)
        assert detections
        assert all(detection.timestamp > 0.0 for detection in detections)
        assert all(detection.text for detection in detections)
        # No profile, so the whole frame was read.
        assert {detection.region for detection in detections} == {"full_frame"}

    def test_the_text_becomes_a_named_event(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        # Generic wording carries "VICTORY" without any game knowledge (§23).
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, media = _project_with(media_service, project_manager, scene_clip)
        runner.run_project(project.id)

        events = GameEventRepository(database).list_for_media(media.id)
        assert any(event.event_type is GameEventType.VICTORY for event in events)
        assert any(event.is_named for event in events)

    def test_events_carry_every_detector_that_saw_them(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        # §26's `sources`, and the thing that distinguishes a corroborated
        # event from a lone audio spike.
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, media = _project_with(media_service, project_manager, scene_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        events = GameEventRepository(database).list_for_media(media.id)
        assert all(event.sources for event in events)
        assert outcomes[JobStage.GAME_EVENTS].result["multi_source_events"] >= 1

    def test_correlation_produces_fewer_events_than_observations(
        self, media_service, project_manager, paths, database, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        # §27: agreeing detectors raise confidence, they do not multiply events.
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, _ = _project_with(media_service, project_manager, scene_clip)
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        result = outcomes[JobStage.GAME_EVENTS].result
        assert result["observations"] > result["events"]


class TestWithAProfile:
    """A profile improves accuracy; it does not enable the feature."""

    def test_a_profile_restricts_ocr_to_its_regions(
        self, media_service, project_manager, database, paths, config, tmp_path,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        _install_profile(paths.profiles_dir, "testgame")
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, media = _project_with(
            media_service, project_manager, scene_clip, game="testgame"
        )
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        assert outcomes[JobStage.OCR].result["mode"] == "regions"
        detections = OcrRepository(database).list_for_media(media.id)
        assert detections
        # Read from the declared boxes, not the whole frame.
        assert {detection.region for detection in detections} <= {"banner", "kill_feed"}

    def test_a_profile_rule_produces_a_more_specific_event(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, scene_clip: Path,
    ) -> None:
        _install_profile(paths.profiles_dir, "testgame")
        # Wording only this game uses: generic patterns cannot read it.
        ocr = FakeOcrProvider(default=[("ROUND WON", 0.92)])
        runner = _runner(database, paths, config, speech_provider, vision_provider, ocr)
        project, media = _project_with(
            media_service, project_manager, scene_clip, game="testgame"
        )
        outcomes = {o.job.stage: o.job for o in runner.run_project(project.id)}

        assert outcomes[JobStage.GAME_EVENTS].result["profile_exact"] is True
        assert outcomes[JobStage.GAME_EVENTS].result["game_profile"] == "testgame"
        events = GameEventRepository(database).list_for_media(media.id)
        assert any(event.event_type is GameEventType.VICTORY for event in events)
        assert all(event.game_profile == "testgame" for event in events)

    def test_an_unknown_game_falls_back_without_failing(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, _ = _project_with(
            media_service, project_manager, scene_clip, game="unwritten_game"
        )
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert outcomes[JobStage.GAME_EVENTS].succeeded
        result = outcomes[JobStage.GAME_EVENTS].job.result
        assert result["game_profile"] == "generic"
        assert result["profile_requested"] == "unwritten_game"
        # The substitution is recorded, not hidden.
        assert result["profile_exact"] is False


class TestDegradation:
    def test_missing_ocr_degrades_rather_than_failing(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, reaction_clip: Path,
    ) -> None:
        # §95: OCR failing falls back to vision and audio. It never invents
        # text. Run on the clip that carries real impacts, so "the sources that
        # did run" have something to report -- otherwise the test would be
        # demanding that the pipeline invent events from silence.
        runner = _runner(
            database, paths, config, speech_provider, vision_provider,
            FakeOcrProvider(available=False),
        )
        project, media = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert outcomes[JobStage.OCR].succeeded
        assert outcomes[JobStage.OCR].job.result["skipped"] is True
        assert OcrRepository(database).count_for_media(media.id) == 0
        # Events are still produced, from the sources that did run.
        assert outcomes[JobStage.GAME_EVENTS].succeeded
        assert outcomes[JobStage.GAME_EVENTS].job.result["events"] > 0

    def test_re_running_replaces_rather_than_appends(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, media = _project_with(media_service, project_manager, scene_clip)
        runner.run_project(project.id)
        first = GameEventRepository(database).count_for_media(media.id)

        job = next(
            j for j in runner.jobs.list_jobs(project.id) if j.stage is JobStage.GAME_EVENTS
        )
        runner.jobs.requeue(job.id)
        assert runner.run_job(job.id).succeeded
        assert GameEventRepository(database).count_for_media(media.id) == first


class TestStageWiring:
    def test_the_gaming_stages_run_after_everything_they_read(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, _ = _project_with(media_service, project_manager, scene_clip)
        order = [o.job.stage for o in runner.run_project(project.id) if o.succeeded]

        assert order.index(JobStage.FRAMES) < order.index(JobStage.OCR)
        for dependency in (
            JobStage.SCENES, JobStage.VISION, JobStage.OCR,
            JobStage.AUDIO_EVENTS, JobStage.TRANSCRIPT,
        ):
            assert order.index(dependency) < order.index(JobStage.GAME_EVENTS)

    def test_the_runner_stops_at_the_frontier(
        self, media_service, project_manager, database, paths, config,
        speech_provider, vision_provider, ocr_provider, scene_clip: Path,
    ) -> None:
        runner = _runner(
            database, paths, config, speech_provider, vision_provider, ocr_provider
        )
        project, _ = _project_with(media_service, project_manager, scene_clip)
        runner.run_project(project.id)

        assert runner.run_next(project.id) is None
        frontier = next(
            job
            for job in runner.jobs.list_jobs(project.id)
            if job.stage not in runner.supported_stages
        )
        # Which stage that is moves as phases land, so this asserts the
        # property rather than the name: the frontier waits, it does not fail.
        assert frontier.stage not in runner.supported_stages
        assert frontier.error_code is None


def _install_profile(profiles_dir: Path, game: str) -> Path:
    """Write a profile into the resolved profiles directory."""
    directory = Path(profiles_dir) / game
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.json").write_text(
        json.dumps(
            {
                "id": game,
                "name": "Test Game",
                "regions": {
                    "banner": {"x": 0.2, "y": 0.35, "width": 0.6, "height": 0.3},
                    "kill_feed": {"x": 0.7, "y": 0.05, "width": 0.28, "height": 0.2},
                },
                "ocr_regions": ["banner", "kill_feed"],
                "event_rules": [
                    {
                        "event_type": "victory",
                        "patterns": ["round\\s+won"],
                        "regions": ["banner"],
                        "confidence": 0.95,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    clear_profile_cache()
    return directory
