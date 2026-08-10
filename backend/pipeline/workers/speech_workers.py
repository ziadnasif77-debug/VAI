"""The speech and audio stages: TRANSCRIPT and AUDIO_EVENTS.

SPEC sections 14 (speech-to-text), 18 (audio analysis), 19 (separate
microphone), 20 (player reactions), 7 (chunked, never in RAM), 54 (one model in
VRAM at a time).

Both stages read the analysis streams the AUDIO stage produced, and they read
them from that stage's recorded result rather than by rebuilding paths from
naming conventions -- the job result is the contract (§81).

TRANSCRIPT chunks. Not because Whisper cannot handle a long file (it windows
internally), but because a two-hour transcription that reports nothing for
forty minutes and cannot be cancelled is unusable. Chunks give progress, a
cancellation checkpoint, and a place to resume.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ai.providers.base import SpeechProvider, TranscriptSegment
from ai.speech import create_speech_provider
from backend.analysis.audio_events import (
    GAMEPLAY,
    MICROPHONE,
    AudioEvent,
    TrackRole,
    detect_audio_events,
    measure_loudness,
)
from backend.analysis.reactions import detect_reactions
from backend.analysis.signal import analyse_stream
from backend.core.errors import AnalysisError, ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, MediaRole
from backend.database.repositories.audio_events import AudioEventRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.media.chunking import plan_chunks
from backend.media.ffmpeg import format_seconds
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.workers.speech", LogChannel.PIPELINE)

#: Directory for the per-chunk audio slices, inside the project's audio dir.
CHUNK_DIRNAME: Final[str] = "chunks"


@dataclass(frozen=True, slots=True)
class _Stream:
    """One analysis stream, with the role §19 assigns it."""

    path: Path
    role: TrackRole
    track_index: int | None
    duration_seconds: float
    is_primary: bool


class TranscriptWorker:
    """TRANSCRIPT -- speech with word timestamps (§14).

    The model is loaded once for the whole stage and unloaded when it finishes.
    Loading Whisper large-v3 costs tens of seconds and 5 GB of VRAM; doing it
    per chunk would multiply that by the number of chunks, and holding it after
    the stage would leave nothing for the vision model on an 8 GB card (§54).
    """

    stage = JobStage.TRANSCRIPT

    def __init__(self, provider: SpeechProvider | None = None) -> None:
        """
        Args:
            provider: injected for tests. In production the provider is built
                from ``config/models.yaml`` at the start of the stage, so a
                model swap is a configuration change (§13).
        """
        self._provider = provider

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        streams = _streams_for(context)
        primary = next((stream for stream in streams if stream.is_primary), None)
        if primary is None:
            context.report(1.0, "No audio to transcribe")
            return {"skipped": True, "reason": "no analysis audio", "segments": 0}

        provider = self._provider or create_speech_provider(
            context.config, model_root=context.models_dir
        )
        if not provider.is_available():
            # §95: a missing model degrades, it does not corrupt. The stage
            # records why and the pipeline continues without a transcript.
            context.report(1.0, "Speech model unavailable")
            logger.warning(
                "Speech provider unavailable; continuing without a transcript",
                extra={"media_id": media.id, "provider": provider.info().provider},
            )
            return {
                "skipped": True,
                "reason": "speech provider unavailable",
                "segments": 0,
                "model": provider.info().name,
            }

        analysis = context.config.analysis
        plan = plan_chunks(
            primary.duration_seconds,
            chunk_seconds=analysis.chunk_seconds,
            overlap_seconds=analysis.chunk_overlap_seconds,
            event_overlap_seconds=analysis.event_overlap_seconds,
        )
        chunk_dir = context.paths.audio / media.id / CHUNK_DIRNAME

        segments: list[TranscriptSegment] = []
        language = context.config.models.speech.language
        try:
            provider.load()
            for chunk in plan:
                if context.should_cancel():
                    from backend.media.ffmpeg import CancelledError

                    raise CancelledError(
                        details={"stage": "transcript", "chunk": chunk.index}
                    )

                context.report(
                    chunk.core_start / max(primary.duration_seconds, 1e-6),
                    f"Transcribing {chunk.index + 1}/{len(plan)}",
                )
                slice_path = _extract_slice(context, primary.path, chunk_dir, chunk)
                try:
                    produced = provider.transcribe(
                        slice_path, language=language, start_offset=chunk.start
                    )
                finally:
                    slice_path.unlink(missing_ok=True)

                # Overlap exists so a sentence spanning a boundary is heard
                # whole by one chunk. Keeping every result would then store it
                # twice, so a segment belongs to the chunk whose core contains
                # its midpoint -- and every instant has exactly one such chunk.
                segments.extend(
                    segment
                    for segment in produced
                    if chunk.owns((segment.start + segment.end) / 2.0)
                )
        finally:
            provider.unload()
            shutil.rmtree(chunk_dir, ignore_errors=True)

        segments.sort(key=lambda segment: segment.start)
        repository = TranscriptRepository(context.database)
        with context.database.transaction():
            stored = repository.replace_for_media(context.project_id, media.id, segments)

        info = provider.info()
        context.report(1.0, f"{stored} utterances transcribed")
        return {
            "segments": stored,
            "chunks": len(plan),
            "words": sum(len(segment.words) for segment in segments),
            "spoken_seconds": round(
                sum(segment.end - segment.start for segment in segments), 3
            ),
            "language": next(
                (segment.language for segment in segments if segment.language), None
            ),
            # §49: provenance travels with the result, so a wrong transcript is
            # traceable to the model that produced it.
            "model": info.name,
            "model_version": info.version,
            "device": info.device,
        }


class AudioEventsWorker:
    """AUDIO_EVENTS -- level, silence, onsets, speech and reactions (§18-§20).

    Every audio stream of the file is analysed, and each keeps its own role.
    That is the whole of §19: the game track and the microphone are measured
    separately, so a scream is never mistaken for an explosion, and a reaction
    can be correlated against what the game was doing at that moment.
    """

    stage = JobStage.AUDIO_EVENTS

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        streams = _streams_for(context)
        if not streams:
            context.report(1.0, "No audio to analyse")
            return {"skipped": True, "reason": "no analysis audio", "events": 0}

        audio_config = context.config.analysis.audio
        by_role: dict[TrackRole, list[AudioEvent]] = {}
        events: list[AudioEvent] = []
        features_by_role = {}

        for position, stream in enumerate(streams):
            if context.should_cancel():
                from backend.media.ffmpeg import CancelledError

                raise CancelledError(details={"stage": "audio_events"})

            context.report(
                position / max(len(streams), 1),
                f"Analysing {stream.role} audio ({position + 1}/{len(streams)})",
            )
            features = analyse_stream(
                stream.path,
                window_seconds=audio_config.window_seconds,
                hop_seconds=audio_config.hop_seconds,
            )
            loudness = (
                measure_loudness(stream.path, context.ffmpeg)
                if audio_config.detect.lufs
                else None
            )
            detected = detect_audio_events(
                features, audio_config, track_role=stream.role, loudness=loudness
            )
            by_role.setdefault(stream.role, []).extend(detected)
            features_by_role[stream.role] = features
            events.extend(detected)

        reactions = self._reactions(context, features_by_role, by_role)
        events.extend(reaction.as_audio_event() for reaction in reactions)
        events.sort(key=lambda event: event.start_seconds)

        repository = AudioEventRepository(context.database)
        with context.database.transaction():
            stored = repository.replace_for_media(context.project_id, media.id, events)

        context.report(1.0, f"{stored} audio events")
        return {
            "events": stored,
            "tracks": len(streams),
            "by_type": repository.counts_by_type(media.id),
            "reactions": len(reactions),
            "correlated_reactions": sum(1 for item in reactions if item.is_correlated),
            "has_microphone_track": any(
                stream.role == MICROPHONE for stream in streams
            ),
        }

    def _reactions(self, context: WorkerContext, features_by_role, by_role):
        """Run §20 detection on the microphone track, if there is one."""
        microphone = features_by_role.get(MICROPHONE)
        if microphone is None:
            return []
        return detect_reactions(
            microphone,
            by_role.get(MICROPHONE, []),
            context.config.analysis,
            gameplay_events=by_role.get(GAMEPLAY, []),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _streams_for(context: WorkerContext) -> list[_Stream]:
    """Resolve this file's analysis streams and their §19 roles.

    Two ways a microphone reaches the pipeline, and both are handled here so no
    detector has to care which one happened:

    * a file imported with the ``microphone`` role -- every stream in it is the
      player's voice;
    * a gameplay recording that kept the microphone on a second audio track --
      the first track is the game, the rest are the player.
    """
    media = context.require_media()
    result = context.stage_result(JobStage.AUDIO)
    raw = result.get("streams") or []
    if not isinstance(raw, list) or not raw:
        return []

    media_is_microphone = media.role in {MediaRole.MICROPHONE, MediaRole.EXTERNAL_AUDIO}
    auto_detect = context.config.analysis.microphone.auto_detect_track

    streams: list[_Stream] = []
    for position, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        path = Path(str(entry.get("path", "")))
        if not path.is_file():
            logger.warning(
                "Analysis stream is missing", extra={"path": str(path), "media_id": media.id}
            )
            continue
        is_primary = bool(entry.get("is_primary", position == 0))
        if media_is_microphone:
            role: TrackRole = MICROPHONE
        elif auto_detect and not is_primary:
            role = MICROPHONE
        else:
            role = GAMEPLAY
        streams.append(
            _Stream(
                path=path,
                role=role,
                track_index=entry.get("source_track_index"),
                duration_seconds=float(entry.get("duration_seconds") or 0.0),
                is_primary=is_primary,
            )
        )
    return streams


def _extract_slice(
    context: WorkerContext, source: Path, destination_dir: Path, chunk
) -> Path:
    """Cut one chunk out of an analysis stream.

    A stream copy, not a re-encode: the analysis WAV is already the format the
    model wants, so cutting it costs a read and a write of the bytes involved
    and changes not one sample.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / f"{chunk.index:04d}.wav"
    target.unlink(missing_ok=True)

    context.ffmpeg.run(
        [
            *context.ffmpeg.base_arguments(),
            "-ss",
            format_seconds(chunk.start),
            "-t",
            format_seconds(chunk.duration),
            "-i",
            str(source),
            "-c",
            "copy",
            str(target),
        ],
        error_code=ErrorCode.TRANSCRIPTION_FAILED,
        error_message=f"Could not cut chunk {chunk.index} out of the analysis audio.",
        details={"source": str(source), "chunk": chunk.index},
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise AnalysisError(
            f"Chunk {chunk.index} of the analysis audio is empty.",
            code=ErrorCode.TRANSCRIPTION_FAILED,
            details={"source": str(source), "chunk": chunk.index},
        )
    return target


__all__ = ["CHUNK_DIRNAME", "AudioEventsWorker", "TranscriptWorker"]
