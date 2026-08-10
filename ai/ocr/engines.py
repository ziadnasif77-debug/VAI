"""OCR engines (SPEC sections 25, 13, 95).

Three engines behind one protocol, because §25 needs text off a frame and no
single OCR package installs cleanly everywhere. `config/analysis.yaml` already
declares the choice — ``paddleocr | easyocr | tesseract | auto`` — and this is
where that choice becomes a runtime.

**Availability means "imports and initialises", not "is installed".** That
distinction is not pedantry: PaddlePaddle 2.6 ships protobuf-generated code
that a protobuf 4+ runtime refuses to load, so ``paddleocr`` can be present in
site-packages and raise ``TypeError`` on import. An availability check that only
asks whether the package exists reports a broken engine as ready, and the
failure then arrives in the middle of a stage.

Every engine returns text with a confidence and a box, and **no timestamp** —
the caller places those, because it is the only layer that knows which instant
an image came from.
"""

from __future__ import annotations

import functools
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

from ai.providers.base import ModelInfo, TextDetection
from backend.config.schema import OcrConfig, OcrModelConfig
from backend.core.errors import ErrorCode, ModelError
from backend.core.logging import LogChannel, get_logger, log_duration

logger = get_logger("ocr.engines", LogChannel.AI)

#: Engines the ``auto`` setting will try, best first. PaddleOCR leads because
#: §25 names it and it is the strongest on stylised game fonts; EasyOCR is the
#: pragmatic fallback; Tesseract is last because it struggles most with the
#: low-contrast, heavily-styled text a game HUD is made of.
AUTO_ORDER: Final[tuple[str, ...]] = ("paddleocr", "easyocr", "tesseract")


_MODULES: Final[dict[str, str]] = {
    "paddleocr": "paddleocr",
    "easyocr": "easyocr",
    "tesseract": "pytesseract",
}

#: Long enough for PaddlePaddle to load its C++ extensions and fail, short
#: enough that probing three engines is not a visible pause.
_PROBE_TIMEOUT_SECONDS: Final[int] = 120


@functools.lru_cache(maxsize=8)
def engine_importable(name: str) -> bool:
    """Whether an engine's package imports cleanly, probed in a **subprocess**.

    The subprocess is not caution, it is necessity. A failed ``import
    paddleocr`` does not merely raise -- it leaves the interpreter's protobuf
    state broken, and a subsequent ``import easyocr`` in the same process then
    fails too. Probing in-process therefore turns one broken engine into no
    working engines, and ``auto`` picks nothing on a machine that has a
    perfectly good EasyOCR installed.

    Cached, because the answer cannot change while the process runs and the
    probe costs a second.
    """
    module = _MODULES.get(name)
    if module is None:
        return False
    try:
        completed = subprocess.run(  # explicit argv, no shell (§85)
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        logger.info("Could not probe OCR engine", extra={"engine": name, "error": str(exc)})
        return False

    if completed.returncode != 0:
        logger.info(
            "OCR engine is installed but not usable",
            extra={
                "engine": name,
                "error": (completed.stderr or "").strip().splitlines()[-1:] or ["unknown"],
            },
        )
        return False
    return True


def resolve_engine(requested: str) -> str | None:
    """Return the engine to use, or ``None`` when none is usable.

    ``auto`` walks :data:`AUTO_ORDER` and takes the first that imports. A named
    engine is used only if it works — falling back silently from an explicit
    choice would hide a broken installation the user asked to use.
    """
    if requested == "auto":
        return next((name for name in AUTO_ORDER if engine_importable(name)), None)
    return requested if engine_importable(requested) else None


class _BaseEngine:
    """Shared lifecycle for the OCR engines."""

    engine_name = "base"

    def __init__(self, config: OcrConfig, model: OcrModelConfig) -> None:
        self._config = config
        self._model_config = model
        self._reader: Any | None = None

    def info(self) -> ModelInfo:
        return ModelInfo(
            name=self._model_config.model,
            version=self._model_config.version,
            provider=self.engine_name,
            device=self._model_config.device,
            estimated_vram_mb=self._model_config.estimated_vram_mb,
        )

    def is_available(self) -> bool:
        return engine_importable(self.engine_name)

    def unload(self) -> None:
        """Release the reader. Idempotent."""
        if self._reader is None:
            return
        self._reader = None
        import gc

        gc.collect()

    def _unavailable(self) -> ModelError:
        return ModelError(
            f"OCR engine {self.engine_name!r} is not usable.",
            code=ErrorCode.MODEL_UNAVAILABLE,
            details={"engine": self.engine_name},
        )


class EasyOcrEngine(_BaseEngine):
    """OCR via EasyOCR. Torch-backed, so it uses the GPU when one is present."""

    engine_name = "easyocr"

    def load(self) -> None:
        if self._reader is not None:
            return
        if not self.is_available():
            raise self._unavailable()

        import easyocr

        languages = list(self._config.languages) or ["en"]
        use_gpu = self._model_config.device != "cpu" and _torch_has_cuda()
        with log_duration(
            logger, "Loaded OCR engine", engine=self.engine_name, gpu=use_gpu
        ):
            self._reader = easyocr.Reader(languages, gpu=use_gpu, verbose=False)

    def read(
        self, image_path: Path, *, min_confidence: float = 0.0
    ) -> tuple[TextDetection, ...]:
        self.load()
        try:
            results = self._reader.readtext(str(image_path))
        except Exception as exc:
            raise ModelError(
                f"EasyOCR failed on {Path(image_path).name}: {exc}",
                code=ErrorCode.OCR_FAILED,
                details={"path": str(image_path)},
                cause=exc,
            ) from exc

        detections: list[TextDetection] = []
        for box, text, confidence in results:
            score = float(confidence)
            cleaned = str(text).strip()
            if not cleaned or score < min_confidence:
                continue
            detections.append(
                TextDetection(
                    text=cleaned,
                    confidence=score,
                    timestamp=0.0,
                    box=_bounds(box),
                )
            )
        return tuple(detections)


class PaddleOcrEngine(_BaseEngine):
    """OCR via PaddleOCR. §25's default where it installs cleanly."""

    engine_name = "paddleocr"

    def load(self) -> None:
        if self._reader is not None:
            return
        if not self.is_available():
            raise self._unavailable()

        from paddleocr import PaddleOCR

        language = (self._config.languages or ["en"])[0]
        with log_duration(logger, "Loaded OCR engine", engine=self.engine_name):
            self._reader = PaddleOCR(use_angle_cls=True, lang=language, show_log=False)

    def read(
        self, image_path: Path, *, min_confidence: float = 0.0
    ) -> tuple[TextDetection, ...]:
        self.load()
        try:
            results = self._reader.ocr(str(image_path), cls=True)
        except Exception as exc:
            raise ModelError(
                f"PaddleOCR failed on {Path(image_path).name}: {exc}",
                code=ErrorCode.OCR_FAILED,
                details={"path": str(image_path)},
                cause=exc,
            ) from exc

        detections: list[TextDetection] = []
        # PaddleOCR nests one list per image, and returns [None] for an image
        # with no text at all.
        for page in results or []:
            for entry in page or []:
                box, (text, confidence) = entry[0], entry[1]
                score = float(confidence)
                cleaned = str(text).strip()
                if not cleaned or score < min_confidence:
                    continue
                detections.append(
                    TextDetection(
                        text=cleaned, confidence=score, timestamp=0.0, box=_bounds(box)
                    )
                )
        return tuple(detections)


class TesseractEngine(_BaseEngine):
    """OCR via Tesseract. Last resort: weakest on stylised game fonts."""

    engine_name = "tesseract"

    def load(self) -> None:
        if not self.is_available():
            raise self._unavailable()
        self._reader = True  # stateless: the binary is invoked per call

    def read(
        self, image_path: Path, *, min_confidence: float = 0.0
    ) -> tuple[TextDetection, ...]:
        self.load()
        import pytesseract
        from PIL import Image

        try:
            with Image.open(image_path) as image:
                data = pytesseract.image_to_data(
                    image, output_type=pytesseract.Output.DICT
                )
        except Exception as exc:
            raise ModelError(
                f"Tesseract failed on {Path(image_path).name}: {exc}",
                code=ErrorCode.OCR_FAILED,
                details={"path": str(image_path)},
                cause=exc,
            ) from exc

        detections: list[TextDetection] = []
        for index, text in enumerate(data.get("text", [])):
            cleaned = str(text).strip()
            # Tesseract reports -1 for lines it did not score.
            score = max(float(data["conf"][index]), 0.0) / 100.0
            if not cleaned or score < min_confidence:
                continue
            left, top = int(data["left"][index]), int(data["top"][index])
            detections.append(
                TextDetection(
                    text=cleaned,
                    confidence=score,
                    timestamp=0.0,
                    box=(
                        left,
                        top,
                        left + int(data["width"][index]),
                        top + int(data["height"][index]),
                    ),
                )
            )
        return tuple(detections)


ENGINES: Final[dict[str, type[_BaseEngine]]] = {
    "easyocr": EasyOcrEngine,
    "paddleocr": PaddleOcrEngine,
    "tesseract": TesseractEngine,
}


def _bounds(box: Any) -> tuple[int, int, int, int] | None:
    """Normalise an engine's polygon into ``(left, top, right, bottom)``."""
    try:
        points = [(float(point[0]), float(point[1])) for point in box]
    except (TypeError, ValueError, IndexError):
        return None
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys)))


def _torch_has_cuda() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:  # torch is optional
        return False


__all__ = [
    "AUTO_ORDER",
    "ENGINES",
    "EasyOcrEngine",
    "PaddleOcrEngine",
    "TesseractEngine",
    "engine_importable",
    "resolve_engine",
]
