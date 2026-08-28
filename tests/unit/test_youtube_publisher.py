"""The YouTube publisher and its OAuth (§50, §51).

Nothing here touches a network. Every test drives the real protocol code
through a scripted transport, the same way the Ollama providers are tested —
because the code that decides what a 308 or an ``authorization_pending`` means
is exactly the code worth testing, and a mock of *that* would test the mock.

The properties under test are the ones a person depends on:

* the device flow shows a code, honours Google's polling rules, and lands a
  token in a file only this user can read;
* the upload resumes from where the *server* says it stands, not from local
  arithmetic;
* 401 says "sign in again", quota says "try tomorrow", and a failed thumbnail
  is a note on a successful publish, never a failed one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.errors import ErrorCode
from backend.core.models.enums import PublishStatus, PublishVisibility
from backend.core.models.publishing import PublishRequest, VideoMetadata
from backend.publishing.base import PublishError
from backend.publishing.google_oauth import (
    DeviceFlow,
    TokenProvider,
    TokenStore,
)
from backend.publishing.youtube import YouTubePublisher

pytestmark = pytest.mark.unit


class ScriptedTransport:
    """Answers requests in order, and records what was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30):
        self.calls.append((method, url, dict(headers or {}), body))
        if not self.answers:
            raise AssertionError(f"unexpected request: {method} {url}")
        answer = self.answers.pop(0)
        return answer if isinstance(answer, tuple) else answer()


def _json(payload: dict, status: int = 200, headers: dict | None = None):
    return (
        status,
        {k.lower(): v for k, v in (headers or {}).items()},
        json.dumps(payload).encode(),
    )


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "token.json")


def _provider(store: TokenStore, transport) -> TokenProvider:
    return TokenProvider(store, client_id="cid", client_secret="sec", transport=transport)


# -- the device flow ---------------------------------------------------------


class TestDeviceFlow:
    def _flow(self, transport) -> DeviceFlow:
        flow = DeviceFlow(client_id="cid", client_secret="sec", transport=transport)
        # The interval gate is real time; the tests step over it explicitly.
        return flow

    def test_begin_hands_back_what_the_person_must_see(self) -> None:
        transport = ScriptedTransport(
            _json(
                {
                    "device_code": "dev",
                    "user_code": "ABCD-EFGH",
                    "verification_url": "https://www.google.com/device",
                    "interval": 5,
                    "expires_in": 1800,
                }
            )
        )
        grant = self._flow(transport).begin()

        assert grant.user_code == "ABCD-EFGH"
        public = grant.public()
        # The device code is the secret half; it never reaches a UI.
        assert "device_code" not in public
        assert public["verification_url"].startswith("https://www.google.com")

    def test_polling_honours_pending_then_lands_the_token(self) -> None:
        transport = ScriptedTransport(
            _json({"device_code": "dev", "user_code": "X", "interval": 0, "expires_in": 600}),
            _json({"error": "authorization_pending"}, status=428),
            _json({"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
        )
        flow = self._flow(transport)
        grant = flow.begin()
        flow._next_poll_at = 0.0

        assert flow.poll(grant) is None  # pending
        flow._next_poll_at = 0.0
        token = flow.poll(grant)

        assert token is not None and token["refresh_token"] == "rt"
        assert token["expires_at"] > 0

    def test_slow_down_widens_the_interval_without_a_request_storm(self) -> None:
        transport = ScriptedTransport(
            _json({"device_code": "dev", "user_code": "X", "interval": 0, "expires_in": 600}),
            _json({"error": "slow_down"}, status=403),
        )
        flow = self._flow(transport)
        grant = flow.begin()
        flow._next_poll_at = 0.0

        assert flow.poll(grant) is None
        # The very next poll is inside the widened window: no HTTP call at all.
        assert flow.poll(grant) is None
        assert len(transport.calls) == 2  # begin + one token poll

    def test_denied_is_terminal_and_says_so(self) -> None:
        transport = ScriptedTransport(
            _json({"device_code": "dev", "user_code": "X", "interval": 0, "expires_in": 600}),
            _json({"error": "access_denied"}, status=403),
        )
        flow = self._flow(transport)
        grant = flow.begin()
        flow._next_poll_at = 0.0

        with pytest.raises(PublishError) as caught:
            flow.poll(grant)
        assert caught.value.code is ErrorCode.PUBLISH_AUTH_FAILED

    def test_an_expired_code_asks_to_start_again(self) -> None:
        transport = ScriptedTransport(
            _json({"device_code": "dev", "user_code": "X", "interval": 0, "expires_in": 0}),
        )
        flow = self._flow(transport)
        grant = flow.begin()

        with pytest.raises(PublishError, match="expired"):
            flow.poll(grant)


# -- the token lifecycle ------------------------------------------------------


class TestTokenProvider:
    def test_a_stored_live_token_is_used_without_a_refresh(self, store) -> None:
        store.save({"access_token": "at", "refresh_token": "rt", "expires_at": 9e12})
        transport = ScriptedTransport()  # any request would fail the test

        assert _provider(store, transport).access_token() == "at"

    def test_an_expiring_token_refreshes_and_keeps_the_refresh_token(self, store) -> None:
        store.save({"access_token": "old", "refresh_token": "rt", "expires_at": 0})
        transport = ScriptedTransport(_json({"access_token": "fresh", "expires_in": 3600}))

        assert _provider(store, transport).access_token() == "fresh"
        saved = store.load()
        # Google only returns refresh_token on the first grant; losing it here
        # would sign the person out an hour after every sign-in.
        assert saved["refresh_token"] == "rt"

    def test_a_revoked_grant_clears_the_store(self, store) -> None:
        store.save({"access_token": "old", "refresh_token": "rt", "expires_at": 0})
        transport = ScriptedTransport(_json({"error": "invalid_grant"}, status=400))

        with pytest.raises(PublishError):
            _provider(store, transport).access_token()
        assert store.load() is None

    def test_no_token_means_sign_in_first(self, store) -> None:
        with pytest.raises(PublishError, match="Sign in"):
            _provider(store, ScriptedTransport()).access_token()

    def test_is_authorised_is_the_configured_check(self, store) -> None:
        provider = _provider(store, ScriptedTransport())
        assert not provider.is_authorised()
        store.save({"access_token": "a", "refresh_token": "rt", "expires_at": 9e12})
        assert provider.is_authorised()


# -- the upload ---------------------------------------------------------------


def _request(**metadata) -> PublishRequest:
    return PublishRequest(
        project_id="proj-x",
        render_id="rnd-x",
        target="youtube",
        metadata=VideoMetadata(
            title=metadata.pop("title", "My video"),
            visibility=metadata.pop("visibility", PublishVisibility.PRIVATE),
            **metadata,
        ),
    )


@pytest.fixture
def render(tmp_path: Path) -> Path:
    path = tmp_path / "final.mp4"
    path.write_bytes(b"x" * 700_000)  # three chunks at the floor chunk size
    return path


def _publisher(store, transport, **kwargs) -> YouTubePublisher:
    store.save({"access_token": "at", "refresh_token": "rt", "expires_at": 9e12})
    tokens = TokenProvider(store, client_id="c", client_secret="s", transport=transport)
    return YouTubePublisher(
        tokens, chunk_bytes=262_144, transport=transport, sleep=lambda _s: None, **kwargs
    )


class TestUpload:
    def test_the_happy_path_is_init_chunks_and_an_id(self, store, render) -> None:
        transport = ScriptedTransport(
            _json({}, status=200, headers={"Location": "https://u/session"}),
            (308, {"range": "bytes=0-262143"}, b""),
            (308, {"range": "bytes=0-524287"}, b""),
            _json({"id": "vid123"}, status=200),
        )
        result = _publisher(store, transport).publish(_request(), render)

        assert result.status is PublishStatus.COMPLETED
        assert result.external_url == "https://youtu.be/vid123"
        init = transport.calls[0]
        assert "uploadType=resumable" in init[1]
        body = json.loads(init[3])
        assert body["snippet"]["categoryId"] == "20"  # Gaming, the one default
        assert body["status"]["privacyStatus"] == "private"
        # Every chunk went out with the range arithmetic intact.
        first_chunk = transport.calls[1]
        assert first_chunk[2]["Content-Range"] == "bytes 0-262143/700000"

    def test_the_server_owns_the_offset(self, store, render) -> None:
        # The 308 answers "I only have 100000 bytes" and the next chunk must
        # start there -- not at the end of what was sent.
        transport = ScriptedTransport(
            _json({}, status=200, headers={"Location": "https://u/session"}),
            (308, {"range": "bytes=0-99999"}, b""),
            (308, {"range": "bytes=0-362143"}, b""),
            (308, {"range": "bytes=0-624287"}, b""),
            _json({"id": "vid"}, status=200),
        )
        _publisher(store, transport).publish(_request(), render)

        second = transport.calls[2]
        assert second[2]["Content-Range"].startswith("bytes 100000-")

    def test_a_401_says_sign_in_again(self, store, render) -> None:
        transport = ScriptedTransport(_json({}, status=401))
        with pytest.raises(PublishError) as caught:
            _publisher(store, transport).publish(_request(), render)
        assert caught.value.code is ErrorCode.PUBLISH_AUTH_FAILED

    def test_quota_exhaustion_names_the_reset(self, store, render) -> None:
        transport = ScriptedTransport(
            _json({"error": {"errors": [{"reason": "uploadLimitExceeded"}]}}, status=403)
        )
        with pytest.raises(PublishError) as caught:
            _publisher(store, transport).publish(_request(), render)
        assert caught.value.code is ErrorCode.PUBLISH_QUOTA_EXCEEDED
        assert "midnight Pacific" in str(caught.value)

    def test_a_missing_render_fails_before_any_request(self, store, tmp_path) -> None:
        transport = ScriptedTransport()
        with pytest.raises(PublishError):
            _publisher(store, transport).publish(_request(), tmp_path / "gone.mp4")
        assert transport.calls == []

    def test_a_failed_thumbnail_is_a_note_not_a_failure(self, store, render, tmp_path) -> None:
        thumb = tmp_path / "thumb.jpg"
        thumb.write_bytes(b"jpg")
        transport = ScriptedTransport(
            _json({}, status=200, headers={"Location": "https://u/session"}),
            _json({"id": "vid"}, status=200),
            _json({"error": {"message": "too big"}}, status=400),  # thumbnails.set
        )
        result = _publisher(store, transport).publish(_request(thumbnail_path=str(thumb)), render)

        assert result.status is PublishStatus.COMPLETED
        assert any("thumbnail" in note for note in result.notes)

    def test_is_configured_reflects_the_token(self, store) -> None:
        transport = ScriptedTransport()
        tokens = TokenProvider(store, client_id="c", client_secret="s", transport=transport)
        publisher = YouTubePublisher(tokens, transport=transport)

        assert not publisher.is_configured()
        store.save({"access_token": "a", "refresh_token": "rt", "expires_at": 9e12})
        assert publisher.is_configured()


class TestCategoryTranslation:
    def test_a_named_category_becomes_the_gaming_id(self) -> None:
        from backend.core.models.publishing import VideoMetadata
        from backend.publishing.youtube import GAMING_CATEGORY_ID, _snippet

        snippet = _snippet(VideoMetadata(title="x", category="Gaming"))["snippet"]

        assert snippet["categoryId"] == GAMING_CATEGORY_ID

    def test_a_numeric_category_passes_through(self) -> None:
        from backend.core.models.publishing import VideoMetadata
        from backend.publishing.youtube import _snippet

        assert _snippet(VideoMetadata(title="x", category="22"))["snippet"]["categoryId"] == "22"
