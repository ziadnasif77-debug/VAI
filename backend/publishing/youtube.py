"""YouTube publisher — resumable upload of a QA-passed render (§50, §51).

The slot for this has existed since the publishing seam was written: the
``PUBLISH`` stage in the job graph, the ``YOUTUBE`` target in the enum, the
metadata model already carrying title, description, tags, category, visibility
and thumbnail with YouTube's own limits. What arrives here is only what the
docstring of that seam promised — one publisher class.

Three properties shape the implementation:

**Resumable, chunk by chunk.** A finished render on this machine measured
1.14 GiB, and a residential uplink drops. The upload protocol is Google's
resumable one: an init request yields a session URL, chunks go up with
``Content-Range``, a 308 answers with how far the server got, and resuming is
asking and continuing from there — including across a process restart, because
the session URL is worthless to anyone without the OAuth token.

**Fresh token per chunk.** An access token lives about an hour; a large upload
on a slow line can outlive it. The token provider refreshes behind a margin,
so asking per chunk costs one file read in the common case.

**Errors say what to do.** 401 → sign in again; 403 with a quota reason →
YouTube's daily upload quota, try tomorrow; anything 5xx retries with backoff
before it surfaces. §79's rule — a finding carries its remedy — applies to
uploads as much as to QA.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from backend.core.errors import ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import PublishStatus, PublishTarget
from backend.core.models.publishing import PublishRequest, PublishResult, VideoMetadata
from backend.publishing.base import PublishError
from backend.publishing.google_oauth import TokenProvider, Transport, UrllibTransport

logger = get_logger("publishing.youtube", LogChannel.RENDERING)

UPLOAD_URL: Final[str] = (
    "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
)
THUMBNAIL_URL: Final[str] = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
PLAYLIST_URL: Final[str] = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"

#: YouTube's category id for Gaming. The one default this app may safely
#: assume about its own users.
GAMING_CATEGORY_ID: Final[str] = "20"

_RETRYABLE: Final[frozenset[int]] = frozenset({500, 502, 503, 504})
_BACKOFF_BASE_SECONDS: Final[float] = 2.0


class YouTubePublisher:
    """Uploads a render to the connected channel. Explicit action only (§51)."""

    target = PublishTarget.YOUTUBE

    def __init__(
        self,
        tokens: TokenProvider,
        *,
        chunk_bytes: int = 8 * 1024 * 1024,
        max_retries: int = 5,
        default_playlist: str | None = None,
        transport: Transport | None = None,
        sleep=time.sleep,
    ) -> None:
        """
        Args:
            tokens: the OAuth session. Its absence is what ``is_configured``
                reports, so the UI can say "connect YouTube" instead of
                failing an upload later.
            chunk_bytes: upload chunk size. Google requires a multiple of
                256 KiB; the configured default is 8 MiB.
            sleep: injectable for the retry tests, like every clock here.
        """
        self._tokens = tokens
        self._chunk = max(262_144, (chunk_bytes // 262_144) * 262_144)
        self._retries = max_retries
        self._default_playlist = default_playlist
        self._transport = transport or UrllibTransport()
        self._sleep = sleep

    def is_configured(self) -> bool:
        return self._tokens.is_authorised()

    def publish(self, request: PublishRequest, render_path: Path) -> PublishResult:
        source = Path(render_path)
        if not source.is_file():
            raise PublishError(
                f"Render file not found: {source}",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"render_path": str(source), "render_id": request.render_id},
                recoverable=False,
            )

        size = source.stat().st_size
        session_url = self._begin_session(request.metadata, size)
        video_id = self._upload(source, size, session_url)

        thumbnail_note = self._maybe_thumbnail(video_id, request.metadata)
        playlist_note = self._maybe_playlist(video_id, request.destination)

        logger.info(
            "Uploaded the render to YouTube",
            extra={
                "project_id": request.project_id,
                "video_id": video_id,
                "size_bytes": size,
                "visibility": request.metadata.visibility.value,
            },
        )
        return PublishResult(
            status=PublishStatus.COMPLETED,
            target=self.target,
            external_id=video_id,
            external_url=f"https://youtu.be/{video_id}",
            completed_at=datetime.now(timezone.utc),
            notes=[note for note in (thumbnail_note, playlist_note) if note],
        )

    # -- the resumable protocol -----------------------------------------

    def _begin_session(self, metadata: VideoMetadata, size: int) -> str:
        body = json.dumps(_snippet(metadata)).encode("utf-8")
        status, headers, raw = self._send(
            "POST",
            UPLOAD_URL,
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            body=body,
        )
        if status != 200 or "location" not in headers:
            raise self._api_error("starting the upload", status, raw)
        return headers["location"]

    def _upload(self, source: Path, size: int, session_url: str) -> str:
        """PUT chunks until the session answers with the video.

        The server owns the offset. After any interruption — a retryable
        status, a dropped connection surfaced as PublishError by the
        transport — the next loop asks the session where it stands rather
        than trusting local arithmetic, which is the entire point of the
        resumable protocol.
        """
        offset = 0
        with source.open("rb") as handle:
            while offset < size:
                handle.seek(offset)
                chunk = handle.read(self._chunk)
                end = offset + len(chunk) - 1
                status, headers, raw = self._send(
                    "PUT",
                    session_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    body=chunk,
                    retry_with=lambda: self._query_offset(session_url, size),
                )
                if status in (200, 201):
                    payload = _json_of(raw)
                    video_id = str(payload.get("id", ""))
                    if not video_id:
                        raise self._api_error("finishing the upload", status, raw)
                    return video_id
                if status == 308:
                    offset = _next_offset(headers, fallback=end + 1)
                    continue
                raise self._api_error("uploading", status, raw)
        raise PublishError(
            "The upload ended without YouTube returning a video id.",
            code=ErrorCode.PUBLISH_FAILED,
            details={"size": size},
        )

    def _query_offset(self, session_url: str, size: int) -> int:
        """Ask the session how much it already has (the resume handshake)."""
        status, headers, _ = self._send(
            "PUT",
            session_url,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
            body=b"",
        )
        if status in (200, 201):
            return size
        if status == 308:
            return _next_offset(headers, fallback=0)
        return 0

    # -- extras that ride along ------------------------------------------

    def _maybe_thumbnail(self, video_id: str, metadata: VideoMetadata) -> str | None:
        """Set the thumbnail when one was provided. A failure is a note.

        The video is already up; failing the whole publish over its thumbnail
        would report success as failure. §95's shape, applied to delivery.
        """
        if not metadata.thumbnail_path:
            return None
        thumb = Path(metadata.thumbnail_path)
        if not thumb.is_file():
            return f"thumbnail skipped: {thumb} does not exist"
        try:
            status, _, raw = self._send(
                "POST",
                f"{THUMBNAIL_URL}?videoId={video_id}",
                headers={"Content-Type": "image/jpeg"},
                body=thumb.read_bytes(),
            )
            if status not in (200, 201):
                raise self._api_error("setting the thumbnail", status, raw)
        except PublishError as error:
            logger.warning(
                "Thumbnail upload failed; the video itself is up",
                extra={"video_id": video_id, "error": str(error)[:160]},
            )
            return f"thumbnail failed: {error}"
        return None

    def _maybe_playlist(self, video_id: str, destination: str | None) -> str | None:
        playlist = destination or self._default_playlist
        if not playlist:
            return None
        body = json.dumps(
            {
                "snippet": {
                    "playlistId": playlist,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            }
        ).encode("utf-8")
        try:
            status, _, raw = self._send(
                "POST",
                PLAYLIST_URL,
                headers={"Content-Type": "application/json; charset=UTF-8"},
                body=body,
            )
            if status not in (200, 201):
                raise self._api_error("adding to the playlist", status, raw)
        except PublishError as error:
            logger.warning(
                "Playlist insert failed; the video itself is up",
                extra={"video_id": video_id, "playlist": playlist},
            )
            return f"playlist failed: {error}"
        return None

    # -- plumbing ---------------------------------------------------------

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        retry_with=None,
    ) -> tuple[int, dict[str, str], bytes]:
        """One authorised request, with backoff on the retryable statuses.

        ``retry_with`` is the resumable protocol's re-entry point: after a
        retryable failure mid-chunk the server may already hold part of it,
        so the caller supplies "ask the session for its offset" and this
        raises a private signal to loop from there rather than resend blind.
        """
        attempt = 0
        while True:
            token = self._tokens.access_token()
            status, response_headers, raw = self._transport.request(
                method,
                url,
                headers={**headers, "Authorization": f"Bearer {token}"},
                body=body,
                timeout=300,
            )
            if status == 401:
                raise PublishError(
                    "YouTube rejected the session. Sign in again.",
                    code=ErrorCode.PUBLISH_AUTH_FAILED,
                    details={"status": status},
                )
            if status == 403 and _is_quota(raw):
                raise PublishError(
                    "YouTube's daily upload quota is spent for this account. "
                    "It resets at midnight Pacific time.",
                    code=ErrorCode.PUBLISH_QUOTA_EXCEEDED,
                    details={"status": status},
                )
            if status in _RETRYABLE and attempt < self._retries:
                attempt += 1
                self._sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                if retry_with is not None:
                    # Let the upload loop re-ask the session for its offset
                    # instead of resending this exact chunk blind.
                    offset = retry_with()
                    return (308, {"range": f"bytes=0-{offset - 1}"} if offset else {}, b"")
                continue
            return status, response_headers, raw

    def _api_error(self, doing: str, status: int, raw: bytes) -> PublishError:
        payload = _json_of(raw)
        message = (
            payload.get("error", {}).get("message")
            if isinstance(payload.get("error"), dict)
            else payload.get("error")
        )
        return PublishError(
            f"YouTube refused while {doing}: {message or status}",
            code=ErrorCode.PUBLISH_FAILED,
            details={"status": status, "message": str(message or "")[:200]},
        )


def _snippet(metadata: VideoMetadata) -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "title": metadata.title or "Untitled",
        "description": metadata.description,
        "categoryId": metadata.category or GAMING_CATEGORY_ID,
    }
    if metadata.tags:
        snippet["tags"] = metadata.tags
    if metadata.language and metadata.language != "auto":
        snippet["defaultLanguage"] = metadata.language
        snippet["defaultAudioLanguage"] = metadata.language
    return {
        "snippet": snippet,
        "status": {
            "privacyStatus": metadata.visibility.value,
            "selfDeclaredMadeForKids": metadata.made_for_kids,
        },
    }


def _next_offset(headers: dict[str, str], *, fallback: int) -> int:
    """Where the server says to continue: ``Range: bytes=0-N`` means N+1."""
    header = headers.get("range", "")
    if "-" in header:
        try:
            return int(header.rsplit("-", 1)[1]) + 1
        except ValueError:
            pass
    return fallback


def _json_of(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_quota(raw: bytes) -> bool:
    payload = _json_of(raw)
    errors = payload.get("error", {})
    if not isinstance(errors, dict):
        return False
    reasons = {
        str(item.get("reason", "")) for item in errors.get("errors", []) if isinstance(item, dict)
    }
    return bool(
        reasons
        & {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded"}
    )


__all__ = ["GAMING_CATEGORY_ID", "YouTubePublisher"]
