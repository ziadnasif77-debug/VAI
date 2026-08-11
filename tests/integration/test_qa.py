"""Phase 11 acceptance: a deliberately broken render is detected (SPEC §76–§79).

    **Acceptance: a deliberately broken render is detected. Technical failures
    block export; content warnings go to human review.**

The only honest way to test a detector is to give it something that is really
broken, so these fixtures are genuinely bad videos: black throughout, silent,
frozen, missing an audio stream, half the length it should be. Each is built
with FFmpeg and each must be caught by name — "QA failed" is not enough, since
a check that fails everything would pass that assertion too.

The mirror of it matters just as much and is easy to forget: a *good* render
must come back clean. A QA engine that cries wolf is one people switch off, and
then it protects nothing.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from backend.config.loader import load_config
from backend.core.models.enums import QAStatus
from backend.media.ffmpeg import FFmpegRunner
from backend.qa import technical
from backend.qa.report import build_report

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]

WIDTH, HEIGHT, FPS, SECONDS = 320, 240, 15, 6

#: One frame of `testsrc`, held for the whole clip.
_HELD = (
    f"trim=start_frame=0:end_frame=1,"
    f"loop=loop={FPS * SECONDS - 1}:size=1:start=0,setpts=N/{FPS}/TB"
)
#: A picture that is *almost* still: a held frame carrying faint temporal
#: noise, which is what a captured menu screen is. A perfectly still source
#: encodes to bit-identical frames whatever the encoder, so it cannot tell an
#: encoder-dependent detector from an encoder-independent one. The seed is
#: pinned because a fixture whose verdict is random is worse than no fixture.
NEAR_STATIC = f"{_HELD},noise=alls=2:allf=t:all_seed=20260812"

#: The two rate controls `config/rendering.yaml` chooses between: a quality
#: target and a bitrate target. They quantise differently, and that difference
#: is what used to decide the frozen-frames verdict.
QUALITY_TARGET = ("-c:v", "libx264", "-preset", "medium", "-crf", "19")
BITRATE_TARGET = (
    "-c:v", "libx264", "-preset", "medium",
    "-b:v", "2M", "-minrate", "2M", "-maxrate", "2M", "-bufsize", "4M",
)


@pytest.fixture(scope="module")
def qa_config():
    return load_config()


@pytest.fixture(scope="module")
def runner(qa_config):
    return FFmpegRunner(qa_config.ffmpeg)


@pytest.fixture(scope="module")
def expected() -> technical.Expected:
    return technical.Expected(
        duration_seconds=float(SECONDS), width=WIDTH, height=HEIGHT, fps=float(FPS)
    )


def _make(
    target: Path,
    video: str,
    audio: str | None,
    *,
    seconds: int = SECONDS,
    filters: str = "",
    encode: Sequence[str] = ("-c:v", "libx264", "-preset", "ultrafast"),
) -> Path:
    """Build one deliberately shaped clip.

    ``video`` is a lavfi source without its size and duration. The separator
    differs by source -- ``testsrc`` takes ``=`` for its first option while
    ``color=c=black`` already has one and takes ``:`` -- so it is chosen here
    rather than left for each caller to get wrong.

    ``encode`` is a parameter because one of these fixtures exists to be built
    twice: the same picture through two rate controls, to check the verdict is
    about the picture.
    """
    separator = ":" if "=" in video else "="
    source = f"{video}{separator}s={WIDTH}x{HEIGHT}:r={FPS}:d={seconds}"
    argv = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", source,
    ]
    if audio is not None:
        argv += ["-f", "lavfi", "-i", f"{audio}:d={seconds}"]
    if filters:
        argv += ["-vf", filters]
    argv += [*encode, "-pix_fmt", "yuv420p"]
    argv += ["-c:a", "aac", "-shortest"] if audio is not None else ["-an"]
    argv += [str(target)]
    subprocess.run(argv, check=True, capture_output=True, timeout=300)
    return target


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One good render and five broken ones."""
    directory = tmp_path_factory.mktemp("qa")
    tone = "sine=f=440:r=48000"
    return {
        "good": _make(directory / "good.mp4", "testsrc", tone),
        "black": _make(directory / "black.mp4", "color=c=black", tone),
        "frozen": _make(directory / "frozen.mp4", "color=c=blue", tone),
        "silent": _make(directory / "silent.mp4", "testsrc", "anullsrc=r=48000:cl=stereo"),
        "no_audio": _make(directory / "no_audio.mp4", "testsrc", None),
        "short": _make(directory / "short.mp4", "testsrc", tone, seconds=2),
    }


@pytest.fixture(scope="module")
def near_static(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One almost-still picture, encoded by each rate control."""
    directory = tmp_path_factory.mktemp("rate")
    return {
        name: _make(
            directory / f"{name}.mp4", "testsrc", None,
            filters=NEAR_STATIC, encode=encode,
        )
        for name, encode in (
            ("quality", QUALITY_TARGET),
            ("bitrate", BITRATE_TARGET),
        )
    }


def _inspect(clips, name: str, expected, runner, qa_config):
    findings = technical.inspect(
        clips[name], expected, runner=runner, config=qa_config.qa
    )
    return findings, build_report(findings, qa_config.qa)


def _status_of(findings, check: str) -> QAStatus | None:
    for finding in findings:
        if finding.check == check:
            return finding.status
    return None


class TestAcceptance:
    """**A deliberately broken render is detected.**"""

    def test_a_good_render_passes_every_check(
        self, clips, expected, runner, qa_config
    ) -> None:
        # The half that is easy to forget: a QA engine that cries wolf is one
        # people switch off.
        findings, report = _inspect(clips, "good", expected, runner, qa_config)

        assert report.status is QAStatus.PASSED, report.explain()
        assert not report.blocks_export
        assert len(findings) >= 8, "too few checks actually ran"

    def test_a_black_render_is_caught(self, clips, expected, runner, qa_config) -> None:
        findings, report = _inspect(clips, "black", expected, runner, qa_config)

        assert _status_of(findings, "black_frames") is QAStatus.FAILED
        assert report.blocks_export

    def test_a_frozen_render_is_caught(self, clips, expected, runner, qa_config) -> None:
        # A freeze that runs to the end of the file has a start and no
        # duration; pairing them naively drops exactly this case.
        findings, report = _inspect(clips, "frozen", expected, runner, qa_config)

        assert _status_of(findings, "frozen_frames") is QAStatus.FAILED
        assert report.blocks_export

    def test_the_verdict_is_about_the_picture_not_the_encoder(
        self, near_static, expected, runner, qa_config
    ) -> None:
        # The bug this replaces: one project, one timeline, two encoders, two
        # answers -- libx264 blocked the export and NVENC found nothing. At a
        # noise floor near bit-identical, what the detector measures is the
        # quantiser, and rate control decides whether consecutive frames come
        # out the same. So the same picture is encoded to a quality target and
        # to a bitrate target, and the two must agree.
        verdicts = {}
        for name, path in near_static.items():
            findings = technical.inspect(
                path, expected, runner=runner, config=qa_config.qa
            )
            verdicts[name] = _status_of(findings, "frozen_frames")

        assert verdicts["quality"] == verdicts["bitrate"], (
            f"the same picture got {verdicts['quality']} from a quality target "
            f"and {verdicts['bitrate']} from a bitrate target"
        )

    def test_a_freeze_the_recording_also_has_does_not_block(
        self, clips, expected, runner, qa_config
    ) -> None:
        # A menu, a loading screen, a paused game: the picture is still and the
        # render is right. §77 makes that a judgement for a person, not a fact
        # about the file, so it must not stop the export.
        source = (
            technical.SourceSpan(
                path=clips["frozen"],
                timeline_start=0.0,
                timeline_end=float(SECONDS),
                source_in=0.0,
            ),
        )
        findings = technical.inspect(
            clips["frozen"], expected, runner=runner, config=qa_config.qa,
            sources=source,
        )
        report = build_report(findings, qa_config.qa)

        assert _status_of(findings, "frozen_frames") is QAStatus.WARNING
        assert not report.blocks_export

    def test_a_freeze_the_recording_does_not_have_still_blocks(
        self, clips, expected, runner, qa_config
    ) -> None:
        # The same frozen render, but the recording behind it is moving -- so
        # the render stopped on its own. That is the defect the check exists
        # for, and it is the one case that should block.
        source = (
            technical.SourceSpan(
                path=clips["good"],
                timeline_start=0.0,
                timeline_end=float(SECONDS),
                source_in=0.0,
            ),
        )
        findings = technical.inspect(
            clips["frozen"], expected, runner=runner, config=qa_config.qa,
            sources=source,
        )
        report = build_report(findings, qa_config.qa)

        assert _status_of(findings, "frozen_frames") is QAStatus.FAILED
        assert report.blocks_export

    def test_a_render_with_no_audio_stream_is_caught(
        self, clips, expected, runner, qa_config
    ) -> None:
        findings, report = _inspect(clips, "no_audio", expected, runner, qa_config)

        assert _status_of(findings, "audio_stream") is QAStatus.FAILED
        assert report.blocks_export

    def test_a_render_of_the_wrong_length_is_caught(
        self, clips, expected, runner, qa_config
    ) -> None:
        findings, report = _inspect(clips, "short", expected, runner, qa_config)

        assert _status_of(findings, "duration") is QAStatus.FAILED
        assert report.blocks_export

    def test_a_silent_render_warns_rather_than_blocking(
        self, clips, expected, runner, qa_config
    ) -> None:
        # A quiet mix is a judgement, not a broken file: the video plays.
        findings, report = _inspect(clips, "silent", expected, runner, qa_config)

        assert _status_of(findings, "loudness") is QAStatus.WARNING
        assert not report.blocks_export

    def test_a_missing_file_is_caught_before_anything_else(
        self, expected, runner, qa_config, tmp_path: Path
    ) -> None:
        findings = technical.inspect(
            tmp_path / "absent.mp4", expected, runner=runner, config=qa_config.qa
        )

        assert findings[0].check == "file_exists"
        assert findings[0].status is QAStatus.FAILED

    def test_every_failure_says_what_to_do_about_it(
        self, clips, expected, runner, qa_config
    ) -> None:
        # §79: a finding without a remedy has moved the problem, not solved it.
        for name in ("black", "frozen", "no_audio", "short"):
            findings, _ = _inspect(clips, name, expected, runner, qa_config)
            for finding in findings:
                if finding.failed or finding.warned:
                    assert finding.remedy, f"{name}/{finding.check} has no remedy"


class TestWhatWasChecked:
    """A report that lists only problems cannot say what was verified."""

    def test_passes_are_reported_too(self, clips, expected, runner, qa_config) -> None:
        findings, _ = _inspect(clips, "good", expected, runner, qa_config)
        names = {finding.check for finding in findings}

        assert {"duration", "resolution", "fps", "audio_stream", "decodes"} <= names

    def test_a_disabled_check_does_not_run(
        self, clips, expected, runner, qa_config
    ) -> None:
        # And its absence is visible, rather than looking like a pass.
        narrowed = qa_config.qa.model_copy(
            update={
                "technical": qa_config.qa.technical.model_copy(
                    update={"checks": {"black_frames": False}}
                )
            }
        )
        findings = technical.inspect(
            clips["black"], expected, runner=runner, config=narrowed
        )

        assert _status_of(findings, "black_frames") is None

    def test_the_noise_floor_is_configuration_not_a_constant(
        self, near_static, expected, runner, qa_config
    ) -> None:
        # It was baked into the filter string, which is how it went untuned for
        # a phase. Driven far enough down, the almost-still picture stops
        # counting as still at all -- so the knob reaches the detector.
        strict = qa_config.qa.model_copy(
            update={
                "technical": qa_config.qa.technical.model_copy(
                    update={
                        "thresholds": qa_config.qa.technical.thresholds.model_copy(
                            update={"freeze_noise_db": -90.0}
                        )
                    }
                )
            }
        )
        findings = technical.inspect(
            near_static["quality"], expected, runner=runner, config=strict
        )

        assert _status_of(findings, "frozen_frames") is QAStatus.PASSED

    def test_the_explanation_carries_the_remedies(
        self, clips, expected, runner, qa_config
    ) -> None:
        _, report = _inspect(clips, "black", expected, runner, qa_config)
        lines = report.explain()

        assert any(line.startswith("    →") for line in lines)


class TestPolicy:
    """§76's policy: technical blocks, content warns (§78)."""

    def test_a_technical_failure_blocks_export(
        self, clips, expected, runner, qa_config
    ) -> None:
        _, report = _inspect(clips, "black", expected, runner, qa_config)

        assert report.blocks_export
        assert report.needs_review

    def test_the_policy_can_be_told_not_to_block(
        self, clips, expected, runner, qa_config
    ) -> None:
        permissive = qa_config.qa.model_copy(
            update={
                "policy": qa_config.qa.policy.model_copy(
                    update={"fail_on_technical_error": False}
                )
            }
        )
        findings = technical.inspect(
            clips["black"], expected, runner=runner, config=qa_config.qa
        )
        report = build_report(findings, permissive)

        # The finding is still reported; only the consequence changed.
        assert report.status is QAStatus.FAILED
        assert not report.blocks_export


class TestNothingToCheck:
    """A recording with nothing worth editing is the pipeline working."""

    def test_qa_skips_when_the_render_skipped(
        self, media_service, project_manager, database, paths, config, test_clip
    ) -> None:
        # Six seconds of test pattern yields no moments, so STORY, EDL and
        # RENDER all skip and there is no video. QA raising here would turn
        # "there was nothing to edit" into "the pipeline failed" (§95).
        from ai.speech.fake_provider import FakeSpeechProvider
        from ai.vision.fake_provider import FakeVisionProvider
        from backend.core.models.enums import JobStage, VideoMode
        from backend.core.models.media import MediaImport
        from backend.core.models.project import ProjectCreate
        from backend.pipeline.runner import PipelineRunner
        from backend.pipeline.workers.speech_workers import TranscriptWorker
        from backend.pipeline.workers.vision_workers import VisionWorker
        from tests.conftest import workers_through

        project = project_manager.create(
            ProjectCreate(name="Empty", target_duration_seconds=600, mode=VideoMode.STORY)
        )
        media_service.import_media(project.id, MediaImport(path=str(test_clip)))
        # Fakes, or this downloads three gigabytes of Whisper to prove
        # something about an empty timeline.
        workers = workers_through(JobStage.QA)
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(FakeSpeechProvider())
        workers[JobStage.VISION] = VisionWorker(FakeVisionProvider())
        runner = PipelineRunner(database, paths, config, workers=workers)
        outcomes = {outcome.job.stage: outcome for outcome in runner.run_project(project.id)}

        assert outcomes[JobStage.RENDER].succeeded
        assert outcomes[JobStage.RENDER].job.result["skipped"] is True
        qa = outcomes[JobStage.QA]
        assert qa.succeeded, qa.job.error_message
        assert qa.job.result["skipped"] is True
        assert qa.job.result["blocks_export"] is False
        assert qa.job.result["reason"], "a skip must say which stage stopped and why"
