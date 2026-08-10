"""AI provider interfaces (SPEC sections 13, 93, 94, 95).

The AI layer is modular by requirement: "do not hardcode one model... the system
must continue working if a specific model is replaced". These protocols are that
contract. Everything above them -- gaming intelligence, moments, narrative --
depends on the interface, never on a runtime.

Two rules apply to every implementation:

* **Structured output only** (§93). A provider returns typed data. Pipeline
  decisions are never taken from free prose.
* **Bounded VRAM** (§54). ``load`` and ``unload`` exist because an 8 GB card
  cannot hold two models at once; the scheduler unloads one stage's model
  before the next one loads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Provenance recorded with every AI result (§49)."""

    name: str
    version: str
    provider: str
    quantization: str | None = None
    device: str = "auto"
    estimated_vram_mb: int = 0


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    """One word with its timing (§14)."""

    word: str
    start: float
    end: float
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One utterance (§14)."""

    start: float
    end: float
    text: str
    language: str | None = None
    speaker: str | None = None
    confidence: float | None = None
    words: tuple[TranscriptWord, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class VisionObservation:
    """What the vision model saw in one frame or short window (§15)."""

    timestamp: float
    description: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    hud: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    """Common lifecycle for anything that occupies VRAM."""

    def info(self) -> ModelInfo:
        """Identify the model, for provenance and cache keys."""
        ...

    def is_available(self) -> bool:
        """Whether the runtime and weights are present.

        Checked before a stage starts so a missing model degrades through the
        §95 fallback chain instead of failing mid-analysis.
        """
        ...

    def load(self) -> None:
        """Load the model. Idempotent."""
        ...

    def unload(self) -> None:
        """Release the model and its cached VRAM blocks. Idempotent."""
        ...


@runtime_checkable
class SpeechProvider(Provider, Protocol):
    """Speech-to-text with word timestamps (§14)."""

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        start_offset: float = 0.0,
    ) -> tuple[TranscriptSegment, ...]:
        """Transcribe an audio file.

        Args:
            audio_path: extracted analysis audio.
            language: ``None`` to detect.
            start_offset: seconds to add to every timestamp, so a chunk's
                results land on the source's timeline (§7).
        """
        ...


@dataclass(frozen=True, slots=True)
class TextDetection:
    """One piece of text read off a frame (§25).

    ``timestamp`` is mandatory and is the whole point: §25 requires every OCR
    result to carry one, because text without a time cannot become an event.
    """

    text: str
    confidence: float
    timestamp: float
    #: Which named profile region it came from, or ``None`` for a full-frame
    #: read. Knowing that a string came from the kill feed rather than from
    #: somewhere on screen is most of its meaning.
    region: str | None = None
    #: Bounding box in the analysed image, as ``(left, top, right, bottom)``.
    box: tuple[int, int, int, int] | None = None


@runtime_checkable
class OcrProvider(Provider, Protocol):
    """Text recognition on frames (§25)."""

    def read(
        self, image_path: Path, *, min_confidence: float = 0.0
    ) -> tuple[TextDetection, ...]:
        """Read text from one image.

        Timestamps are attached by the caller, which is the only layer that
        knows what instant the image came from; implementations return
        ``0.0`` and let :func:`~backend.gaming.ocr.read_frames` place them.
        """
        ...


@runtime_checkable
class VisionProvider(Provider, Protocol):
    """Frame understanding (§15)."""

    def describe(
        self, frame_paths: tuple[Path, ...], timestamps: tuple[float, ...], *, prompt_id: str
    ) -> tuple[VisionObservation, ...]:
        """Describe a small batch of frames.

        Batches are small on purpose: only candidate regions reach the model,
        never every sampled frame (§16).
        """
        ...


@runtime_checkable
class LLMProvider(Provider, Protocol):
    """Structured reasoning over analysis results (§93, §94)."""

    def complete_json(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        prompt_id: str,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Return a JSON object conforming to ``schema``.

        Implementations must validate before returning. An unparseable or
        non-conforming response is retried and then surfaced as a typed error;
        it is never passed downstream (§94).
        """
        ...


class ProviderRegistry:
    """Maps a capability name to its provider instance.

    Registration is explicit so an unavailable capability is a clear error at
    the start of a stage rather than an obscure failure inside it.
    """

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, capability: str, provider: Provider, *, replace: bool = False) -> None:
        if capability in self._providers and not replace:
            raise ValueError(
                f"A provider is already registered for {capability!r}. "
                "Pass replace=True to override it."
            )
        self._providers[capability] = provider

    def get(self, capability: str) -> Provider:
        try:
            return self._providers[capability]
        except KeyError as exc:
            raise KeyError(
                f"No provider registered for {capability!r}. "
                f"Available: {sorted(self._providers)}"
            ) from exc

    def has(self, capability: str) -> bool:
        return capability in self._providers

    def available(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def clear(self) -> None:
        self._providers.clear()


#: Process-wide registry, populated at startup from ``config/models.yaml``.
registry = ProviderRegistry()


__all__ = [
    "LLMProvider",
    "ModelInfo",
    "OcrProvider",
    "Provider",
    "ProviderRegistry",
    "SpeechProvider",
    "TextDetection",
    "TranscriptSegment",
    "TranscriptWord",
    "VisionObservation",
    "VisionProvider",
    "registry",
]
