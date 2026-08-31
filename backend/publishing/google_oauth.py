"""Google device-flow OAuth for the YouTube publisher (§50, §51).

The flow this implements is "OAuth 2.0 for TV and Limited-Input Devices",
chosen over the loopback-redirect flow for one practical reason: it needs no
local web server and no browser hand-off machinery. The app shows a short code,
the person types it at google.com/device on any device they like, and the app
polls until Google says yes. That shape survives every environment this app
runs in — a headless render box included.

Three rules from the configuration file are honoured here rather than merely
quoted:

* **Credentials never live in configuration.** The OAuth *client* pair is
  supplied by the person at setup (Google treats installed-app client secrets
  as non-confidential, but they are still theirs, not this repository's), and
  the *token* lives in one JSON file under the data root with owner-only
  permissions where the OS supports them.
* **Nothing uploads on its own.** This module only ever runs inside an
  explicit publish or an explicit auth request; there is no ambient refresh
  loop.
* **Every HTTP exchange goes through an injectable transport**, so the tests
  exercise the whole protocol — pending, slow_down, expiry, refresh — without
  a network, the same way the Ollama providers are tested.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from backend.core.errors import ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.publishing.base import PublishError

logger = get_logger("publishing.google_oauth", LogChannel.RENDERING)

DEVICE_CODE_URL: Final[str] = "https://oauth2.googleapis.com/device/code"
TOKEN_URL: Final[str] = "https://oauth2.googleapis.com/token"

#: The full YouTube scope, not merely ``youtube.upload``: setting a thumbnail
#: and adding to a playlist need it, and asking a person to re-authorise a
#: second time for a thumbnail is worse than asking for the right thing once.
UPLOAD_SCOPE: Final[str] = "https://www.googleapis.com/auth/youtube"

#: Read-only access to the channel's own analytics (V2-P9). Separate constant
#: because a grant made before this phase does not carry it: the token store
#: keeps what Google actually granted, so the system can say which of the two
#: it holds instead of failing at the first report with a bare 403.
ANALYTICS_SCOPE: Final[str] = "https://www.googleapis.com/auth/yt-analytics.readonly"

#: What the **device flow** asks for, and it is the upload scope alone.
#:
#: Not a preference. Google's device flow -- the one this file implements,
#: because a desktop app with no browser redirect is what it was built for --
#: supports a limited list of scopes, and the YouTube Analytics scopes are not
#: on it. Measured against Google's own endpoint with this client id:
#:
#:     youtube                  -> accepted
#:     yt-analytics.readonly    -> 400 invalid_scope
#:     both together            -> 400 invalid_scope
#:
#: V2-P9 set this to both and shipped it. The effect was worse than the thing
#: it was trying to fix: every new sign-in failed with invalid_scope, so the
#: attempt to gain a read permission had taken away the ability to authorise an
#: upload. Analytics needs a different flow, and asking for it here cannot work
#: however the request is phrased.
SCOPE: Final[str] = UPLOAD_SCOPE

_GRANT_DEVICE: Final[str] = "urn:ietf:params:oauth:grant-type:device_code"
_HTTP_TIMEOUT_SECONDS: Final[int] = 30

#: What a **browser** sign-in asks for: everything this project uses.
#:
#: The loopback flow -- an installed app receiving the code on a local port --
#: has no scope restrictions, so one sign-in through it covers uploading and
#: reading both, and replaces the narrower device grant entirely. Measured
#: against Google's own consent endpoint with this client id: accepted.
FULL_SCOPE: Final[str] = f"{UPLOAD_SCOPE} {ANALYTICS_SCOPE}"

AUTH_URL: Final[str] = "https://accounts.google.com/o/oauth2/v2/auth"

#: Refresh when this close to expiry. Google access tokens live ~3600 s; a
#: chunked upload of a large file can outlive one, so the uploader asks for a
#: fresh token per chunk and this margin makes that ask cheap.
_EXPIRY_MARGIN_SECONDS: Final[int] = 120


class Transport(Protocol):
    """One HTTP exchange. The seam every test stands on."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: int = _HTTP_TIMEOUT_SECONDS,
    ) -> tuple[int, dict[str, str], bytes]: ...


class UrllibTransport:
    """The production transport: stdlib only, like every other provider here."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: int = _HTTP_TIMEOUT_SECONDS,
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return (
                    response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as error:
            # An HTTP error *is* a response; the protocol layers above decide
            # what a 403 or a 428 means. Raising here would turn "pending
            # approval" into an exception.
            return (
                error.code,
                {k.lower(): v for k, v in error.headers.items()},
                error.read(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise PublishError(
                f"Cannot reach {urllib.parse.urlsplit(url).netloc}: {error}",
                code=ErrorCode.PUBLISH_FAILED,
                details={"url": url},
                cause=error,
            ) from error


def _form(payload: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(payload).encode("utf-8")


def _json_of(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True, slots=True)
class DeviceGrant:
    """What Google hands back when a device flow starts: show these two."""

    verification_url: str
    user_code: str
    device_code: str
    interval_seconds: int
    expires_at: float

    def public(self) -> dict[str, Any]:
        """The part a UI may show. The device code itself stays server-side."""
        return {
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
        }


class TokenStore:
    """One JSON file holding the refresh token, owner-readable only.

    A file rather than the OS keyring, stated plainly: the keyring on Windows
    caps secret length and needs a per-user service name scheme, and this app
    already keeps everything project-local by §84's design. The file lives
    under the data root, never inside the repository, and the seam is this
    class — a keyring-backed store can replace it without touching a caller.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any] | None:
        try:
            return _json_of(self._path.read_bytes()) or None
        except FileNotFoundError:
            return None
        except OSError as error:
            logger.warning(
                "Could not read the stored YouTube token",
                extra={"path": str(self._path), "error": str(error)[:120]},
            )
            return None

    def save(self, token: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(token, indent=2), encoding="utf-8")
        # Windows ACLs do not speak chmod; the file still sits under the
        # user's own data root, which is the boundary that matters there.
        with contextlib.suppress(OSError):
            self._path.chmod(0o600)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class DeviceFlow:
    """The device grant, one poll at a time.

    Deliberately not a blocking loop: approval takes as long as a person takes,
    and holding an HTTP request open for minutes is how a UI freezes. The API
    calls :meth:`begin` once, shows the code, then calls :meth:`poll` on its
    own schedule; each call makes exactly one token request, honouring
    Google's interval and ``slow_down``.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: Transport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport or UrllibTransport()
        self._next_poll_at = 0.0

    def begin(self) -> DeviceGrant:
        status, _, body = self._transport.request(
            "POST",
            DEVICE_CODE_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=_form({"client_id": self._client_id, "scope": SCOPE}),
        )
        payload = _json_of(body)
        if status != 200 or "device_code" not in payload:
            raise PublishError(
                "Google refused to start device authorisation: "
                + str(payload.get("error", status)),
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"status": status, "error": payload.get("error")},
            )
        interval = int(payload.get("interval", 5))
        self._next_poll_at = time.time() + interval
        return DeviceGrant(
            verification_url=str(
                payload.get("verification_url")
                or payload.get("verification_uri")
                or "https://www.google.com/device"
            ),
            user_code=str(payload["user_code"]),
            device_code=str(payload["device_code"]),
            interval_seconds=interval,
            expires_at=time.time() + int(payload.get("expires_in", 1800)),
        )

    def poll(self, grant: DeviceGrant) -> dict[str, Any] | None:
        """One attempt. A token when approved, ``None`` while still pending.

        Raises on the terminal answers — denied, expired — because those are
        outcomes a caller must surface, not states to keep polling in.
        """
        if time.time() >= grant.expires_at:
            raise PublishError(
                "The sign-in code expired before it was entered. Start again.",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"reason": "expired_token"},
            )
        if time.time() < self._next_poll_at:
            # Between allowed polls. Not an error and not a request: Google
            # answers early polls with slow_down and enough of those get a
            # client blocked.
            return None

        status, _, body = self._transport.request(
            "POST",
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=_form(
                {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "device_code": grant.device_code,
                    "grant_type": _GRANT_DEVICE,
                }
            ),
        )
        payload = _json_of(body)
        error = str(payload.get("error", ""))

        if status == 200 and "access_token" in payload:
            return _with_expiry(payload)
        if error == "authorization_pending":
            self._next_poll_at = time.time() + grant.interval_seconds
            return None
        if error == "slow_down":
            self._next_poll_at = time.time() + grant.interval_seconds + 5
            return None
        if error == "access_denied":
            raise PublishError(
                "The Google account declined the request.",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"reason": error},
            )
        raise PublishError(
            f"Device authorisation failed: {error or status}",
            code=ErrorCode.PUBLISH_AUTH_FAILED,
            details={"status": status, "reason": error},
        )


class TokenProvider:
    """Hands out a live access token, refreshing through the stored one."""

    def __init__(
        self,
        store: TokenStore,
        *,
        client_id: str,
        client_secret: str,
        transport: Transport | None = None,
    ) -> None:
        self._store = store
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport or UrllibTransport()

    @property
    def store(self) -> TokenStore:
        return self._store

    def is_authorised(self) -> bool:
        token = self._store.load()
        return bool(token and token.get("refresh_token"))

    def granted_scopes(self) -> frozenset[str]:
        """What Google actually granted, which is not what was asked for.

        A refresh token keeps the scopes it was issued with. Widening
        :data:`SCOPE` in the code does not widen a grant already on disk, and
        the honest thing is to be able to say so rather than to discover it as
        a 403 in the middle of a report.
        """
        token = self._store.load() or {}
        return frozenset(str(token.get("scope") or "").split())

    def may_read_analytics(self) -> bool:
        return ANALYTICS_SCOPE in self.granted_scopes()

    def access_token(self) -> str:
        token = self._store.load()
        if not token or not token.get("refresh_token"):
            raise PublishError(
                "YouTube is not connected. Sign in first.",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"reason": "no_token"},
            )
        if float(token.get("expires_at", 0)) - time.time() > _EXPIRY_MARGIN_SECONDS:
            return str(token["access_token"])
        return self._refresh(token)

    def _refresh(self, token: dict[str, Any]) -> str:
        status, _, body = self._transport.request(
            "POST",
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=_form(
                {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": str(token["refresh_token"]),
                    "grant_type": "refresh_token",
                }
            ),
        )
        payload = _json_of(body)
        if status != 200 or "access_token" not in payload:
            reason = str(payload.get("error", status))
            if reason == "invalid_grant":
                # The person revoked access, or Google expired the grant.
                # Keeping a dead token means every later publish fails with a
                # worse message than "sign in again".
                self._store.clear()
            raise PublishError(
                f"Could not refresh the YouTube session ({reason}). Sign in again.",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"status": status, "reason": reason},
            )
        fresh = _with_expiry(payload)
        # Google only returns refresh_token on the first grant; carry it over.
        fresh.setdefault("refresh_token", token["refresh_token"])
        self._store.save(fresh)
        return str(fresh["access_token"])


class LoopbackFlow:
    """Sign in through the browser, receiving the code on a local port.

    The device flow this file was built around is right for a machine with no
    browser, and Google restricts it to a scope list that does not include
    YouTube Analytics -- measured, not assumed: `yt-analytics.readonly` comes
    back `invalid_scope` from the device endpoint, alone or in company.

    This is the installed-application flow instead. The person approves in
    their own browser, Google redirects to `http://localhost:<port>/` with a
    one-time code, and this exchanges it. Nothing here ever sees a password,
    and the local server answers exactly one request before it stops.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        scope: str = FULL_SCOPE,
        transport: Transport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._transport = transport or UrllibTransport()

    def authorise(
        self, *, timeout_seconds: int = 300, on_url: Any = None, port: int = 0
    ) -> dict[str, Any]:
        """Run the whole flow and return the token, or raise saying why.

        ``on_url`` is called with the consent address **before** the wait
        begins. Not a nicety: the first version printed it only when the
        flow ended, so a person whose browser did not open sat looking at
        nothing for fifteen minutes and then received the address they
        had needed at the start.

        ``port`` fixes the local port, and therefore the consent address. A
        port of 0 takes whatever is free, which means a new address every
        attempt -- and an address that changes between attempts is one a person
        cannot come back to. Loopback redirects accept any port for an
        installed-app client, so pinning one costs nothing.
        """
        import http.server
        import socket
        import threading
        import urllib.parse
        import webbrowser

        received: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = urllib.parse.urlparse(self.path).query
                received.update(
                    {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
                )
                body = (
                    b"<html><body style='font:16px system-ui;padding:3rem'>"
                    b"<h2>Signed in.</h2><p>You can close this tab and go back "
                    b"to the terminal.</p></body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                """Silence. The caller reports; a stdlib access log does not."""

        if not port:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

        try:
            server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        except OSError as error:
            raise PublishError(
                f"Port {port} is already in use, so the sign-in cannot listen "
                f"for the code. Close whatever holds it, or pass another port.",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"port": port},
                recoverable=True,
            ) from error
        server.timeout = timeout_seconds
        redirect = f"http://localhost:{port}/"
        query = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": self._scope,
                "access_type": "offline",
                # Without this, Google reuses the earlier consent and returns
                # no refresh token -- and a grant with no refresh token is one
                # that stops working in an hour.
                "prompt": "consent",
            }
        )
        url = f"{AUTH_URL}?{query}"

        self.url = url
        if on_url is not None:
            with contextlib.suppress(Exception):
                on_url(url)

        def open_browser() -> None:
            with contextlib.suppress(Exception):
                webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()
        logger.info("Waiting for a browser sign-in", extra={"port": port})
        server.handle_request()
        server.server_close()

        if "error" in received:
            raise PublishError(
                f"The sign-in was refused: {received['error']}",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"error": received["error"]},
                recoverable=False,
            )
        code = received.get("code")
        if not code:
            raise PublishError(
                "No authorisation code arrived before the wait ran out.",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"reason": "timeout"},
                recoverable=True,
            )
        return self._exchange(code, redirect)

    def _exchange(self, code: str, redirect: str) -> dict[str, Any]:
        status, _headers, body = self._transport.request(
            "POST",
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=_form(
                {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect,
                }
            ),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        payload = _json_of(body)
        if status != 200 or "access_token" not in payload:
            raise PublishError(
                f"Google refused to exchange the code: "
                f"{payload.get('error', status)}",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"status": status, "error": payload.get("error")},
                recoverable=False,
            )
        if not payload.get("refresh_token"):
            raise PublishError(
                "Google returned no refresh token, so this sign-in would stop "
                "working in an hour.",
                code=ErrorCode.PUBLISH_AUTH_FAILED,
                details={"reason": "no_refresh_token"},
                recoverable=False,
            )
        return _with_expiry(payload)


def _with_expiry(payload: dict[str, Any]) -> dict[str, Any]:
    token = dict(payload)
    token["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
    return token


__all__ = [
    "ANALYTICS_SCOPE",
    "AUTH_URL",
    "FULL_SCOPE",
    "SCOPE",
    "UPLOAD_SCOPE",
    "DeviceFlow",
    "DeviceGrant",
    "LoopbackFlow",
    "TokenProvider",
    "TokenStore",
    "Transport",
    "UrllibTransport",
]
