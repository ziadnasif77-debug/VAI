"""Thumbnail composition (docs/DIRECTION.md §22-§23): a plan, not a screenshot.

The doctrine's words: the thumbnail is a visual advertisement, one primary
subject, understandable at small size. Until now the pipeline shipped the
peak frame as extracted; this module recomposes it around the subject the
vision model locates -- cropped so the subject sits on a third, zoomed so it
carries the frame, vignetted so the eye lands on it.

The subject box comes from the same local VL that watches every candidate
frame, asked once with a schema (prompt ``vision.subject_box``). Validation
is the seatbelt: a box that is implausibly small, implausibly large, or
reported at low confidence recomposes nothing -- the un-recomposed peak
frame is already a thumbnail, and a crop around a wrong box is worse than
none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger

logger = get_logger("metadata.composition", LogChannel.PIPELINE)

#: The subject lands here horizontally (left third: the hook text block sits
#: bottom-centre, and RTL titles read from the right -- a left-set subject
#: fights neither).
_SUBJECT_ANCHOR_X: Final[float] = 0.33

#: How much of the frame's height the subject should carry after the zoom.
_SUBJECT_TARGET_HEIGHT: Final[float] = 0.62

#: Sanity rails on the model's box, as frame fractions.
_MIN_SIDE: Final[float] = 0.06
_MAX_SIDE: Final[float] = 0.85
_MIN_CONFIDENCE: Final[float] = 0.45

#: Never zoom past this: a 1.6x crop of a 1080p frame still upscales cleanly
#: to the 1280-wide thumbnail; past it the face turns to mud.
_MAX_ZOOM: Final[float] = 1.6


def valid_box(box: Any) -> tuple[float, float, float, float] | None:
    """The model's box, admitted only when it is plausible."""
    try:
        x = float(box["x"])
        y = float(box["y"])
        w = float(box["w"])
        h = float(box["h"])
        confidence = float(box.get("confidence", 0.0))
    except (TypeError, KeyError, ValueError):
        return None
    if confidence < _MIN_CONFIDENCE:
        return None
    if not (_MIN_SIDE <= w <= _MAX_SIDE and _MIN_SIDE <= h <= _MAX_SIDE):
        return None
    if x < 0 or y < 0 or x + w > 1.001 or y + h > 1.001:
        return None
    return (x, y, min(w, 1.0 - x), min(h, 1.0 - y))


def crop_for(
    box: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """The crop rectangle (left, top, right, bottom) that composes the frame.

    Pure arithmetic so the tests need no model and no image: zoom bounded by
    :data:`_MAX_ZOOM`, subject centred on the anchor third, crop clamped to
    the frame, aspect preserved.
    """
    x, y, w, h = box
    zoom = min(_SUBJECT_TARGET_HEIGHT / max(h, 1e-6), _MAX_ZOOM)
    if zoom <= 1.02:
        return None
    crop_h = height / zoom
    crop_w = width / zoom

    subject_cx = (x + w / 2.0) * width
    subject_cy = (y + h / 2.0) * height
    left = subject_cx - crop_w * _SUBJECT_ANCHOR_X
    top = subject_cy - crop_h * 0.5

    left = max(0.0, min(left, width - crop_w))
    top = max(0.0, min(top, height - crop_h))
    return (round(left), round(top), round(left + crop_w), round(top + crop_h))


def compose(image_path: Path, box: Any) -> bool:
    """Recompose the frame around the subject, in place. Never raises."""
    admitted = valid_box(box)
    if admitted is None:
        return False
    try:
        from PIL import Image, ImageDraw, ImageFilter

        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        rectangle = crop_for(admitted, width=image.width, height=image.height)
        if rectangle is None:
            return False
        composed = image.crop(rectangle).resize(image.size, Image.LANCZOS)

        # A soft vignette: the doctrine's focal pull, kept subtle enough that
        # the frame never reads as filtered.
        mask = Image.new("L", composed.size, 0)
        draw = ImageDraw.Draw(mask)
        margin_x = composed.width // 14
        margin_y = composed.height // 14
        draw.ellipse(
            [(-margin_x * 3, -margin_y * 3),
             (composed.width + margin_x * 3, composed.height + margin_y * 3)],
            fill=255,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(composed.width // 10))
        shaded = Image.composite(
            composed, Image.eval(composed, lambda value: int(value * 0.72)), mask
        )
        shaded.save(image_path, quality=92)
        logger.info(
            "Recomposed the thumbnail around its subject",
            extra={"crop": rectangle},
        )
        return True
    except Exception:
        logger.exception("Composition failed; the plain frame stands")
        return False


__all__ = ["compose", "crop_for", "valid_box"]
