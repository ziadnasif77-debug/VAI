"""The media stages: IMPORT, PROBE, PROXY, AUDIO, FRAMES.

SPEC sections 46 (the stage chain), 55 (proxy), 16 (frame sampling), 19
(separate microphone track), 7 (nothing loads a recording into RAM).

Each worker is the thin layer between a job and the media engine: it resolves
paths, calls into ``backend.media``, writes what came back to the database, and
returns a result for the job row. The media logic itself lives in
``backend.media`` and knows nothing about jobs, which is what lets it be tested
against a five-second clip.

Every worker here is idempotent. Re-running PROXY on a project that already has
one recognises it rather than spending ten minutes rebuilding it (§47).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from backend.core.errors import ErrorCode, MediaError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, MediaState
from backend.core.models.media import Media
from backend.database.repositories.frames import FrameRepository
from backend.database.repositories.media import MediaRepository, MediaTrackRepository
from backend.media.audio import extract_all_tracks
from backend.media.frames import clear_frames, extract_uniform, plan_base_sampling
from backend.media.probe import probe_media
from backend.media.proxy import generate_proxy
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.workers.media", LogChannel.PIPELINE)

#: Roles whose files must contain a video stream.
_VIDEO_ROLES: Final[frozenset[str]] = frozenset({"gameplay", "webcam"})

#: The forward progression of a media file through the §46 chain. ``FAILED`` is
#: deliberately absent -- it is an outcome, not a position.
_PROGRESSION: Final[tuple[MediaState, ...]] = (
    MediaState.REGISTERED,
    MediaState.PROBED,
    MediaState.PROXIED,
    MediaState.AUDIO_EXTRACTED,
    MediaState.ANALYZED,
)


class ImportWorker:
    """IMPORT -- confirm the registered file is still usable.

    Ingestion already validated and hashed the file (Phase 1). This stage exists
    because §42 keeps originals where the user put them: between pressing import
    and pressing analyse, the recording can be moved, renamed, or on a drive
    that is no longer plugged in. Catching that here fails one cheap stage
    instead of a probe, a proxy and an audio extraction in turn.

    The checksum is not recomputed: re-hashing a 40 GB recording to detect a
    change nobody made would cost minutes at the start of every analysis.
    """

    stage = JobStage.IMPORT

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        path = context.source_path()
        size = path.stat().st_size

        if size != media.size_bytes:
            raise MediaError(
                f"{path.name} changed size since it was imported "
                f"({media.size_bytes} -> {size} bytes).",
                code=ErrorCode.MEDIA_CORRUPTED,
                details={"media_id": media.id, "path": str(path),
                         "imported_size": media.size_bytes, "current_size": size},
                recoverable=False,
            )

        context.report(1.0, "Source verified")
        return {"path": str(path), "size_bytes": size, "container": media.container}


class ProbeWorker:
    """PROBE -- read duration, resolution, frame rate and stream layout (§45).

    Everything downstream is defined in terms of what this stage records:
    chunking needs the duration (§7), sampling needs the frame rate (§16), and
    microphone analysis needs to know a second audio stream exists (§19).
    """

    stage = JobStage.PROBE

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        path = context.source_path()
        context.report(0.1, "Reading media metadata")

        probe = probe_media(
            path,
            context.ffmpeg,
            require_video=media.role.value in _VIDEO_ROLES,
        )

        media_repo = MediaRepository(context.database)
        track_repo = MediaTrackRepository(context.database)
        with context.database.transaction():
            media_repo.update(
                media.model_copy(
                    update={
                        "metadata": probe.metadata,
                        "state": _forward(media.state, MediaState.PROBED),
                        "error_code": None,
                        "error_message": None,
                    }
                )
            )
            track_repo.replace_for_media(media.id, probe.to_media_tracks(media.id))

        context.report(1.0, "Metadata read")
        return {
            "duration_seconds": probe.duration_seconds,
            "width": probe.metadata.width,
            "height": probe.metadata.height,
            "fps": probe.metadata.fps,
            "video_codec": probe.metadata.video_codec,
            "audio_codec": probe.metadata.audio_codec,
            "tracks": len(probe.tracks),
            "audio_tracks": len(probe.audio_tracks),
            # §19: recorded on the job so the analysis screen can say whether a
            # microphone will be analysed separately, before any of it runs.
            "has_separate_microphone_track": probe.has_separate_microphone_track,
            "format": probe.format_name,
        }


class ProxyWorker:
    """PROXY -- build the 720p preview and analysis copy (§55).

    The original is never touched. Everything the UI shows and everything the
    visual stages decode comes from here, which is what keeps scrubbing a
    two-hour 1080p60 recording responsive.
    """

    stage = JobStage.PROXY

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        config = context.config.proxy

        if not config.enabled:
            context.report(1.0, "Proxy disabled")
            return {"skipped": True, "reason": "proxy disabled in configuration"}
        if media.metadata.has_video is False:
            context.report(1.0, "No video stream")
            return {"skipped": True, "reason": "source has no video stream"}

        # Re-probed rather than read back from the database: the exact stream
        # indices are needed for mapping, and a fresh probe costs milliseconds
        # while also confirming the file is still readable.
        source = context.source_path()
        probe = probe_media(source, context.ffmpeg, require_video=True)
        destination = _proxy_path(context, media)

        result = generate_proxy(
            source,
            destination,
            context.ffmpeg,
            config,
            probe=probe,
            on_progress=context.report,
            should_cancel=context.should_cancel,
            timeout_seconds=context.config.ffmpeg.timeout_seconds,
        )

        _advance_state(context, media, MediaState.PROXIED)
        return {
            "proxy_path": str(result.path),
            "duration_seconds": result.duration_seconds,
            "width": result.width,
            "height": result.height,
            "fps": result.fps,
            "size_bytes": result.size_bytes,
            "segments": result.segment_count,
            "reused_segments": result.reused_segments,
            "resumed": result.was_resumed,
        }


class AudioWorker:
    """AUDIO -- extract the normalised analysis stream for every audio track.

    Per track, not per file (§19): a recording that kept the microphone
    separate gets two streams, because a scream and an explosion are different
    evidence and mixing them first throws that away.

    The source is read, not the proxy: the proxy's audio has been through a
    lossy encode at preview bitrate, and transcription and transient detection
    deserve the original.
    """

    stage = JobStage.AUDIO

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        source = context.source_path()
        probe = probe_media(source, context.ffmpeg)

        if not probe.audio_tracks:
            context.report(1.0, "No audio track")
            return {"skipped": True, "reason": "source has no audio stream", "streams": []}

        streams = extract_all_tracks(
            source,
            context.paths.audio,
            context.ffmpeg,
            context.config.audio.analysis,
            probe=probe,
            on_progress=context.report,
            should_cancel=context.should_cancel,
            timeout_seconds=context.config.ffmpeg.timeout_seconds,
        )

        _advance_state(context, media, MediaState.AUDIO_EXTRACTED)
        return {
            "streams": [
                {
                    "path": str(stream.path),
                    "source_track_index": stream.source_track_index,
                    "duration_seconds": stream.duration_seconds,
                    "sample_rate": stream.sample_rate,
                    "channels": stream.channels,
                    "size_bytes": stream.size_bytes,
                    "is_primary": stream.is_primary,
                }
                for stream in streams
            ],
            "track_count": len(streams),
            "has_separate_microphone_track": probe.has_separate_microphone_track,
        }


class FramesWorker:
    """FRAMES -- the sparse base pass over the whole recording (§16).

    This is the base level of the sampling hierarchy only. The dense passes
    around candidate regions belong to the stages that nominate them, because
    nothing yet knows where anything interesting is -- running them now would
    mean sampling two hours densely to find the four minutes that mattered,
    which is exactly what §15 forbids.

    Frames come from the proxy: decoding 720p30 instead of 1080p60 is several
    times cheaper for pictures that only ever feed detectors and a vision model.
    """

    stage = JobStage.FRAMES

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        sampling = context.config.analysis.frame_sampling

        source, from_proxy = _frame_source(context, media)
        probe = probe_media(source, context.ffmpeg, require_video=True)
        plan = plan_base_sampling(probe.duration_seconds, sampling)
        if not plan.timestamps:
            context.report(1.0, "Nothing to sample")
            return {"skipped": True, "reason": "source too short to sample", "frames": 0}

        output_dir = context.paths.frames / media.id / plan.level
        frame_repo = FrameRepository(context.database)

        # A previous run's frames would be globbed alongside the new ones and
        # mapped to the wrong timestamps, so the directory starts empty (§84).
        removed = clear_frames(output_dir)
        with context.database.transaction():
            frame_repo.delete_for_media(media.id, level=plan.level)

        frames = extract_uniform(
            source,
            output_dir,
            plan,
            context.ffmpeg,
            jpeg_quality=sampling.jpeg_quality,
            duration_seconds=probe.duration_seconds,
            on_progress=lambda report: context.report(
                report.fraction if report.fraction is not None else 0.0,
                f"Sampling frames ({report.frame or 0} extracted)",
            ),
            should_cancel=context.should_cancel,
            timeout_seconds=context.config.ffmpeg.timeout_seconds,
        )

        with context.database.transaction():
            stored = frame_repo.add_many(context.project_id, media.id, frames)

        if plan.was_capped:
            logger.warning(
                "Frame budget widened the sampling interval",
                extra={"media_id": media.id, **plan.summary()},
            )

        context.report(1.0, f"{stored} frames sampled")
        return {
            "frames": stored,
            "directory": str(output_dir),
            "from_proxy": from_proxy,
            "replaced_frames": removed,
            **plan.summary(),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _proxy_path(context: WorkerContext, media: Media) -> Path:
    """Where this file's proxy lives (§43).

    Derived from the source filename rather than the media id so the proxy
    directory stays readable to a human looking for the right file.
    """
    return context.paths.proxy / f"{Path(media.filename).stem}_proxy.mp4"


def _frame_source(context: WorkerContext, media: Media) -> tuple[Path, bool]:
    """Return the file to sample and whether it is the proxy.

    Falls back to the original when no proxy exists -- proxy generation can be
    disabled in configuration, and a stage that simply failed in that case
    would make an optional feature mandatory.
    """
    proxy_path = _proxy_path(context, media)
    if proxy_path.is_file() and proxy_path.stat().st_size > 0:
        return proxy_path, True
    return context.source_path(), False


def _forward(current: MediaState, target: MediaState) -> MediaState:
    """Return the later of two states.

    Stages legitimately re-run out of order -- a re-queued AUDIO after FRAMES, a
    resumed PROXY on an already-analysed file -- and a straight assignment would
    rewind a file's state to something earlier than the work actually done.

    ``FAILED`` is not a point on the progression: a file that failed and then
    succeeded is recovering, so the new state wins outright.
    """
    if current is MediaState.FAILED:
        return target
    if target is MediaState.FAILED:
        return target
    return max(current, target, key=_PROGRESSION.index)


def _advance_state(context: WorkerContext, media: Media, state: MediaState) -> None:
    """Persist a media state change, unless it would be a step backwards."""
    resolved = _forward(media.state, state)
    if resolved is media.state:
        return
    repository = MediaRepository(context.database)
    with context.database.transaction():
        repository.update(media.model_copy(update={"state": resolved}))


__all__ = [
    "AudioWorker",
    "FramesWorker",
    "ImportWorker",
    "ProbeWorker",
    "ProxyWorker",
]
