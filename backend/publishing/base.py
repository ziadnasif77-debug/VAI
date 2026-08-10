"""Publisher interface and registry.

A publisher takes a rendered file plus :class:`VideoMetadata` and delivers it
somewhere. That is the whole contract. Everything a specific destination needs
beyond it -- OAuth, resumable upload, quota handling, retry semantics -- belongs
inside that publisher, not in the pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from backend.core.errors import ErrorCode, GamingEditorError
from backend.core.models.enums import PublishTarget
from backend.core.models.publishing import PublishRequest, PublishResult


class PublishError(GamingEditorError):
    """A publication attempt failed."""

    default_code = ErrorCode.PUBLISH_FAILED


@runtime_checkable
class Publisher(Protocol):
    """Delivers a rendered video to one destination."""

    target: PublishTarget

    def is_configured(self) -> bool:
        """Whether this publisher has everything it needs to run.

        Checked before a publish job starts so a missing credential surfaces as
        a clear error in the UI instead of a failure halfway through an upload.
        """
        ...

    def publish(self, request: PublishRequest, render_path: Path) -> PublishResult:
        """Deliver ``render_path`` and return the outcome.

        Implementations must not mutate ``render_path`` and must raise
        :class:`PublishError` (never a bare exception) on failure.
        """
        ...


class PublisherRegistry:
    """Maps a :class:`PublishTarget` to its publisher.

    Registration is explicit. A target with no registered publisher fails with
    ``PUBLISH_TARGET_NOT_CONFIGURED``, which is exactly what should happen if
    someone selects YouTube before that publisher exists.
    """

    def __init__(self) -> None:
        self._publishers: dict[PublishTarget, Publisher] = {}

    def register(self, publisher: Publisher, *, replace: bool = False) -> None:
        """Register ``publisher`` for its declared target."""
        target = publisher.target
        if target in self._publishers and not replace:
            raise ValueError(
                f"A publisher is already registered for {target.value!r}. "
                "Pass replace=True to override it."
            )
        self._publishers[target] = publisher

    def get(self, target: PublishTarget) -> Publisher:
        """Return the publisher for ``target``.

        Raises:
            PublishError: when nothing is registered for the target.
        """
        try:
            return self._publishers[target]
        except KeyError as exc:
            raise PublishError(
                f"No publisher is available for target {target.value!r}. "
                "It is declared but not implemented in this build.",
                code=ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED,
                details={"target": target.value, "available": self.available_names()},
                recoverable=False,
            ) from exc

    def available(self) -> tuple[PublishTarget, ...]:
        """Targets that have a registered publisher."""
        return tuple(self._publishers)

    def available_names(self) -> list[str]:
        return [target.value for target in self._publishers]

    def is_available(self, target: PublishTarget) -> bool:
        return target in self._publishers


#: Process-wide registry. Application startup registers the publishers that the
#: current build and configuration support.
registry = PublisherRegistry()


__all__ = ["PublishError", "Publisher", "PublisherRegistry", "registry"]
