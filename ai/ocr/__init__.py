"""OCR providers (SPEC sections 13, 25, 95).

The factory maps ``analysis.ocr.engine`` onto a runtime. ``auto`` picks the
first engine that actually imports, which is what makes the setting useful on a
machine where one OCR package is installed but broken.

Returns ``None`` rather than raising when nothing is usable: §95 degrades
missing OCR to vision and audio, and a stage that cannot read text should say
so and continue rather than fail the analysis.
"""

from __future__ import annotations

from ai.ocr.engines import AUTO_ORDER, ENGINES, engine_importable, resolve_engine
from ai.ocr.fake_provider import FakeOcrProvider
from ai.providers.base import OcrProvider
from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, ModelError
from backend.core.logging import LogChannel, get_logger

logger = get_logger("ocr.factory", LogChannel.AI)

#: Engine names accepted in ``config/analysis.yaml``.
OCR_ENGINES = (*AUTO_ORDER, "fake", "auto")


def create_ocr_provider(config: AppConfig) -> OcrProvider | None:
    """Build an OCR provider, or ``None`` when no engine is usable.

    Raises:
        ModelError: the configured engine name is not one this application
            knows. A typo must fail loudly; a *missing* engine must not.
    """
    requested = config.analysis.ocr.engine
    if requested == "fake":
        return FakeOcrProvider()
    if requested not in {*AUTO_ORDER, "auto"}:
        raise ModelError(
            f"Unknown OCR engine {requested!r}.",
            code=ErrorCode.PROVIDER_NOT_REGISTERED,
            details={"engine": requested, "supported": list(OCR_ENGINES)},
            recoverable=False,
        )

    resolved = resolve_engine(requested)
    if resolved is None:
        logger.warning(
            "No usable OCR engine; text reading is unavailable",
            extra={
                "requested": requested,
                "installed_but_broken": [
                    name for name in AUTO_ORDER if not engine_importable(name)
                ],
            },
        )
        return None
    if resolved != requested:
        logger.info("Resolved OCR engine", extra={"requested": requested, "using": resolved})
    return ENGINES[resolved](config.analysis.ocr, config.models.ocr)


__all__ = [
    "AUTO_ORDER",
    "OCR_ENGINES",
    "FakeOcrProvider",
    "create_ocr_provider",
    "engine_importable",
    "resolve_engine",
]
