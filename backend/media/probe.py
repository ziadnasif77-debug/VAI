"""Media probing (SPEC sections 45, 65, 19).

ffprobe answers the questions every later stage depends on: how long is this
recording, what resolution and frame rate, does it carry a second audio track.
Getting them wrong is expensive -- a frame rate rounded from 59.94 to 60 puts
every derived timestamp minutes out of place by the end of a two-hour source.

The probe reads container metadata only. It never decodes the file, so probing
an eight-hour recording costs milliseconds and no memory (§7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from backend.core.errors import ErrorCode, MediaError
from backend.core.ids import new_id
from backend.core.logging import LogChannel, get_logger
from backend.core.models.media import MediaMetadata, MediaTrack
from backend.media.ffmpeg import FFmpegRunner, parse_rational, parse_timestamp

logger = get_logger("media.probe", LogChannel.PIPELINE)

#: Probing is metadata-only; a file that needs longer than this is not going to
#: succeed by waiting, it is going to be on a disconnected network drive.
PROBE_TIMEOUT_SECONDS: Final[int] = 120

#: ffprobe's ``codec_type`` mapped onto the four kinds §45 stores.
_TRACK_KINDS: Final[dict[str, str]] = {
    "video": "video",
    "audio": "audio",
    "subtitle": "subtitle",
    "data": "data",
    "attachment": "data",
}

#: Cover art and thumbnails are video streams by codec_type. Treating them as
#: the video track would report a 600x600 recording at 1 fps.
_IMAGE_CODECS: Final[frozenset[str]] = frozenset({"mjpeg", "png", "bmp", "gif", "webp"})


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """One stream as ffprobe reported it.

    Kept separate from :class:`~backend.core.models.media.MediaTrack` because a
    probe result exists before any database row does -- QA probes a finished
    render that is not a ``media`` row at all.
    """

    index: int
    kind: str
    codec: str | None = None
    language: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bitrate: int | None = None
    duration_seconds: float | None = None
    #: Where this stream begins, in container time. Two streams that start at
    #: different moments are a mux-level A/V desync, which is the kind this
    #: pipeline can actually produce (§76).
    start_seconds: float | None = None
    is_default: bool = False
    #: Cover art, thumbnails: a video stream that is not moving pictures.
    is_image: bool = False

    def to_media_track(self, media_id: str) -> MediaTrack:
        """Return the persistable row for ``media_id``."""
        return MediaTrack(
            id=new_id("media_track"),
            media_id=media_id,
            track_index=self.index,
            kind=self.kind,  # type: ignore[arg-type]  # constrained by _TRACK_KINDS
            codec=self.codec,
            language=self.language,
            width=self.width,
            height=self.height,
            fps=self.fps,
            sample_rate=self.sample_rate,
            channels=self.channels,
            bitrate=self.bitrate,
            is_default=self.is_default,
        )


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Everything ffprobe reported about one file."""

    path: Path
    metadata: MediaMetadata
    tracks: tuple[TrackInfo, ...]
    format_name: str | None = None
    size_bytes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def video_tracks(self) -> tuple[TrackInfo, ...]:
        """Moving-picture streams, excluding embedded cover art."""
        return tuple(t for t in self.tracks if t.kind == "video" and not t.is_image)

    @property
    def audio_tracks(self) -> tuple[TrackInfo, ...]:
        return tuple(t for t in self.tracks if t.kind == "audio")

    @property
    def has_separate_microphone_track(self) -> bool:
        """Whether a second audio stream exists (§19).

        Game audio and a player's microphone carry very different semantic
        weight, so a recording that kept them apart must be analysed as two
        signals rather than one mix.
        """
        return len(self.audio_tracks) > 1

    @property
    def duration_seconds(self) -> float:
        """Duration in seconds; ``0.0`` only when the probe could not find one."""
        return self.metadata.duration_seconds or 0.0

    def to_media_tracks(self, media_id: str) -> list[MediaTrack]:
        """Return every stream as a persistable row for ``media_id``."""
        return [track.to_media_track(media_id) for track in self.tracks]


def probe_media(
    path: Path,
    runner: FFmpegRunner,
    *,
    require_video: bool = False,
    require_duration: bool = True,
) -> ProbeResult:
    """Read container and stream metadata from ``path``.

    Args:
        path: file to probe.
        runner: supplies the ffprobe binary and the failure translation.
        require_video: reject a file with no moving-picture stream. Set for
            gameplay sources; left off for microphone and music files.
        require_duration: reject a file whose duration cannot be determined.
            Chunking, sampling and progress reporting are all defined in terms
            of duration (§7, §16), so a source without one cannot be analysed --
            failing here is more useful than dividing by an assumed length.

    Raises:
        MediaError: the file is missing, unreadable, has no streams, or is
            missing information the pipeline requires.
    """
    source = Path(path)
    if not source.is_file():
        raise MediaError(
            f"File not found: {source}",
            code=ErrorCode.MEDIA_NOT_FOUND,
            details={"path": str(source)},
            recoverable=False,
        )

    result = runner.run(
        [
            runner.ffprobe_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_error",
            str(source),
        ],
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        error_code=ErrorCode.MEDIA_PROBE_FAILED,
        error_message=f"ffprobe could not read {source.name}.",
        details={"path": str(source)},
        # ffprobe exits non-zero for an unreadable file but still prints a JSON
        # "error" object, which is a better diagnosis than the exit code.
        check=False,
    )

    payload = _parse_json(result.stdout, source)
    error = payload.get("error")
    if error or result.returncode != 0:
        message = (
            str(error.get("string", "unreadable")) if isinstance(error, dict) else "unreadable"
        )
        raise MediaError(
            f"{source.name} could not be read: {message}",
            code=ErrorCode.MEDIA_CORRUPTED,
            details={"path": str(source), "ffprobe_error": message,
                     "returncode": result.returncode},
            recoverable=False,
        )

    streams = payload.get("streams") or []
    if not isinstance(streams, list) or not streams:
        raise MediaError(
            f"{source.name} contains no media streams.",
            code=ErrorCode.MEDIA_CORRUPTED,
            details={"path": str(source)},
            recoverable=False,
        )

    container = payload.get("format") or {}
    tracks = tuple(_track_from_stream(stream) for stream in streams if isinstance(stream, dict))
    metadata = _metadata_from(container, tracks)

    if require_video and not any(t.kind == "video" and not t.is_image for t in tracks):
        raise MediaError(
            f"{source.name} has no video stream.",
            code=ErrorCode.UNSUPPORTED_CONTAINER,
            details={"path": str(source),
                     "streams": [f"{t.index}:{t.kind}" for t in tracks]},
            recoverable=False,
        )
    if require_duration and not metadata.duration_seconds:
        raise MediaError(
            f"{source.name} has no readable duration.",
            code=ErrorCode.MEDIA_CORRUPTED,
            details={"path": str(source), "format": container.get("format_name")},
            recoverable=False,
        )

    probe = ProbeResult(
        path=source,
        metadata=metadata,
        tracks=tracks,
        format_name=_text(container.get("format_name")),
        size_bytes=_integer(container.get("size")),
        raw=payload,
    )
    logger.info(
        "Probed media",
        extra={
            "path": str(source),
            "duration": metadata.duration_seconds,
            "resolution": f"{metadata.width}x{metadata.height}"
            if metadata.width
            else None,
            "fps": metadata.fps,
            "audio_tracks": len(probe.audio_tracks),
        },
    )
    return probe


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _parse_json(text: str, source: Path) -> dict[str, Any]:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(
            f"ffprobe returned output that is not JSON for {source.name}.",
            code=ErrorCode.MEDIA_PROBE_FAILED,
            details={"path": str(source), "output": text[:500]},
            cause=exc,
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _track_from_stream(stream: dict[str, Any]) -> TrackInfo:
    codec_type = _text(stream.get("codec_type")) or "data"
    codec = _text(stream.get("codec_name"))
    disposition = stream.get("disposition") or {}
    tags = stream.get("tags") or {}

    # An attached cover image is a video stream with a still-image codec and,
    # usually, the `attached_pic` disposition.
    is_image = codec_type == "video" and (
        bool(disposition.get("attached_pic")) or (codec or "") in _IMAGE_CODECS
    )

    return TrackInfo(
        index=_integer(stream.get("index")) or 0,
        kind=_TRACK_KINDS.get(codec_type, "data"),
        codec=codec,
        language=_text(tags.get("language")) if isinstance(tags, dict) else None,
        width=_positive(stream.get("width")),
        height=_positive(stream.get("height")),
        # avg_frame_rate is the true average and is what variable-frame-rate
        # recordings (every screen capture tool) need; r_frame_rate is the
        # container's nominal base rate and is the fallback.
        fps=parse_rational(_text(stream.get("avg_frame_rate")))
        or parse_rational(_text(stream.get("r_frame_rate"))),
        sample_rate=_positive(stream.get("sample_rate")),
        channels=_positive(stream.get("channels")),
        bitrate=_integer(stream.get("bit_rate")),
        duration_seconds=_stream_duration(stream),
        start_seconds=parse_timestamp(_text(stream.get("start_time"))),
        is_default=bool(disposition.get("default")) if isinstance(disposition, dict) else False,
        is_image=is_image,
    )


def _metadata_from(container: dict[str, Any], tracks: tuple[TrackInfo, ...]) -> MediaMetadata:
    video = _primary(t for t in tracks if t.kind == "video" and not t.is_image)
    audio = _primary(t for t in tracks if t.kind == "audio")
    return MediaMetadata(
        duration_seconds=_duration_of(container, tracks),
        width=video.width if video else None,
        height=video.height if video else None,
        fps=video.fps if video else None,
        video_codec=video.codec if video else None,
        audio_codec=audio.codec if audio else None,
        sample_rate=audio.sample_rate if audio else None,
        channels=audio.channels if audio else None,
        bitrate=_integer(container.get("bit_rate")),
        has_video=video is not None,
        has_audio=audio is not None,
    )


def _primary(candidates: Any) -> TrackInfo | None:
    """Return the default stream of a kind, or the first one."""
    tracks = list(candidates)
    if not tracks:
        return None
    return next((track for track in tracks if track.is_default), tracks[0])


def _duration_of(container: dict[str, Any], tracks: tuple[TrackInfo, ...]) -> float | None:
    """Resolve the source duration.

    Container duration first; a Matroska or transport stream may omit it, in
    which case the longest stream is the honest answer. Neither is guessed --
    ``None`` propagates and the caller decides whether that is fatal.
    """
    duration = parse_timestamp(_text(container.get("duration")))
    if duration and duration > 0:
        return duration
    stream_durations = [t.duration_seconds for t in tracks if t.duration_seconds]
    return max(stream_durations) if stream_durations else None


def _stream_duration(stream: dict[str, Any]) -> float | None:
    duration = parse_timestamp(_text(stream.get("duration")))
    if duration and duration > 0:
        return duration
    # Some containers report duration only as a count of time-base units.
    ticks = _integer(stream.get("duration_ts"))
    time_base = parse_rational(_text(stream.get("time_base")))
    if ticks and time_base:
        return ticks * time_base
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _positive(value: Any) -> int | None:
    parsed = _integer(value)
    return parsed if parsed and parsed > 0 else None


__all__ = ["PROBE_TIMEOUT_SECONDS", "ProbeResult", "TrackInfo", "probe_media"]
