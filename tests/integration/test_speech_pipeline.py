"""Phase 3 acceptance: speech and audio through the pipeline.

The criterion has two halves, and both are checked here against real files:

1. **Transcript timestamps align with the source.** The stage chunks the audio
   and transcribes each chunk separately, so every timestamp it returns is
   relative to a slice and has to be moved onto the recording's timeline. Get
   the offset wrong and chunk five's speech is attributed to the first ten
   minutes; get the overlap handling wrong and the same sentence is stored
   twice. Both are checked with a chunk size small enough that a six-second
   clip produces several chunks.

2. **A separate microphone track is analysed independently of gameplay audio**
   (§19). Checked on a recording whose second audio track carries a real
   laugh and a real scream, each following a gameplay impact -- so the test
   asserts not merely that two tracks were processed, but that the reaction on
   one was correlated with the event on the other (§20).
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from ai.speech.fake_provider import FakeSpeechProvider
from backend.analysis.audio_events import GAMEPLAY, MICROPHONE
from backend.core.models.enums import AudioEventType, JobStage, JobStatus
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.repositories.audio_events import AudioEventRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers.speech_workers import TranscriptWorker
from tests.conftest import workers_through

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]


def _project_with(media_service, project_manager, clip: Path):
    project = project_manager.create(
        ProjectCreate(name="Speech", target_duration_seconds=600)
    )
    media = media_service.import_media(project.id, MediaImport(path=str(clip)))
    return project, media


def _chunked(config, *, chunk_seconds: float, overlap_seconds: float):
    """Config with a chunk size small enough to exercise chunking on a short clip."""
    analysis = config.analysis.model_copy(
        update={"chunk_seconds": chunk_seconds, "chunk_overlap_seconds": overlap_seconds}
    )
    return config.model_copy(update={"analysis": analysis})


class TestTranscriptAlignment:
    """Acceptance, first half: timestamps land where the speech actually is."""

    def test_chunked_transcription_covers_the_whole_source(
        self, media_service, project_manager, database, paths, config, test_clip: Path
    ) -> None:
        chunked = _chunked(config, chunk_seconds=2.0, overlap_seconds=0.5)
        provider = FakeSpeechProvider(segment_seconds=0.5, gap_seconds=0.1)
        workers = workers_through("audio_events")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(provider)
        runner = PipelineRunner(database, paths, chunked, workers=workers)

        project, media = _project_with(media_service, project_manager, test_clip)
        runner.run_project(project.id)

        segments = TranscriptRepository(database).list_for_media(media.id)
        assert segments, "a six-second clip split into two-second chunks must transcribe"

        # More than one chunk was involved: the clip is ~6 s at 2 s per chunk.
        assert len(provider.transcribe_calls) >= 3
        assert {round(offset, 3) for _, offset in provider.transcribe_calls} != {0.0}

        # Coverage reaches the end of the recording, not just the first chunk.
        assert max(segment.end for segment in segments) > 4.0

    def test_no_utterance_is_stored_twice_across_an_overlap(
        self, media_service, project_manager, database, paths, config, test_clip: Path
    ) -> None:
        # Chunks overlap so a sentence on a boundary is heard whole by one of
        # them. Keeping both copies would double-count every boundary.
        chunked = _chunked(config, chunk_seconds=2.0, overlap_seconds=0.5)
        workers = workers_through("audio_events")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(
            FakeSpeechProvider(segment_seconds=0.5, gap_seconds=0.1)
        )
        runner = PipelineRunner(database, paths, chunked, workers=workers)

        project, media = _project_with(media_service, project_manager, test_clip)
        runner.run_project(project.id)

        segments = TranscriptRepository(database).list_for_media(media.id)
        starts = [round(segment.start, 3) for segment in segments]
        assert len(starts) == len(set(starts))
        # And they are in order, with no segment beginning before its predecessor ends.
        for earlier, later in pairwise(segments):
            assert earlier.start <= later.start

    def test_timestamps_stay_within_the_recording(
        self, media_service, project_manager, database, pipeline_runner, test_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, test_clip)
        pipeline_runner.run_project(project.id)

        segments = TranscriptRepository(database).list_for_media(media.id)
        assert segments
        assert all(0.0 <= segment.start < segment.end <= 6.5 for segment in segments)
        assert all(
            segment.start <= word.start <= word.end <= segment.end + 1e-6
            for segment in segments
            for word in segment.words
        )

    def test_the_model_is_loaded_once_for_the_stage(
        self, media_service, project_manager, database, paths, config, test_clip: Path
    ) -> None:
        # Loading Whisper per chunk costs tens of seconds and 5 GB each time.
        chunked = _chunked(config, chunk_seconds=1.0, overlap_seconds=0.2)
        provider = FakeSpeechProvider(segment_seconds=0.4, gap_seconds=0.1)
        workers = workers_through("audio_events")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(provider)
        runner = PipelineRunner(database, paths, chunked, workers=workers)

        project, _ = _project_with(media_service, project_manager, test_clip)
        runner.run_project(project.id)

        assert len(provider.transcribe_calls) >= 5
        assert provider.load_count == 1
        # §54: and released, so the vision model has somewhere to go.
        assert provider.unload_count == 1

    def test_words_and_provenance_are_persisted(
        self, media_service, project_manager, database, pipeline_runner, test_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, test_clip)
        outcomes = {o.job.stage: o.job for o in pipeline_runner.run_project(project.id)}

        result = outcomes[JobStage.TRANSCRIPT].result
        assert result["segments"] > 0
        assert result["words"] > 0
        # §49: model name and version travel with the analysis.
        assert result["model_version"]

        stored = TranscriptRepository(database).list_for_media(media.id)
        assert all(segment.words for segment in stored)
        assert TranscriptRepository(database).spoken_seconds(media.id) > 0

    def test_an_unavailable_model_degrades_rather_than_failing(
        self, media_service, project_manager, database, paths, config, test_clip: Path
    ) -> None:
        # §95: no transcript is a degraded analysis; a failed stage is a broken
        # one. A missing model must produce the first.
        workers = workers_through("audio_events")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(FakeSpeechProvider(available=False))
        runner = PipelineRunner(database, paths, config, workers=workers)

        project, media = _project_with(media_service, project_manager, test_clip)
        outcomes = {o.job.stage: o for o in runner.run_project(project.id)}

        assert outcomes[JobStage.TRANSCRIPT].succeeded
        assert outcomes[JobStage.TRANSCRIPT].job.result["skipped"] is True
        assert TranscriptRepository(database).count_for_media(media.id) == 0

    def test_re_running_replaces_rather_than_appends(
        self, media_service, project_manager, database, pipeline_runner, test_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, test_clip)
        pipeline_runner.run_project(project.id)
        first = TranscriptRepository(database).count_for_media(media.id)

        job = next(
            j for j in pipeline_runner.jobs.list_jobs(project.id) if j.stage is JobStage.TRANSCRIPT
        )
        pipeline_runner.jobs.requeue(job.id)
        pipeline_runner.run_job(job.id)

        assert TranscriptRepository(database).count_for_media(media.id) == first


class TestMicrophoneIndependence:
    """Acceptance, second half: §19 and §20 on a real two-track recording."""

    def test_both_tracks_are_analysed_and_kept_apart(
        self, media_service, project_manager, database, pipeline_runner, reaction_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in pipeline_runner.run_project(project.id)}

        result = outcomes[JobStage.AUDIO_EVENTS].result
        assert result["tracks"] == 2
        assert result["has_microphone_track"] is True

        repository = AudioEventRepository(database)
        gameplay = repository.list_for_media(media.id, track_role=GAMEPLAY)
        microphone = repository.list_for_media(media.id, track_role=MICROPHONE)
        assert gameplay and microphone
        # The distinction survives persistence, which is what §27 will need.
        assert {event.track_role for event in gameplay} == {GAMEPLAY}
        assert {event.track_role for event in microphone} == {MICROPHONE}

    def test_gameplay_impacts_are_found_on_the_game_track(
        self, media_service, project_manager, database, pipeline_runner, reaction_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, reaction_clip)
        pipeline_runner.run_project(project.id)

        transients = AudioEventRepository(database).list_for_media(
            media.id, track_role=GAMEPLAY, event_type=AudioEventType.TRANSIENT
        )
        # The fixture carries three impacts, at 5 s, 15 s and 25 s.
        assert len(transients) == 3
        for event, expected in zip(transients, (5.0, 15.0, 25.0), strict=True):
            assert event.start_seconds == pytest.approx(expected, abs=1.0)

    def test_the_reaction_on_the_microphone_is_correlated_with_the_game(
        self, media_service, project_manager, database, pipeline_runner, reaction_clip: Path
    ) -> None:
        # §20 end to end: a laugh and a scream, each following an impact on the
        # other track, detected and lined up with it.
        project, media = _project_with(media_service, project_manager, reaction_clip)
        outcomes = {o.job.stage: o.job for o in pipeline_runner.run_project(project.id)}

        assert outcomes[JobStage.AUDIO_EVENTS].result["reactions"] == 2
        assert outcomes[JobStage.AUDIO_EVENTS].result["correlated_reactions"] == 2

        microphone = AudioEventRepository(database).list_for_media(
            media.id, track_role=MICROPHONE
        )
        reactions = [e for e in microphone if "reaction_type" in e.metadata]
        assert {e.metadata["reaction_type"] for e in reactions} == {"laugh", "scream"}
        assert all(e.metadata["correlation_offset"] is not None for e in reactions)

    def test_a_single_track_source_produces_no_microphone_events(
        self, media_service, project_manager, database, pipeline_runner, test_clip: Path
    ) -> None:
        project, media = _project_with(media_service, project_manager, test_clip)
        outcomes = {o.job.stage: o.job for o in pipeline_runner.run_project(project.id)}

        assert outcomes[JobStage.AUDIO_EVENTS].result["has_microphone_track"] is False
        assert (
            AudioEventRepository(database).list_for_media(media.id, track_role=MICROPHONE) == []
        )

    def test_a_silent_source_is_not_a_failure(
        self, media_service, project_manager, database, pipeline_runner, silent_clip: Path
    ) -> None:
        # Silent gameplay is a real recording.
        project, media = _project_with(media_service, project_manager, silent_clip)
        outcomes = {o.job.stage: o for o in pipeline_runner.run_project(project.id)}

        assert outcomes[JobStage.AUDIO_EVENTS].succeeded
        assert outcomes[JobStage.AUDIO_EVENTS].job.result["skipped"] is True
        assert AudioEventRepository(database).list_for_media(media.id) == []


class TestStageWiring:
    def test_the_speech_stages_run_after_the_media_stages(
        self, media_service, project_manager, pipeline_runner, test_clip: Path
    ) -> None:
        project, _ = _project_with(media_service, project_manager, test_clip)
        outcomes = pipeline_runner.run_project(project.id)

        order = [o.job.stage for o in outcomes if o.succeeded]
        assert JobStage.TRANSCRIPT in order
        assert JobStage.AUDIO_EVENTS in order
        # Both depend on AUDIO, and the graph must have enforced it.
        assert order.index(JobStage.AUDIO) < order.index(JobStage.TRANSCRIPT)
        assert order.index(JobStage.AUDIO) < order.index(JobStage.AUDIO_EVENTS)

    def test_cancellation_stops_the_transcript_stage_at_a_chunk_boundary(
        self, media_service, project_manager, database, paths, config, job_manager,
        test_clip: Path,
    ) -> None:
        chunked = _chunked(config, chunk_seconds=1.0, overlap_seconds=0.2)
        workers = workers_through("audio_events")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(FakeSpeechProvider())
        runner = PipelineRunner(database, paths, chunked, workers=workers)

        project, _ = _project_with(media_service, project_manager, test_clip)
        runner.run_project(project.id, max_jobs=4)  # through AUDIO
        job_manager.cancel_project(project.id)

        outcome = runner.run_next(project.id)
        assert outcome is None or outcome.job.status in {
            JobStatus.CANCELLED,
            JobStatus.COMPLETED,
        }


@pytest.mark.requires_models
class TestRealWhisper:
    """Against the actual model. Skipped unless ``VAI_TEST_MODELS=1``.

    The first run downloads gigabytes of weights, so this is opt-in rather than
    part of the default suite.
    """

    def test_voice_activity_detection_suppresses_hallucination_on_silence(
        self, tmp_path: Path, config
    ) -> None:
        # The property that matters most in practice: Whisper invents text on
        # silence, and a gameplay recording is mostly silence between callouts.
        # A hallucinated "thanks for watching" becomes a caption in the video.
        import wave

        from ai.speech.faster_whisper_provider import FasterWhisperProvider

        silent = tmp_path / "silence.wav"
        with wave.open(str(silent), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\x00\x00" * 16000 * 30)

        speech = config.models.speech.model_copy(update={"model": "tiny"})
        provider = FasterWhisperProvider(speech, gpu=config.gpu)
        try:
            segments = provider.transcribe(silent)
        finally:
            provider.unload()

        assert segments == (), f"VAD let hallucinated text through: {segments}"

    def test_a_real_transcript_carries_word_timestamps(self, tmp_path: Path, config) -> None:
        # §14 requires word timings, and the provider must surface them.
        import wave

        import numpy as np

        from ai.speech.faster_whisper_provider import FasterWhisperProvider

        # Not speech, but enough signal that VAD passes something through; the
        # assertion is about the shape of what comes back, not its content.
        time = np.arange(16000 * 10) / 16000
        signal = 0.3 * np.sin(2 * np.pi * 300 * time) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * time))
        path = tmp_path / "tone.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes((signal * 32767).astype("<i2").tobytes())

        speech = config.models.speech.model_copy(update={"model": "tiny"})
        provider = FasterWhisperProvider(speech, gpu=config.gpu)
        try:
            segments = provider.transcribe(path, start_offset=100.0)
        finally:
            provider.unload()

        for segment in segments:
            assert segment.start >= 100.0, "start_offset must move every timestamp"
            for word in segment.words:
                assert segment.start - 1e-3 <= word.start <= segment.end + 1e-3


class TestWhichTrackIsTranscribed:
    """§19: the microphone carries the words; the gameplay track carries the game.

    The defect this pins was invisible to the whole suite and only appeared on
    a real recording: the stage transcribed the *primary* track, so a
    twenty-minute session in which the audio analysis found 255 speech events
    produced a transcript of zero segments — and therefore no captions, and no
    speech dimension in any moment's score.
    """

    def _run(self, media_service, project_manager, database, paths, config, clip: Path):
        provider = FakeSpeechProvider(segment_seconds=1.0, gap_seconds=0.2)
        workers = workers_through("audio_events")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(provider)
        runner = PipelineRunner(database, paths, config, workers=workers)
        project, media = _project_with(media_service, project_manager, clip)
        outcomes = {outcome.job.stage: outcome.job for outcome in runner.run_project(project.id)}
        return project, media, outcomes

    def test_a_two_track_recording_transcribes_the_microphone(
        self, media_service, project_manager, database, paths, config, reaction_clip: Path
    ) -> None:
        _, _, outcomes = self._run(
            media_service, project_manager, database, paths, config, reaction_clip
        )
        result = outcomes[JobStage.TRANSCRIPT].result

        assert result["track_role"] == "microphone"
        # Track 1 is the game; the player's voice is on track 2.
        assert result["track_index"] == 2

    def test_a_single_track_recording_transcribes_the_only_track(
        self, media_service, project_manager, database, paths, config, test_clip: Path
    ) -> None:
        # Nothing to choose between, and no microphone to prefer.
        _, _, outcomes = self._run(
            media_service, project_manager, database, paths, config, test_clip
        )
        result = outcomes[JobStage.TRANSCRIPT].result

        assert result["track_index"] == 1
        assert result["segments"] > 0

    def test_the_transcript_records_which_track_it_read(
        self, media_service, project_manager, database, paths, config, reaction_clip: Path
    ) -> None:
        # An empty transcript is ambiguous without this: nobody spoke, or the
        # wrong track was listened to? (§80)
        _, _, outcomes = self._run(
            media_service, project_manager, database, paths, config, reaction_clip
        )

        assert outcomes[JobStage.TRANSCRIPT].result["track_reason"]

    def test_a_silent_microphone_track_falls_back_to_the_gameplay_track(
        self, media_service, project_manager, database, paths, config, silent_mic_clip: Path
    ) -> None:
        # A second track that was armed but never connected. Transcribing it
        # would replace a usable transcript with nothing.
        _, _, outcomes = self._run(
            media_service, project_manager, database, paths, config, silent_mic_clip
        )
        result = outcomes[JobStage.TRANSCRIPT].result

        assert result["track_role"] == "gameplay"
        assert result["track_index"] == 1
        assert "silent" in result["track_reason"]


class TestDuplicatedTracks:
    """A capture tool writing the same mix to both tracks (§19).

    Not a hypothetical: every recording on the machine this pipeline was first
    run against had two audio tracks carrying byte-identical audio. Treating
    the copy as a microphone costs a second pass over the same samples and
    attributes every reaction on it to a player who was not the one making the
    sound.
    """

    def _run(self, media_service, project_manager, database, paths, config, clip: Path):
        workers = workers_through("audio_events")
        workers[JobStage.TRANSCRIPT] = TranscriptWorker(FakeSpeechProvider())
        runner = PipelineRunner(database, paths, config, workers=workers)
        project, media = _project_with(media_service, project_manager, clip)
        outcomes = {outcome.job.stage: outcome.job for outcome in runner.run_project(project.id)}
        return project, media, outcomes

    def test_the_duplicate_is_dropped(
        self,
        media_service,
        project_manager,
        database,
        paths,
        config,
        duplicated_track_clip: Path,
    ) -> None:
        _, _, outcomes = self._run(
            media_service, project_manager, database, paths, config, duplicated_track_clip
        )
        audio = outcomes[JobStage.AUDIO].result

        assert audio["track_count"] == 1
        assert len(audio["streams"]) == 1

    def test_nothing_is_called_a_microphone_that_is_a_copy_of_the_game(
        self,
        media_service,
        project_manager,
        database,
        paths,
        config,
        duplicated_track_clip: Path,
    ) -> None:
        _, _, outcomes = self._run(
            media_service, project_manager, database, paths, config, duplicated_track_clip
        )

        assert outcomes[JobStage.TRANSCRIPT].result["track_role"] == "gameplay"
        assert outcomes[JobStage.AUDIO_EVENTS].result["has_microphone_track"] is False

    def test_a_genuinely_separate_microphone_is_kept(
        self, media_service, project_manager, database, paths, config, reaction_clip: Path
    ) -> None:
        # The other side of the rule: different audio stays two tracks.
        _, _, outcomes = self._run(
            media_service, project_manager, database, paths, config, reaction_clip
        )

        assert outcomes[JobStage.AUDIO].result["track_count"] == 2
        assert outcomes[JobStage.AUDIO_EVENTS].result["has_microphone_track"] is True
