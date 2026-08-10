"""The media engine against real FFmpeg (Phase 2).

These tests decode actual files. Clips are generated from FFmpeg's synthetic
sources by the fixtures, so the repository stays free of binaries and each test
gets media with exactly the property it is about -- two audio tracks, none at
all, a 30000/1001 frame rate.

Marked ``requires_ffmpeg`` and skipped, never silently passed, on a machine
without FFmpeg installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.errors import ErrorCode, MediaError
from backend.core.models.enums import JobStage, JobStatus, MediaState
from backend.core.models.media import MediaImport
from backend.database.repositories.frames import FrameRepository
from backend.database.repositories.media import MediaTrackRepository
from backend.media.audio import PRIMARY_FILENAME_STEM, extract_all_tracks, extract_analysis_audio
from backend.media.frames import (
    clear_frames,
    extract_at_times,
    extract_uniform,
    plan_base_sampling,
)
from backend.media.probe import probe_media
from backend.media.proxy import SEGMENT_DIRNAME, generate_proxy

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


class TestProbe:
    def test_reads_the_properties_every_later_stage_needs(
        self, test_clip: Path, ffmpeg_runner
    ) -> None:
        probe = probe_media(test_clip, ffmpeg_runner, require_video=True)
        assert probe.duration_seconds == pytest.approx(6.0, abs=0.5)
        assert probe.metadata.width == 320
        assert probe.metadata.height == 240
        assert probe.metadata.fps == pytest.approx(30.0, abs=0.01)
        assert probe.metadata.has_video is True
        assert probe.metadata.has_audio is True
        assert probe.metadata.video_codec == "h264"

    def test_a_rational_frame_rate_survives_the_probe(
        self, ntsc_clip: Path, ffmpeg_runner
    ) -> None:
        # The whole point of parsing rationals: 29.97, not 30.
        probe = probe_media(ntsc_clip, ffmpeg_runner)
        assert probe.metadata.fps == pytest.approx(29.97, abs=0.01)
        assert probe.metadata.fps != 30.0

    def test_a_second_audio_track_is_detected(self, two_track_clip: Path, ffmpeg_runner) -> None:
        # §19: game audio and microphone are different evidence.
        probe = probe_media(two_track_clip, ffmpeg_runner)
        assert len(probe.audio_tracks) == 2
        assert probe.has_separate_microphone_track is True

    def test_a_single_track_source_has_no_microphone(
        self, test_clip: Path, ffmpeg_runner
    ) -> None:
        assert probe_media(test_clip, ffmpeg_runner).has_separate_microphone_track is False

    def test_silent_video_is_not_an_error(self, silent_clip: Path, ffmpeg_runner) -> None:
        probe = probe_media(silent_clip, ffmpeg_runner, require_video=True)
        assert probe.metadata.has_audio is False
        assert probe.audio_tracks == ()

    def test_tracks_become_persistable_rows(self, two_track_clip: Path, ffmpeg_runner) -> None:
        tracks = probe_media(two_track_clip, ffmpeg_runner).to_media_tracks("media_1")
        assert [track.kind for track in tracks] == ["video", "audio", "audio"]
        assert all(track.media_id == "media_1" for track in tracks)
        assert len({track.id for track in tracks}) == 3

    def test_a_corrupt_file_fails_with_a_typed_error(
        self, tmp_path: Path, ffmpeg_runner
    ) -> None:
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\xff" * 4096)
        with pytest.raises(MediaError) as exc_info:
            probe_media(broken, ffmpeg_runner)
        assert exc_info.value.code in {ErrorCode.MEDIA_CORRUPTED, ErrorCode.MEDIA_PROBE_FAILED}
        assert exc_info.value.recoverable is False

    def test_a_missing_file_is_reported_before_ffprobe_runs(
        self, tmp_path: Path, ffmpeg_runner
    ) -> None:
        with pytest.raises(MediaError) as exc_info:
            probe_media(tmp_path / "absent.mp4", ffmpeg_runner)
        assert exc_info.value.code is ErrorCode.MEDIA_NOT_FOUND

    def test_require_video_rejects_an_audio_only_source(
        self, media_fixtures_dir: Path, ffmpeg_runner, clip_factory
    ) -> None:
        audio_only = clip_factory(
            media_fixtures_dir / "audio_only.m4a", seconds=2.0, video=False
        )
        with pytest.raises(MediaError) as exc_info:
            probe_media(audio_only, ffmpeg_runner, require_video=True)
        assert exc_info.value.code is ErrorCode.UNSUPPORTED_CONTAINER


class TestProxy:
    def test_downscales_to_the_configured_resolution(
        self, hd_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        probe = probe_media(hd_clip, ffmpeg_runner, require_video=True)
        result = generate_proxy(
            hd_clip, tmp_path / "proxy.mp4", ffmpeg_runner, config.proxy, probe=probe
        )
        assert result.height == config.proxy.resolution
        assert result.width == 1280  # aspect ratio preserved
        assert result.fps == pytest.approx(config.proxy.fps, abs=0.5)
        assert result.duration_seconds == pytest.approx(probe.duration_seconds, abs=0.5)

    def test_a_small_source_is_not_upscaled(
        self, test_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        # Encoding time spent inventing detail the recording does not contain.
        probe = probe_media(test_clip, ffmpeg_runner, require_video=True)
        result = generate_proxy(
            test_clip, tmp_path / "proxy.mp4", ffmpeg_runner, config.proxy, probe=probe
        )
        assert result.height == 240

    def test_running_twice_reuses_the_finished_proxy(
        self, test_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        # §47: re-running analysis must not re-encode a proxy that is correct.
        probe = probe_media(test_clip, ffmpeg_runner, require_video=True)
        destination = tmp_path / "proxy.mp4"
        first = generate_proxy(
            test_clip, destination, ffmpeg_runner, config.proxy, probe=probe
        )
        stamp = destination.stat().st_mtime_ns
        second = generate_proxy(
            test_clip, destination, ffmpeg_runner, config.proxy, probe=probe
        )
        assert second.segment_count == 0
        assert destination.stat().st_mtime_ns == stamp
        assert second.duration_seconds == pytest.approx(first.duration_seconds)

    def test_segments_are_assembled_and_then_cleaned_up(
        self, long_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        probe = probe_media(long_clip, ffmpeg_runner, require_video=True)
        destination = tmp_path / "proxy.mp4"
        result = generate_proxy(
            long_clip,
            destination,
            ffmpeg_runner,
            config.proxy,
            probe=probe,
            segment_seconds=300.0,
        )
        assert result.segment_count == 3
        assert result.duration_seconds == pytest.approx(probe.duration_seconds, abs=1.5)
        # §84: segments are as large as the proxy itself.
        assert not (tmp_path / SEGMENT_DIRNAME).exists()

    def test_an_interrupted_run_resumes_from_its_completed_segments(
        self, long_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        probe = probe_media(long_clip, ffmpeg_runner, require_video=True)
        work_dir = tmp_path / "segments"

        # Simulate a crash: stop after the first segment, keeping the work dir.
        calls = {"count": 0}

        def cancel_after_first() -> bool:
            calls["count"] += 1
            return (work_dir / "0000.mp4").exists()

        from backend.media.ffmpeg import CancelledError

        with pytest.raises(CancelledError):
            generate_proxy(
                long_clip,
                tmp_path / "proxy.mp4",
                ffmpeg_runner,
                config.proxy,
                probe=probe,
                work_dir=work_dir,
                segment_seconds=300.0,
                should_cancel=cancel_after_first,
            )
        assert (work_dir / "0000.mp4").is_file()

        result = generate_proxy(
            long_clip,
            tmp_path / "proxy.mp4",
            ffmpeg_runner,
            config.proxy,
            probe=probe,
            work_dir=work_dir,
            segment_seconds=300.0,
        )
        assert result.reused_segments >= 1
        assert result.was_resumed
        assert result.duration_seconds == pytest.approx(probe.duration_seconds, abs=1.5)

    def test_progress_is_reported_against_the_source_timeline(
        self, test_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        reports: list[tuple[float, str]] = []
        probe = probe_media(test_clip, ffmpeg_runner, require_video=True)
        generate_proxy(
            test_clip,
            tmp_path / "proxy.mp4",
            ffmpeg_runner,
            config.proxy,
            probe=probe,
            on_progress=lambda fraction, message: reports.append((fraction, message)),
        )
        assert reports
        assert all(0.0 <= fraction <= 1.0 for fraction, _ in reports)
        assert reports[-1][0] == 1.0


class TestAudioExtraction:
    def test_produces_the_configured_analysis_format(
        self, test_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        stream = extract_analysis_audio(
            test_clip, tmp_path / "analysis.wav", ffmpeg_runner, config.audio.analysis
        )
        assert stream.sample_rate == config.audio.analysis.sample_rate == 16000
        assert stream.channels == config.audio.analysis.channels == 1
        assert stream.duration_seconds == pytest.approx(6.0, abs=0.5)
        assert stream.size_bytes > 0

    def test_each_track_is_extracted_separately(
        self, two_track_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        # §19: mixing them down first destroys the distinction the moment
        # detector depends on.
        probe = probe_media(two_track_clip, ffmpeg_runner)
        streams = extract_all_tracks(
            two_track_clip, tmp_path, ffmpeg_runner, config.audio.analysis, probe=probe
        )
        assert len(streams) == 2
        assert streams[0].is_primary and not streams[1].is_primary
        assert streams[0].path.stem == PRIMARY_FILENAME_STEM
        assert streams[0].path != streams[1].path
        assert streams[0].source_track_index != streams[1].source_track_index
        assert all(stream.path.is_file() for stream in streams)

    def test_a_silent_source_yields_nothing_rather_than_failing(
        self, silent_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        probe = probe_media(silent_clip, ffmpeg_runner)
        assert extract_all_tracks(
            silent_clip, tmp_path, ffmpeg_runner, config.audio.analysis, probe=probe
        ) == []

    def test_no_partial_file_survives_a_failure(
        self, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"\x00" * 2048)
        target = tmp_path / "analysis.wav"
        with pytest.raises(MediaError):
            extract_analysis_audio(broken, target, ffmpeg_runner, config.audio.analysis)
        assert not target.exists()
        assert not list(tmp_path.glob("*.part.wav"))


class TestFrameExtraction:
    def test_one_pass_produces_the_planned_timestamps(
        self, test_clip: Path, tmp_path: Path, ffmpeg_runner, config
    ) -> None:
        sampling = config.analysis.frame_sampling
        probe = probe_media(test_clip, ffmpeg_runner, require_video=True)
        plan = plan_base_sampling(probe.duration_seconds, sampling)
        frames = extract_uniform(
            test_clip, tmp_path / "frames", plan, ffmpeg_runner,
            jpeg_quality=sampling.jpeg_quality,
            duration_seconds=probe.duration_seconds,
        )
        assert frames
        assert len(frames) <= plan.count
        assert [frame.timestamp for frame in frames] == list(plan.timestamps[: len(frames)])
        assert all(frame.path.is_file() and frame.path.stat().st_size > 0 for frame in frames)
        # Lexical order is chronological order, which every later stage assumes.
        assert [f.path.name for f in frames] == sorted(f.path.name for f in frames)

    def test_targeted_extraction_names_frames_by_timestamp(
        self, test_clip: Path, tmp_path: Path, ffmpeg_runner
    ) -> None:
        frames = extract_at_times(
            test_clip, tmp_path / "targets", [1.0, 2.5, 4.0], ffmpeg_runner
        )
        assert len(frames) == 3
        assert all(frame.path.is_file() for frame in frames)
        assert [frame.timestamp for frame in frames] == [1.0, 2.5, 4.0]

    def test_targeted_extraction_reuses_frames_already_on_disk(
        self, test_clip: Path, tmp_path: Path, ffmpeg_runner
    ) -> None:
        # §47: an interrupted dense pass resumes instead of repeating.
        target_dir = tmp_path / "targets"
        first = extract_at_times(test_clip, target_dir, [1.0, 2.0], ffmpeg_runner)
        stamps = {frame.path: frame.path.stat().st_mtime_ns for frame in first}
        second = extract_at_times(test_clip, target_dir, [1.0, 2.0, 3.0], ffmpeg_runner)
        assert len(second) == 3
        for path, stamp in stamps.items():
            assert path.stat().st_mtime_ns == stamp

    def test_clearing_reports_what_it_removed(
        self, test_clip: Path, tmp_path: Path, ffmpeg_runner
    ) -> None:
        target_dir = tmp_path / "targets"
        extract_at_times(test_clip, target_dir, [1.0, 2.0], ffmpeg_runner)
        assert clear_frames(target_dir) == 2
        assert not target_dir.exists()
        assert clear_frames(target_dir) == 0


class TestPipelineEndToEnd:
    """§126 step 05-06: import a recording and produce everything analysis reads."""

    @staticmethod
    def _import(media_service, project_manager, clip: Path):
        from backend.core.models.project import ProjectCreate

        project = project_manager.create(
            ProjectCreate(name="Media engine", target_duration_seconds=600)
        )
        media = media_service.import_media(project.id, MediaImport(path=str(clip)))
        return project, media

    def test_every_media_stage_completes(
        self, media_service, project_manager, pipeline_runner, test_clip: Path
    ) -> None:
        project, _ = self._import(media_service, project_manager, test_clip)

        outcomes = pipeline_runner.run_project(project.id)

        completed = [outcome.job.stage for outcome in outcomes if outcome.succeeded]
        # The media engine's own stages, in dependency order. Later stages run
        # too once their phases land, so this asserts the prefix: adding a phase
        # must not make a Phase 2 test wrong.
        assert completed[:5] == [
            JobStage.IMPORT,
            JobStage.PROBE,
            JobStage.PROXY,
            JobStage.AUDIO,
            JobStage.FRAMES,
        ]
        assert all(outcome.job.progress == 1.0 for outcome in outcomes)

    def test_the_probe_stage_persists_metadata_and_tracks(
        self, media_service, project_manager, database, pipeline_runner,
        two_track_clip: Path,
    ) -> None:
        project, media = self._import(media_service, project_manager, two_track_clip)
        pipeline_runner.run_project(project.id, max_jobs=2)

        stored = media_service.get_media(media.id)
        assert stored.state is MediaState.PROBED
        assert stored.metadata.duration_seconds == pytest.approx(6.0, abs=0.5)
        assert stored.metadata.width == 320

        tracks = MediaTrackRepository(database).list_for_media(media.id)
        assert len(tracks) == 3
        assert len(MediaTrackRepository(database).audio_tracks(media.id)) == 2

    def test_artefacts_land_in_the_project_layout(
        self, media_service, project_manager, paths, pipeline_runner, test_clip: Path
    ) -> None:
        # §43: proxy, audio and frames each in their own directory.
        project, media = self._import(media_service, project_manager, test_clip)
        pipeline_runner.run_project(project.id)

        project_paths = paths.project(project.id)
        assert list(project_paths.proxy.glob("*.mp4"))
        # Audio and frames are namespaced per media: two gameplay files in one
        # project must not overwrite each other's analysis stream.
        assert (project_paths.audio / media.id / "analysis.wav").is_file()
        assert list(project_paths.frames.rglob("*.jpg"))

    def test_frames_are_recorded_for_the_vision_stage(
        self, media_service, project_manager, database, pipeline_runner, test_clip: Path
    ) -> None:
        project, media = self._import(media_service, project_manager, test_clip)
        pipeline_runner.run_project(project.id)

        frames = FrameRepository(database).list_for_media(media.id)
        assert frames
        assert all(frame.sampling_level == "base" for frame in frames)
        assert all(not frame.analyzed for frame in frames)
        assert [frame.timestamp for frame in frames] == sorted(
            frame.timestamp for frame in frames
        )

    def test_job_results_carry_what_the_next_stage_needs(
        self, media_service, project_manager, pipeline_runner, test_clip: Path
    ) -> None:
        project, _ = self._import(media_service, project_manager, test_clip)
        outcomes = {
            outcome.job.stage: outcome.job
            for outcome in pipeline_runner.run_project(project.id)
        }

        assert outcomes[JobStage.PROBE].result["duration_seconds"] > 0
        assert outcomes[JobStage.PROXY].result["proxy_path"].endswith(".mp4")
        assert outcomes[JobStage.AUDIO].result["track_count"] == 1
        assert outcomes[JobStage.FRAMES].result["frames"] > 0
        assert outcomes[JobStage.FRAMES].result["from_proxy"] is True

    def test_the_runner_stops_where_the_pipeline_ends(
        self, media_service, project_manager, pipeline_runner, test_clip: Path
    ) -> None:
        # The first stage with no worker yet is a stopping point, not a
        # failure -- a stage that was never implemented must not be reported
        # as one that broke.
        project, _ = self._import(media_service, project_manager, test_clip)
        pipeline_runner.run_project(project.id)

        assert pipeline_runner.run_next(project.id) is None
        frontier = next(
            job
            for job in pipeline_runner.jobs.list_jobs(project.id)
            if job.stage not in pipeline_runner.supported_stages
        )
        assert frontier.status is JobStatus.QUEUED
        assert frontier.error_code is None

    def test_a_rerun_is_idempotent(
        self, media_service, project_manager, database, pipeline_runner, test_clip: Path
    ) -> None:
        project, media = self._import(media_service, project_manager, test_clip)
        runner = pipeline_runner
        runner.run_project(project.id)

        frames_job = next(
            job for job in runner.jobs.list_jobs(project.id) if job.stage is JobStage.FRAMES
        )
        # §90: re-analysis returns a finished stage to the queue and runs it
        # again over the same source.
        runner.jobs.requeue(frames_job.id)
        outcome = runner.run_job(frames_job.id)

        assert outcome.succeeded
        stored = FrameRepository(database).list_for_media(media.id)
        # Not doubled: the stage clears its previous output before writing.
        assert len(stored) == outcome.job.result["frames"]

    def test_a_missing_source_fails_the_stage_not_the_runner(
        self, media_service, project_manager, pipeline_runner, tmp_path: Path,
        clip_factory,
    ) -> None:
        clip = clip_factory(tmp_path / "temporary.mp4", seconds=2.0)
        project, media = self._import(media_service, project_manager, clip)
        clip.unlink()

        outcomes = pipeline_runner.run_project(project.id)
        assert outcomes[-1].error_code is ErrorCode.MEDIA_NOT_FOUND
        assert outcomes[-1].job.status is JobStatus.FAILED
        assert media_service.get_media(media.id).state is MediaState.FAILED

    def test_cancellation_stops_the_pipeline_cleanly(
        self, media_service, project_manager, job_manager, pipeline_runner,
        test_clip: Path,
    ) -> None:
        # §82: cancelling never leaves the project half-written.
        project, _ = self._import(media_service, project_manager, test_clip)
        job_manager.cancel_project(project.id)

        outcome = pipeline_runner.run_next(project.id)
        assert outcome is None or outcome.cancelled
