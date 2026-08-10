"""Vision providers (SPEC sections 13, 15, 95).

The factory maps ``models.vision.provider`` onto an implementation and never
substitutes one for another. A machine without the model degrades through the
§95 chain — OCR, audio, scene detection, the game profile — rather than
receiving invented descriptions of frames nobody looked at.
"""

from __future__ import annotations

from ai.providers.base import VisionProvider
from ai.vision.fake_provider import FakeVisionProvider
from ai.vision.ollama_provider import OllamaVisionProvider
from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, ModelError

#: Provider names accepted in ``config/models.yaml``.
VISION_PROVIDERS = ("ollama", "fake")


def create_vision_provider(config: AppConfig, *, game: str = "auto") -> VisionProvider:
    """Build the configured vision provider.

    Args:
        game: the project's game profile id, passed into the prompt. ``"auto"``
            and ``"generic"`` become a neutral phrase, because telling the
            model the game is called "auto" is worse than telling it nothing
            (§23: an unknown game still has to work).

    Raises:
        ModelError: the configured provider name has no implementation.
    """
    vision = config.models.vision
    if vision.provider == "ollama":
        return OllamaVisionProvider(vision, gpu=config.gpu, game=_game_phrase(game))
    if vision.provider == "fake":
        return FakeVisionProvider()
    raise ModelError(
        f"Unknown vision provider {vision.provider!r}.",
        code=ErrorCode.PROVIDER_NOT_REGISTERED,
        details={"provider": vision.provider, "supported": list(VISION_PROVIDERS)},
        recoverable=False,
    )


def _game_phrase(game: str) -> str:
    normalised = (game or "").strip().lower()
    if normalised in {"", "auto", "generic", "unknown"}:
        return "an unidentified game"
    return game


__all__ = [
    "VISION_PROVIDERS",
    "FakeVisionProvider",
    "OllamaVisionProvider",
    "create_vision_provider",
]
