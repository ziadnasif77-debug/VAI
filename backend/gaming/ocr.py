"""Region-restricted OCR (SPEC sections 25, 23, 15).

§25 wants text off the frame — kill feed, victory, defeat, score, objectives,
damage, timers, items, player names — and it wants **every result to carry a
timestamp**. Text without a time cannot become an event, so the timestamp is
attached here, by the only layer that knows which instant an image came from.

Two paths, and which one runs is the whole §22/§23 trade-off:

* **Region-restricted.** A profile declares where the kill feed and the score
  live, and OCR reads only those boxes. Cheaper — three small crops instead of
  a 720p frame — and far more accurate, because a recogniser given a tight crop
  of a kill feed is not also trying to read the minimap, the crosshair and the
  watermark.
* **Full-frame fallback.** No profile, or no declared regions. §23 requires
  this to work, so the frame is read whole, at **reduced resolution** and only
  on candidate frames — the same cascade logic as §15, for the same reason.

Regions are cropped rather than passed as coordinates because no OCR engine's
API takes a region of interest, and cropping is what makes the accuracy gain
real rather than notional.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ai.providers.base import OcrProvider, TextDetection
from backend.config.schema import OcrConfig
from backend.core.logging import LogChannel, get_logger
from backend.gaming.profiles import GameProfile, Region

logger = get_logger("gaming.ocr", LogChannel.PIPELINE)

#: Subdirectory holding the crops handed to the recogniser. Kept on disk rather
#: than in memory so a surprising reading can be looked at.
CROP_DIRNAME: Final[str] = "ocr_crops"

#: Name used for a full-frame read, so a detection's origin is never ambiguous.
FULL_FRAME: Final[str] = "full_frame"


@dataclass(frozen=True, slots=True)
class FrameText:
    """Everything read from one frame."""

    timestamp: float
    frame_path: Path
    detections: tuple[TextDetection, ...]

    @property
    def is_empty(self) -> bool:
        return not self.detections

    def text(self) -> str:
        """All detections joined, for pattern matching across a whole frame."""
        return " ".join(detection.text for detection in self.detections)

    def by_region(self, region: str) -> tuple[TextDetection, ...]:
        return tuple(item for item in self.detections if item.region == region)


def read_frames(
    frames: Sequence[tuple[float, Path]],
    provider: OcrProvider,
    config: OcrConfig,
    profile: GameProfile,
    *,
    work_dir: Path,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> list[FrameText]:
    """Read text from each frame, region-restricted where a profile allows.

    Args:
        frames: ``(timestamp, path)`` pairs. The timestamp is what makes the
            result usable (§25), so it is required rather than derived.
        provider: the OCR engine.
        config: ``analysis.ocr``.
        profile: the game profile. A generic one takes the full-frame path.
        work_dir: where crops are written.

    Returns one :class:`FrameText` per frame that produced any text. Frames
    that read as nothing are omitted rather than stored empty — a HUD-free
    frame is the common case, not a finding.
    """
    regions = profile.reading_regions() if config.use_profile_regions else {}
    use_regions = bool(regions)
    if not use_regions and not config.full_frame_fallback:
        logger.info(
            "No profile regions and full-frame fallback disabled; OCR reads nothing",
            extra={"profile": profile.id},
        )
        return []

    results: list[FrameText] = []
    total = max(len(frames), 1)
    for index, (timestamp, path) in enumerate(frames):
        if should_cancel is not None and should_cancel():
            from backend.media.ffmpeg import CancelledError

            raise CancelledError(details={"stage": "ocr", "frame": index})
        if on_progress is not None:
            on_progress(index / total, f"Reading text {index + 1}/{len(frames)}")

        detections = (
            _read_regions(path, timestamp, provider, config, regions, work_dir)
            if use_regions
            else _read_full_frame(path, timestamp, provider, config, work_dir)
        )
        kept = tuple(item for item in detections if not profile.should_ignore(item.text))
        if kept:
            results.append(FrameText(timestamp=timestamp, frame_path=Path(path), detections=kept))

    logger.info(
        "Read text from frames",
        extra={
            "frames": len(frames),
            "with_text": len(results),
            "detections": sum(len(item.detections) for item in results),
            "mode": "regions" if use_regions else "full_frame",
            "profile": profile.id,
        },
    )
    return results


# ---------------------------------------------------------------------------
# reading modes
# ---------------------------------------------------------------------------


def _read_regions(
    path: Path,
    timestamp: float,
    provider: OcrProvider,
    config: OcrConfig,
    regions: dict[str, Region],
    work_dir: Path,
) -> list[TextDetection]:
    """Read only the boxes a profile declared (§25)."""
    detections: list[TextDetection] = []
    for name, region in regions.items():
        crop = _crop(path, region, work_dir, suffix=name)
        if crop is None:
            continue
        for item in provider.read(crop, min_confidence=config.min_confidence):
            detections.append(
                TextDetection(
                    text=item.text,
                    confidence=item.confidence,
                    timestamp=timestamp,
                    region=name,
                    box=item.box,
                )
            )
    return detections


def _read_full_frame(
    path: Path,
    timestamp: float,
    provider: OcrProvider,
    config: OcrConfig,
    work_dir: Path,
) -> list[TextDetection]:
    """Read the whole frame at reduced resolution (§23).

    The unknown-game path. Downscaling is not only about speed: a recogniser
    given a 1080p frame finds far more spurious text in textures and particle
    effects than one given the same frame at 960 px wide.
    """
    scaled = _downscale(path, config.full_frame_max_width, work_dir)
    source = scaled if scaled is not None else Path(path)
    return [
        TextDetection(
            text=item.text,
            confidence=item.confidence,
            timestamp=timestamp,
            region=FULL_FRAME,
            box=item.box,
        )
        for item in provider.read(source, min_confidence=config.min_confidence)
    ]


# ---------------------------------------------------------------------------
# image helpers
# ---------------------------------------------------------------------------


def _crop(path: Path, region: Region, work_dir: Path, *, suffix: str) -> Path | None:
    """Write the region's crop and return its path, or ``None`` on failure."""
    image = _open(path)
    if image is None:
        return None
    with image:
        box = region.to_pixels(image.width, image.height)
        destination = _destination(path, work_dir, suffix)
        try:
            image.crop(box).save(destination)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not crop a region", extra={"path": str(path), "region": suffix,
                                                  "error": str(exc)}
            )
            return None
    return destination


def _downscale(path: Path, max_width: int, work_dir: Path) -> Path | None:
    """Write a width-limited copy, or ``None`` if the frame is already small."""
    image = _open(path)
    if image is None:
        return None
    with image:
        if image.width <= max_width:
            return None
        height = max(round(image.height * max_width / image.width), 1)
        destination = _destination(path, work_dir, "scaled")
        try:
            image.resize((max_width, height)).save(destination)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not downscale a frame",
                extra={"path": str(path), "error": str(exc)},
            )
            return None
    return destination


def _open(path: Path) -> Any:
    try:
        from PIL import Image

        return Image.open(path)
    except ImportError:  # pragma: no cover - reported by doctor.py
        logger.warning("Pillow is unavailable; OCR cannot crop or downscale frames")
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Could not open a frame", extra={"path": str(path), "error": str(exc)})
        return None


def _destination(path: Path, work_dir: Path, suffix: str) -> Path:
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{Path(path).stem}__{suffix}.png"


def merge_text(results: Iterable[FrameText]) -> str:
    """All text from several frames, for a coarse whole-source search."""
    return " ".join(item.text() for item in results)


__all__ = ["CROP_DIRNAME", "FULL_FRAME", "FrameText", "merge_text", "read_frames"]
