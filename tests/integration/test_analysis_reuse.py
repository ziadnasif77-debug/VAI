"""Cross-project analysis reuse (SPEC §48, §49, §127).

The measured cost this exists to delete: the same 67-minute recording imported
into two projects ran the full analysis chain twice — over an hour of
duplicate GPU work for byte-identical conclusions.

The decisive assertion in every reuse test is about the *providers*: reuse
means the model was never asked. Row counts matching is nice; the fake
transcriber's call counter staying flat is the claim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from ai.speech.fake_provider import FakeSpeechProvider
from ai.vision.fake_provider import FakeVisionProvider
from backend.core.models.enums import JobStage
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker
from tests.conftest import workers_through

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


class CountingSpeech(FakeSpeechProvider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def transcribe(self, *args, **kwargs):
        self.calls += 1
        return super().transcribe(*args, **kwargs)


class CountingVision(FakeVisionProvider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def describe(self, *args, **kwargs):
        self.calls += 1
        return super().describe(*args, **kwargs)


class CountingOcr(FakeOcrProvider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def read(self, *args, **kwargs):
        self.calls += 1
        return super().read(*args, **kwargs)


@pytest.fixture
def providers():
    return CountingSpeech(), CountingVision(), CountingOcr()


def _runner(database, paths, config, providers) -> PipelineRunner:
    speech, vision, ocr = providers
    workers = workers_through("game_events")
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech)
    workers[JobStage.VISION] = VisionWorker(vision)
    workers[JobStage.OCR] = OcrWorker(ocr)
    return PipelineRunner(database, paths, config, workers=workers)


def _analysed_project(runner, media_service, project_manager, clip: Path, name: str):
    project = project_manager.create(
        ProjectCreate(name=name, target_duration_seconds=600, game="auto")
    )
    media = media_service.import_media(project.id, MediaImport(path=str(clip)))
    while runner.run_next(project.id) is not None:
        pass
    return project, media


def _rows(database, table: str, media_id: str) -> int:
    row = database.fetch_one(
        f"SELECT COUNT(*) AS n FROM {table} WHERE media_id = ?", (media_id,)
    )
    return int(row["n"])


def _result(database, project_id: str, media_id: str, stage: str) -> dict:
    import json

    row = database.fetch_one(
        "SELECT result FROM analysis_jobs "
        "WHERE project_id = ? AND media_id = ? AND stage = ? AND status = 'completed'",
        (project_id, media_id, stage),
    )
    return json.loads(row["result"]) if row and row["result"] else {}


class TestSecondProjectReuses:
    def test_the_models_are_never_asked_twice(
        self, database, paths, config, media_service, project_manager, test_clip, providers
    ) -> None:
        speech, vision, ocr = providers
        runner = _runner(database, paths, config, providers)

        _, media_a = _analysed_project(
            runner, media_service, project_manager, test_clip, "first"
        )
        after_first = (speech.calls, vision.calls, ocr.calls)
        assert after_first[0] > 0, "the first project must actually transcribe"

        project_b, media_b = _analysed_project(
            runner, media_service, project_manager, test_clip, "second"
        )

        assert (speech.calls, vision.calls, ocr.calls) == after_first, (
            "an identical recording was re-analysed instead of reused"
        )
        # The rows really did travel.
        for table in ("transcript_segments", "audio_events", "scenes", "vision_observations"):
            assert _rows(database, table, media_b.id) == _rows(database, table, media_a.id)
        # And the result says where it came from (§49 provenance).
        reused = _result(database, project_b.id, media_b.id, "transcript")
        assert reused.get("reused_from_project")

    def test_game_events_still_derive_per_project(
        self, database, paths, config, media_service, project_manager, test_clip, providers
    ) -> None:
        # GAME_EVENTS recomputes from the copied rows: it is seconds of CPU
        # and profile-dependent, so reusing it would trade correctness for
        # nothing (§127 calls the re-run cheap, and it is).
        runner = _runner(database, paths, config, providers)
        _analysed_project(runner, media_service, project_manager, test_clip, "first")
        project_b, media_b = _analysed_project(
            runner, media_service, project_manager, test_clip, "second"
        )

        result = _result(database, project_b.id, media_b.id, "game_events")
        assert "reused_from_project" not in result


class TestVersionsInvalidate:
    def test_a_changed_speech_model_recomputes(
        self, database, paths, config, media_service, project_manager, test_clip, providers
    ) -> None:
        speech, _, _ = providers
        runner = _runner(database, paths, config, providers)
        _analysed_project(runner, media_service, project_manager, test_clip, "first")
        baseline = speech.calls

        upgraded = config.model_copy(
            update={
                "models": config.models.model_copy(
                    update={
                        "speech": config.models.speech.model_copy(
                            update={"version": "faster-whisper-somehow-newer"}
                        )
                    }
                )
            }
        )
        runner_b = _runner(database, paths, upgraded, providers)
        _analysed_project(runner_b, media_service, project_manager, test_clip, "second")

        assert speech.calls > baseline, (
            "a new model version must invalidate the cache (§48, §49)"
        )


class TestReuseNeverBreaksAStage:
    def test_a_donor_with_vanished_rows_is_recomputed(
        self, database, paths, config, media_service, project_manager, test_clip, providers
    ) -> None:
        speech, _, _ = providers
        runner = _runner(database, paths, config, providers)
        _, media_a = _analysed_project(
            runner, media_service, project_manager, test_clip, "first"
        )
        baseline = speech.calls

        # The pointer survives; the rows do not. The donor's result still
        # claims segments, so the donor must be refused.
        database.execute(
            "DELETE FROM transcript_segments WHERE media_id = ?", (media_a.id,)
        )

        _, media_b = _analysed_project(
            runner, media_service, project_manager, test_clip, "second"
        )

        assert speech.calls > baseline
        assert _rows(database, "transcript_segments", media_b.id) > 0

    def test_a_requeued_stage_recomputes_rather_than_copying_itself(
        self, database, paths, config, media_service, project_manager, test_clip, providers
    ) -> None:
        # §90: re-analysis exists to run a stage again. A cache hit pointing
        # at the very media being re-analysed must not turn that into a no-op.
        from backend.services.job_manager import JobManager

        speech, _, _ = providers
        runner = _runner(database, paths, config, providers)
        project, media = _analysed_project(
            runner, media_service, project_manager, test_clip, "only"
        )
        baseline = speech.calls

        jobs = JobManager(database, config)
        row = database.fetch_one(
            "SELECT id FROM analysis_jobs WHERE project_id = ? AND stage = 'transcript'",
            (project.id,),
        )
        jobs.requeue(row["id"])
        while runner.run_next(project.id) is not None:
            pass

        assert speech.calls > baseline
        assert _rows(database, "transcript_segments", media.id) > 0
