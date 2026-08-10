"""Speech-to-text providers (SPEC sections 13, 14, 95).

The factory maps ``models.speech.provider`` onto an implementation. It never
substitutes one provider for another: a machine without Whisper gets a typed
error and degrades through the §95 fallback chain, because a transcript is
evidence that ends up as on-screen captions, and inventing it would be worse
than not having it.
"""

from __future__ import annotations

from pathlib import Path

from ai.providers.base import SpeechProvider
from ai.speech.fake_provider import FakeSpeechProvider
from ai.speech.faster_whisper_provider import FasterWhisperProvider
from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, ModelError

#: Provider names accepted in ``config/models.yaml``.
SPEECH_PROVIDERS = ("faster_whisper", "fake")


def create_speech_provider(
    config: AppConfig, *, model_root: Path | None = None
) -> SpeechProvider:
    """Build the configured speech provider.

    Raises:
        ModelError: the configured provider name has no implementation. A typo
            in configuration must fail at the start of the stage, not silently
            transcribe nothing.
    """
    speech = config.models.speech
    if speech.provider == "faster_whisper":
        return FasterWhisperProvider(speech, gpu=config.gpu, model_root=model_root)
    if speech.provider == "fake":
        return FakeSpeechProvider()
    raise ModelError(
        f"Unknown speech provider {speech.provider!r}.",
        code=ErrorCode.PROVIDER_NOT_REGISTERED,
        details={"provider": speech.provider, "supported": list(SPEECH_PROVIDERS)},
        recoverable=False,
    )


__all__ = [
    "SPEECH_PROVIDERS",
    "FakeSpeechProvider",
    "FasterWhisperProvider",
    "create_speech_provider",
]
