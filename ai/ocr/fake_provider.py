"""A deterministic OCR provider for tests (SPEC §13, §25, §113-§119).

Real OCR needs a model, a GPU and an image with legible text in it. Almost
nothing the pipeline does with a detection needs any of that: region
restriction, timestamp attachment, the ignore list, event rules and correlation
all care about the *shape* of a detection, not about pixels.

So this returns text scripted per timestamp. A test says "at 12.0 seconds the
kill feed reads ELIMINATED" and then asserts what the pipeline made of it,
which is the part worth testing.

A test double, never a fallback. §95 degrades missing OCR to vision and audio,
not to invented text — inventing a "VICTORY" nobody saw would put a fabricated
event in the finished video.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ai.providers.base import ModelInfo, TextDetection

FAKE_VERSION = "fake-ocr-1"


class FakeOcrProvider:
    """Returns scripted text, keyed by image filename or in a fixed sequence."""

    def __init__(
        self,
        *,
        by_filename: Mapping[str, Sequence[tuple[str, float]]] | None = None,
        default: Sequence[tuple[str, float]] = (),
        available: bool = True,
    ) -> None:
        """
        Args:
            by_filename: image filename to ``(text, confidence)`` pairs. Frame
                filenames encode their timestamp, so a test scripts a moment by
                naming the frame it happens on.
            default: returned for any image not named in ``by_filename``.
            available: report unavailable, to exercise the §95 fallback path.
        """
        self._by_filename = {key: tuple(value) for key, value in (by_filename or {}).items()}
        self._default = tuple(default)
        self._available = available
        self.load_count = 0
        self.unload_count = 0
        self.read_paths: list[Path] = []

    def info(self) -> ModelInfo:
        return ModelInfo(
            name="fake-ocr", version=FAKE_VERSION, provider="fake", device="cpu"
        )

    def is_available(self) -> bool:
        return self._available

    def load(self) -> None:
        self.load_count += 1

    def unload(self) -> None:
        self.unload_count += 1

    def read(
        self, image_path: Path, *, min_confidence: float = 0.0
    ) -> tuple[TextDetection, ...]:
        path = Path(image_path)
        self.read_paths.append(path)
        scripted = self._by_filename.get(path.name, self._default)
        return tuple(
            TextDetection(text=text, confidence=confidence, timestamp=0.0)
            for text, confidence in scripted
            if confidence >= min_confidence
        )


__all__ = ["FAKE_VERSION", "FakeOcrProvider"]
