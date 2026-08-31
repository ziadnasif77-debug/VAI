"""What the audience did with a finished video (V2-P9).

The first thing in this system that reads a fact it did not decide. Every other
record here is the machine's own choice written down; these are what people did
with the result, and they are the only evidence that any of those choices were
good ones.

Read-only, on the owner's own channel, and asked for explicitly -- there is no
ambient polling. The YouTube Analytics API answers two different questions and
this module asks both:

* the totals for a window (views, watch time, the average share of the video a
  viewer saw), which say whether the video worked;
* ``audienceWatchRatio`` against ``elapsedVideoTimeRatio``, which says *where*
  it worked and where it did not -- the curve P10 will eventually be allowed to
  respond to, and which this phase only stores.

Nothing here predicts. A retention curve is a measurement of one video that has
already been watched; treating it as a forecast for the next one is the claim
this project has ruled out until there is enough data to earn it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlencode

from backend.core.errors import ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.publishing.base import PublishError
from backend.publishing.google_oauth import ANALYTICS_SCOPE, Transport, UrllibTransport

logger = get_logger("analytics.youtube", LogChannel.RENDERING)

REPORTS_URL: Final[str] = "https://youtubeanalytics.googleapis.com/v2/reports"

#: The totals worth keeping. Named rather than "everything the API has": a
#: metric nobody reads is the orphaned configuration key of the analytics
#: layer, and the raw response is stored anyway for the ones added later.
TOTALS: Final[tuple[str, ...]] = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
)

#: The curve. ``relativeRetentionPerformance`` needs enough traffic before
#: YouTube will compare a video with others of its length, so it is requested
#: separately and its absence is normal rather than an error.
CURVE_METRIC: Final[str] = "audienceWatchRatio"
CURVE_DIMENSION: Final[str] = "elapsedVideoTimeRatio"

_TIMEOUT_SECONDS: Final[int] = 60


@dataclass(frozen=True, slots=True)
class Totals:
    """One video's numbers over one window."""

    video_id: str
    start_date: str
    end_date: str
    metrics: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> float | None:
        value = self.metrics.get(name)
        return None if value is None else float(value)


@dataclass(frozen=True, slots=True)
class RetentionPoint:
    """One sample of the curve: a fraction of the video, and who was still there."""

    elapsed_ratio: float
    audience_watch_ratio: float
    relative_performance: float | None = None


class YouTubeAnalytics:
    """Reads the owner's own channel. Never writes anything anywhere."""

    def __init__(self, tokens: Any, *, transport: Transport | None = None) -> None:
        self._tokens = tokens
        self._transport = transport or UrllibTransport()

    # -- what the caller should check first --------------------------------

    def is_ready(self) -> bool:
        """Whether a report can be asked for at all."""
        return bool(
            getattr(self._tokens, "is_authorised", lambda: False)()
            and getattr(self._tokens, "may_read_analytics", lambda: False)()
        )

    def why_not(self) -> str | None:
        """One sentence a person can act on, or None when it is ready."""
        if not getattr(self._tokens, "is_authorised", lambda: False)():
            return "YouTube is not connected: no analytics until someone signs in."
        if not getattr(self._tokens, "may_read_analytics", lambda: False)():
            return (
                "The stored authorisation predates the analytics scope. Sign in "
                f"again to grant {ANALYTICS_SCOPE}; uploading keeps working "
                "meanwhile, and nothing is read until it is granted."
            )
        return None

    # -- the two questions --------------------------------------------------

    def totals(self, video_id: str, *, start_date: str, end_date: str) -> Totals:
        """The window's numbers for one video."""
        payload = self._report(
            {
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": ",".join(TOTALS),
                "filters": f"video=={video_id}",
            }
        )
        names = [str(column.get("name")) for column in payload.get("columnHeaders", [])]
        rows = payload.get("rows") or []
        metrics: dict[str, float] = {}
        if rows:
            for name, value in zip(names, rows[0], strict=False):
                if isinstance(value, (int, float)):
                    metrics[name] = float(value)
        else:
            # A published video with no rows is not an error: a video nobody
            # has watched yet reports nothing, and that is itself the answer.
            logger.info(
                "The window holds no data for this video yet",
                extra={"video_id": video_id, "start": start_date, "end": end_date},
            )
        return Totals(
            video_id=video_id,
            start_date=start_date,
            end_date=end_date,
            metrics=metrics,
            raw=payload,
        )

    def retention(
        self, video_id: str, *, start_date: str, end_date: str
    ) -> list[RetentionPoint]:
        """The curve, ordered from the first frame to the last."""
        payload = self._report(
            {
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": CURVE_METRIC,
                "dimensions": CURVE_DIMENSION,
                "filters": f"video=={video_id}",
                "sort": CURVE_DIMENSION,
            }
        )
        names = [str(column.get("name")) for column in payload.get("columnHeaders", [])]
        try:
            at = names.index(CURVE_DIMENSION)
            watched = names.index(CURVE_METRIC)
        except ValueError:
            logger.warning(
                "The retention report came back without its own columns",
                extra={"video_id": video_id, "columns": names},
            )
            return []
        points: list[RetentionPoint] = []
        for row in payload.get("rows") or []:
            try:
                points.append(
                    RetentionPoint(
                        elapsed_ratio=min(1.0, max(0.0, float(row[at]))),
                        audience_watch_ratio=float(row[watched]),
                    )
                )
            except (IndexError, TypeError, ValueError):
                continue
        points.sort(key=lambda point: point.elapsed_ratio)
        return points

    # -- one authorised GET -------------------------------------------------

    @staticmethod
    def _refusal(status: int, raw: bytes) -> str:
        """Say what Google actually refused, not what seems likely.

        The first version blamed the scope for every 401 and 403. When the
        scope was finally granted and the call still failed, the message sent
        the reader back to the sign-in they had just completed correctly --
        the real answer, `accessNotConfigured`, was in the body all along and
        the message was hiding it behind a guess.
        """
        try:
            error = (json.loads(raw.decode("utf-8")) or {}).get("error", {})
        except Exception:
            error = {}
        reason = str((error.get("errors") or [{}])[0].get("reason") or "")
        message = str(error.get("message") or "").strip()

        if reason == "accessNotConfigured":
            return (
                "The YouTube Analytics API is not enabled for this Google "
                "Cloud project. That is separate from the sign-in, which is "
                "fine. Enable it and retry:\n"
                "  https://console.cloud.google.com/apis/library/"
                "youtubeanalytics.googleapis.com"
            )
        if reason in ("insufficientPermissions", "forbidden") or status == 401:
            return (
                "YouTube refused the request as unauthorised. The stored "
                "sign-in may predate the analytics scope; "
                "run scripts/youtube_auth.py to see what it covers."
            )
        return message or f"YouTube Analytics refused the request ({status})."

    def _report(self, query: dict[str, str]) -> dict[str, Any]:
        refusal = self.why_not()
        if refusal:
            raise PublishError(
                refusal,
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"reason": "analytics_scope_missing"},
            )
        token = self._tokens.access_token()
        status, _headers, raw = self._transport.request(
            "GET",
            f"{REPORTS_URL}?{urlencode(query)}",
            headers={"Authorization": f"Bearer {token}"},
            body=None,
            timeout=_TIMEOUT_SECONDS,
        )
        if status == 200:
            try:
                return json.loads(raw.decode("utf-8") or "{}")
            except ValueError as error:
                raise PublishError(
                    "The analytics report was not JSON.",
                    code=ErrorCode.PUBLISH_FAILED,
                    details={"status": status},
                ) from error
        if status in (401, 403):
            raise PublishError(
                self._refusal(status, raw),
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"status": status, "body": raw[:400].decode("utf-8", "replace")},
            )
        raise PublishError(
            f"YouTube Analytics answered {status}.",
            code=ErrorCode.PUBLISH_FAILED,
            details={"status": status, "body": raw[:400].decode("utf-8", "replace")},
        )


__all__ = [
    "CURVE_DIMENSION",
    "CURVE_METRIC",
    "REPORTS_URL",
    "TOTALS",
    "RetentionPoint",
    "Totals",
    "YouTubeAnalytics",
]
