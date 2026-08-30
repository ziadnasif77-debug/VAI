"""Distribution layer -- where a finished render goes.

This package exists so that adding a destination never reaches back into the
pipeline. It sits after QA and depends only on the render output and the
publication models; the analysis, moment, narrative, timeline and rendering
layers do not import it and do not know it exists.

Current state:

* ``local_file`` -- implemented. Copies the QA-passed render to a user-chosen
  path. This is the export.
* ``youtube`` -- implemented: device-flow OAuth and resumable chunked upload,
  with thumbnail and playlist riding along as notes rather than failure modes.
  It registers only when the person has supplied their own OAuth client in
  ``config/publishing.yaml`` -- the promise that nothing uploads on its own
  (§51) starts with there being nothing that *could*.

The construction lives in :func:`build_registry` so the API process and the
job worker assemble the exact same registry from the exact same configuration,
rather than each wiring its own copy and drifting.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.models.enums import PublishTarget
from backend.publishing.base import Publisher, PublisherRegistry, registry
from backend.publishing.google_oauth import TokenProvider, TokenStore
from backend.publishing.local_file import LocalFilePublisher
from backend.publishing.youtube import YouTubePublisher


def youtube_client(config, data_root: Path) -> tuple[str, str] | None:
    """The person's OAuth client pair, or ``None`` when they have not set one.

    The id may sit in configuration (it is public by design); the secret is
    read from its own file so the YAML never carries one. Either part missing
    means YouTube simply is not configured -- a state, not an error. Relative
    paths resolve against the data root, the same place the token lives.
    """
    settings = config.publishing.youtube
    if not settings.client_id or not settings.client_secret_file:
        return None
    secret_path = Path(settings.client_secret_file)
    if not secret_path.is_absolute():
        secret_path = Path(data_root) / secret_path
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return (settings.client_id, secret) if secret else None


def build_token_provider(config, data_root: Path) -> TokenProvider | None:
    """The OAuth session for YouTube, or ``None`` without a client."""
    client = youtube_client(config, data_root)
    if client is None:
        return None
    client_id, client_secret = client
    token_path = Path(config.publishing.youtube.token_file)
    if not token_path.is_absolute():
        token_path = Path(data_root) / token_path
    return TokenProvider(TokenStore(token_path), client_id=client_id, client_secret=client_secret)


def build_registry(config, data_root: Path) -> PublisherRegistry:
    """Every publisher this configuration supports, assembled once.

    ``local_file`` always registers -- it needs nothing. ``youtube`` registers
    exactly when a client pair exists; selecting it before then fails with the
    typed ``PUBLISH_TARGET_NOT_CONFIGURED`` the registry has always promised.

    ``publishing.enabled_targets`` is honoured here, and until now was not:
    the list read like an off switch for a destination and controlled nothing,
    so removing ``youtube`` from it left the channel one API call away.
    """
    enabled = set(config.publishing.enabled_targets)
    publishers = PublisherRegistry()
    local = config.publishing.local_file
    if PublishTarget.LOCAL_FILE in enabled:
        publishers.register(
            LocalFilePublisher(
                default_directory=(
                    Path(local.default_directory) if local.default_directory else None
                )
            )
        )
    tokens = build_token_provider(config, data_root)
    if tokens is not None and PublishTarget.YOUTUBE in enabled:
        settings = config.publishing.youtube
        publishers.register(
            YouTubePublisher(
                tokens,
                chunk_bytes=settings.upload_chunk_bytes,
                max_retries=settings.max_retries,
                default_playlist=settings.default_playlist,
            )
        )
    return publishers


__all__ = [
    "LocalFilePublisher",
    "Publisher",
    "PublisherRegistry",
    "TokenProvider",
    "TokenStore",
    "YouTubePublisher",
    "build_registry",
    "build_token_provider",
    "registry",
    "youtube_client",
]
