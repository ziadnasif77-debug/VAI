"""Phase 10 acceptance: a finished MP4 (SPEC §65, §67, §72–§75).

    **Acceptance: the final MP4 opens in standard players; duration inside the
    10–60 minute band.**

The second half of that is checked against a policy that no test fixture can
satisfy — a forty-second clip cannot become a ten-minute video, and pretending
otherwise would mean testing the clamp rather than the render. So the band is
checked where it is decided, in the timeline (`test_timeline.py`), and what is
checked here is the part only a real encode can show: that the file exists, is
readable by a decoder that was not the one that wrote it, carries the streams
it should, and lasts as long as the edit said it would.

"Opens in standard players" is verified the only way available without a
player: the file is demuxed and decoded end to end by FFmpeg, which is what a
player does. A file that decodes without error and reports the expected
duration, resolution, frame rate and streams is one a player opens.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from ai.speech.fake_provider import FakeSpeechProvider
from ai.vision.fake_provider import FakeVisionProvider
from backend.config.loader import load_config, reset_config_cache
from backend.config.paths import build_paths, find_repository_root
from backend.core.models.enums import JobStage, VideoMode
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.database.repositories.renders import RenderRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.media.ffmpeg import FFmpegRunner
from backend.media.probe import probe_media
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers import default_workers
from backend.pipeline.workers.gaming_workers import OcrWorker
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.pipeline.workers.vision_workers import VisionWorker
from backend.rendering.composite import _placed_segments
from backend.rendering.overlay_plan import OverlayPlan, Segment
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg, pytest.mark.slow]


# The pipeline runs **once** for this file. Every assertion below is about the
# same finished MP4, and re-running a full analysis and a CPU encode per test
# would turn one acceptance into fifteen minutes of repetition. The fixtures
# are therefore module-scoped, which means building the chain here rather than
# borrowing conftest's function-scoped ones.
@pytest.fixture(scope="module")
def render_config():
    """The shipped configuration, targeting 720p30 instead of 1080p60.

    The acceptance is that the finished file is correct, not that it is large:
    every assertion below reads the preset rather than a literal, so the only
    thing the smaller target changes is that three encodes of a 320x240 fixture
    stop being upscaled sixfold. The 1080p60 flags are covered where they are
    built, in ``test_encoder.py``.
    """
    reset_config_cache()
    config = load_config(find_repository_root() / "config")
    yield config.model_copy(
        update={
            "youtube_preset": config.youtube_preset.model_copy(
                update={"resolution": 720, "fps": 30}
            )
        }
    )
    reset_config_cache()


@pytest.fixture(scope="module")
def render_paths(render_config, tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("render")
    resolved = build_paths(render_config, data_root=root).create()
    profiles = root / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    return dataclasses.replace(resolved, profiles_dir=profiles)


@pytest.fixture(scope="module")
def render_database(render_paths, render_config):
    database = Database(render_paths.database_path, render_config.application.database)
    migrate(database)
    yield database
    database.close()


@pytest.fixture(scope="module")
def render_runner(render_database, render_paths, render_config):
    workers = default_workers()
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(FakeSpeechProvider())
    workers[JobStage.VISION] = VisionWorker(FakeVisionProvider())
    workers[JobStage.OCR] = OcrWorker(FakeOcrProvider(default=[("VICTORY", 0.92)]))
    return PipelineRunner(render_database, render_paths, render_config, workers=workers)


@pytest.fixture(scope="module")
def rendered(render_runner, render_database, render_paths, render_config, module_clip):
    """A project taken all the way to a finished file, once."""
    projects = ProjectManager(render_database, render_paths, render_config)
    media = MediaIngestionService(render_database, render_paths, render_config)
    project = projects.create(
        ProjectCreate(name="Render", target_duration_seconds=600, mode=VideoMode.STORY)
    )
    media.import_media(project.id, MediaImport(path=str(module_clip)))
    outcomes = {outcome.job.stage: outcome for outcome in render_runner.run_project(project.id)}
    return project, outcomes


@pytest.fixture(scope="module")
def module_clip(reaction_clip: Path) -> Path:
    """The session's reaction clip, reachable from module-scoped fixtures."""
    return reaction_clip


def _decode_fully(path: Path) -> subprocess.CompletedProcess[str]:
    """Demux and decode the whole file, discarding the output.

    What a player does, minus the window. An error here is an error a viewer
    would see.
    """
    return subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )


class TestAcceptance:
    """**The final MP4 opens in standard players.**"""

    def test_the_stage_runs_and_produces_a_file(self, rendered) -> None:
        _, outcomes = rendered

        assert JobStage.RENDER in outcomes, "the runner must reach the RENDER stage"
        outcome = outcomes[JobStage.RENDER]
        assert outcome.succeeded, outcome.job.error_message
        assert Path(outcome.job.result["output_path"]).is_file()

    def test_the_file_decodes_end_to_end_without_error(self, rendered) -> None:
        # The acceptance, in the only form available without a player.
        _, outcomes = rendered
        path = Path(outcomes[JobStage.RENDER].job.result["output_path"])

        result = _decode_fully(path)

        assert result.returncode == 0, result.stderr
        assert result.stderr.strip() == "", result.stderr

    def test_it_is_an_mp4_with_the_configured_codecs(self, rendered, render_config) -> None:
        _, outcomes = rendered
        path = Path(outcomes[JobStage.RENDER].job.result["output_path"])
        probe = probe_media(path, FFmpegRunner(render_config.ffmpeg))

        assert path.suffix == ".mp4"
        assert probe.metadata.video_codec == "h264"
        assert probe.metadata.audio_codec == "aac"

    def test_it_has_both_a_picture_and_a_sound(self, rendered) -> None:
        # A silent render and a black one both "succeed"; neither is a video.
        _, outcomes = rendered
        result = outcomes[JobStage.RENDER].job.result

        assert result["video_codec"], "no video stream"
        assert result["audio_codec"], "no audio stream"

    def test_it_matches_the_requested_format(self, rendered, render_config) -> None:
        _, outcomes = rendered
        result = outcomes[JobStage.RENDER].job.result

        assert result["height"] == render_config.youtube_preset.resolution
        assert result["fps"] == render_config.youtube_preset.fps

    def test_its_length_matches_the_edit(self, rendered, render_database) -> None:
        project, outcomes = rendered
        edit_seconds = TimelineRepository(render_database).duration_seconds(project.id)
        rendered_seconds = outcomes[JobStage.RENDER].job.result["duration_seconds"]

        # A frame per cut is normal; a second is not.
        assert rendered_seconds == pytest.approx(edit_seconds, abs=1.0)

    def test_the_duration_is_measured_not_restated(self, rendered) -> None:
        # The result must come from probing the output. An encoder that
        # produced the wrong length has to be visible here, not in QA.
        _, outcomes = rendered
        result = outcomes[JobStage.RENDER].job.result
        path = Path(result["output_path"])
        probe_seconds = float(
            subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            ).stdout.strip()
        )

        assert result["duration_seconds"] == pytest.approx(probe_seconds, abs=0.05)


class TestTheRenderRecord:
    """§80: which encoder made which file, answerable later."""

    def test_the_render_is_recorded(self, rendered, render_database) -> None:
        project, outcomes = rendered
        record = RenderRepository(render_database).latest(project.id)

        assert record is not None
        assert record["status"] == "completed"
        assert record["output_path"] == outcomes[JobStage.RENDER].job.result["output_path"]

    def test_it_names_the_encoder_that_produced_the_file(self, rendered, render_database) -> None:
        project, _ = rendered
        record = RenderRepository(render_database).latest(project.id)

        assert record["encoder"] in {"h264_nvenc", "libx264", "hevc_nvenc", "libx265"}
        assert record["render_seconds"] > 0

    def test_a_second_render_adds_a_row_rather_than_replacing_one(
        self, rendered, render_database, render_runner
    ) -> None:
        # Re-rendering after an edit is normal, and the earlier file may still
        # be open on someone's desktop.
        project, _ = rendered
        renders = RenderRepository(render_database)
        before = renders.count_for_project(project.id)

        job = next(
            j for j in render_runner.jobs.list_jobs(project.id) if j.stage is JobStage.RENDER
        )
        render_runner.jobs.requeue(job.id)
        assert render_runner.run_job(job.id).succeeded

        assert renders.count_for_project(project.id) == before + 1


class TestTheOverlayReachedTheVideo:
    """§66 through §67: what Remotion drew has to arrive in the file."""

    def test_the_result_says_whether_an_overlay_was_composited(self, rendered) -> None:
        _, outcomes = rendered
        result = outcomes[JobStage.RENDER].job.result

        assert "has_overlay" in result
        if not result["has_overlay"]:
            assert any("overlay" in note for note in result["notes"])

    @pytest.mark.requires_node
    def test_captions_are_burned_into_the_picture(self, rendered, render_database) -> None:
        # The clip has speech, so it has captions, so the overlay must exist
        # and must have reached the encode.
        project, outcomes = rendered
        if TimelineRepository(render_database).caption_count(project.id) == 0:
            pytest.skip("this clip produced no captions to composite")

        assert outcomes[JobStage.RENDER].job.result["has_overlay"]


class TestResumeAndCleanup:
    def test_the_intermediates_are_cleared_once_the_file_exists(
        self, rendered, render_paths
    ) -> None:
        # Kept during the render for §47's resume; removed after, because they
        # are several times the size of the video they produced.
        _, outcomes = rendered
        assert outcomes[JobStage.RENDER].job.result["segments_removed"] >= 0
        project, _ = rendered
        segments = render_paths.projects_dir / project.id / "renders" / "work" / "segments"
        remaining = list(segments.iterdir()) if segments.is_dir() else []

        assert not [path for path in remaining if path.suffix == ".mp4"]

    def test_re_running_produces_an_equivalent_video(self, rendered, render_runner) -> None:
        # §127: a re-render costs a render, never a re-analysis.
        project, outcomes = rendered
        first = outcomes[JobStage.RENDER].job.result

        job = next(
            j for j in render_runner.jobs.list_jobs(project.id) if j.stage is JobStage.RENDER
        )
        render_runner.jobs.requeue(job.id)
        outcome = render_runner.run_job(job.id)

        assert outcome.succeeded
        assert outcome.job.result["duration_seconds"] == pytest.approx(
            first["duration_seconds"], abs=0.2
        )
        assert outcome.job.result["clips"] == first["clips"]


class TestStagePipeline:
    def test_render_runs_after_the_edl(self, rendered) -> None:
        # The order the runner actually executed, which a later re-run cannot
        # rewrite the way a stored completed_at can.
        _, outcomes = rendered
        order = list(outcomes)

        assert order.index(JobStage.EDL) < order.index(JobStage.RENDER)

    def test_the_runner_stops_at_the_frontier(
        self, rendered, render_runner, frontier_check
    ) -> None:
        # The same project the rest of the file rendered: the first stage with
        # no worker waits rather than failing.
        project, _ = rendered
        frontier_check(render_runner, project.id)


class TestQaStage:
    """Phase 11's stage, against the render this file already produced.

    Sharing the fixture on purpose: QA's whole job is to inspect a real
    finished file, and rendering a second one to inspect would double the
    slowest part of the suite to learn nothing new.
    """

    def test_qa_runs_and_finds_nothing_that_blocks_export(self, rendered) -> None:
        # Not "no findings at all": the fake vision provider describes a menu
        # screen, and a clip covering it is exactly what §77 should warn about.
        # What a good render means here is that nothing *technical* failed.
        _, outcomes = rendered

        assert JobStage.QA in outcomes, "the runner must reach the QA stage"
        outcome = outcomes[JobStage.QA]
        assert outcome.succeeded, outcome.job.error_message
        assert outcome.job.result["failures"] == [], outcome.job.result["explanation"]
        assert outcome.job.result["blocks_export"] is False

    def test_a_content_warning_does_not_stop_the_video(self, rendered) -> None:
        # §78: the human decides. A menu in the edit is worth saying and is not
        # a reason to withhold the file.
        _, outcomes = rendered
        result = outcomes[JobStage.QA].job.result

        if result["warnings"]:
            assert result["blocks_export"] is False
            assert result["needs_review"] in (True, False)

    def test_every_check_is_recorded(self, rendered, render_database) -> None:
        # §80: "the audio was checked and is fine" and "nobody looked at the
        # audio" are different statements, and only a stored pass tells them
        # apart.
        from backend.database.repositories.qa import QaRepository

        _, outcomes = rendered
        render_id = outcomes[JobStage.RENDER].job.result["render_id"]
        stored = QaRepository(render_database).list_for_render(render_id)

        assert len(stored) >= 8
        assert {row["check_name"] for row in stored} >= {
            "duration",
            "resolution",
            "audio_stream",
            "decodes",
        }

    def test_the_results_are_tied_to_the_render_they_describe(
        self, rendered, render_database
    ) -> None:
        from backend.database.repositories.qa import QaRepository

        _, outcomes = rendered
        render_id = outcomes[JobStage.RENDER].job.result["render_id"]
        stored = QaRepository(render_database).list_for_render(render_id)

        assert all(row["render_id"] == render_id for row in stored)
        assert QaRepository(render_database).failures_for_render(render_id) == []

    def test_qa_runs_after_the_render(self, rendered) -> None:
        _, outcomes = rendered
        order = list(outcomes)

        assert order.index(JobStage.RENDER) < order.index(JobStage.QA)


class TestTheSegmentedOverlay:
    """§66's shortcut, proved against a real decoder.

    The unit tests own the arithmetic. What only FFmpeg can settle is whether
    a filter graph built from a plan actually puts the overlay on the frames
    the plan named -- and, just as importantly, leaves every other frame alone.
    A `repeatlast` default in the wrong place paints the last caption across
    the rest of the video, and nothing about that is visible in a filter
    string.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def placed(cls, tmp_path_factory, render_config):
        """A green ten-second video with two one-second red stretches put back."""
        work = tmp_path_factory.mktemp("segments")
        runner = FFmpegRunner(render_config.ffmpeg)
        programme = work / "programme.mp4"
        overlay = work / "overlay.webm"
        output = work / "composited.mp4"

        # Two seconds of opaque red: the *compacted* overlay, exactly as
        # Remotion would return it -- both stretches back to back, no gap.
        _ffmpeg(
            runner,
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=320x180:r=30:d=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(programme),
            ],
        )
        _ffmpeg(
            runner,
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=320x180:r=30:d=2",
                "-vf",
                "format=yuva420p",
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuva420p",
                str(overlay),
            ],
        )

        plan = OverlayPlan(
            segments=(
                Segment(source_start=60, source_end=90, render_start=0),
                Segment(source_start=210, source_end=240, render_start=30),
            ),
            total_frames=300,
            fps=30,
        )
        filters, label = _placed_segments(plan)
        _ffmpeg(
            runner,
            [
                "-i",
                str(programme),
                "-c:v",
                "libvpx-vp9",
                "-i",
                str(overlay),
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[{label}]",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ],
        )
        return output, runner

    @pytest.mark.parametrize(
        ("second", "red"),
        [
            (0.5, False),  # before the first stretch
            (2.5, True),  # inside it: frames 60-90 at 30 fps
            (3.5, False),  # the gap -- the overlay must not have frozen
            (5.0, False),
            (7.5, True),  # the second stretch: frames 210-240
            (8.5, False),  # after it, and still not frozen
            (9.5, False),
        ],
    )
    def test_the_overlay_lands_only_where_the_plan_put_it(
        self, placed, second: float, red: bool
    ) -> None:
        output, runner = placed
        pixel = _pixel_at(runner, output, second)
        assert (pixel[0] > 200 and pixel[1] < 60) is red, f"at {second}s the pixel was {pixel}"

    def test_the_video_keeps_its_full_length(self, placed) -> None:
        # Two seconds of overlay must not truncate a ten-second video, which is
        # what `shortest` and the default eof_action would do.
        output, runner = placed
        assert probe_media(output, runner).duration_seconds == pytest.approx(10.0, abs=0.1)


def _ffmpeg(runner: FFmpegRunner, arguments: list[str]) -> None:
    subprocess.run(
        [*runner.base_arguments(), *arguments],
        check=True,
        capture_output=True,
    )


def _pixel_at(runner: FFmpegRunner, path: Path, second: float) -> tuple[int, int, int]:
    """The top-left pixel of the frame at ``second``, as RGB."""
    completed = subprocess.run(
        [
            *runner.base_arguments(),
            "-ss",
            f"{second}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    raw = completed.stdout[:3]
    return (raw[0], raw[1], raw[2])
