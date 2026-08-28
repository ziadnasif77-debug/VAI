"""The thumbnail's hook: a phrase that grabs, burned onto the peak frame.

A thumbnail is the video's one shot at a click, and a bare screenshot spends
it. The phrase comes from what the analysis already knows -- the dominant
moment type -- in the transcript's own language, and it is burned with real
Arabic shaping: PIL draws codepoints left-to-right as given, so Arabic text
must be reshaped (contextual letter forms) and bidi-reordered first or it
renders as disconnected letters backwards. ``arabic_reshaper`` and
``python-bidi`` do exactly that and nothing else.

Degradation is §95's: no font found, or a drawing failure of any kind, leaves
the plain frame -- a thumbnail without a hook is a product, a crash in a
suggestion endpoint is not.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger

logger = get_logger("metadata.hooks", LogChannel.PIPELINE)

#: One phrase per dominant moment type, written to be read in half a second.
#: Keyed by MomentType value; the fallback row covers everything unlisted.
_PHRASES: Final[dict[str, tuple[str, str]]] = {
    # value: (arabic, english)
    "clutch": ("نجاة مستحيلة!", "IMPOSSIBLE CLUTCH!"),
    "epic": ("لحظة أسطورية!", "LEGENDARY MOMENT!"),
    "chaos": ("فوضى كاملة!", "TOTAL CHAOS!"),
    "boss": ("معركة الزعيم!", "BOSS FIGHT!"),
    "victory": ("انتصار ساحق!", "SWEET VICTORY!"),
    "defeat": ("نهاية قاسية!", "BRUTAL ENDING!"),
    "fail": ("لن تصدق ما حدث!", "YOU WON'T BELIEVE IT!"),
    "funny": ("لن تتوقف عن الضحك!", "TRY NOT TO LAUGH!"),
    "tension": ("لحظات حبس الأنفاس!", "HOLD YOUR BREATH!"),
    "surprise": ("مفاجأة غير متوقعة!", "UNEXPECTED TWIST!"),
    "skill": ("مهارة خارقة!", "INSANE SKILL!"),
    "rage": ("لحظة الانهيار!", "RAGE MOMENT!"),
    "comeback": ("عودة مستحيلة!", "EPIC COMEBACK!"),
    "discovery": ("اكتشاف مذهل!", "AMAZING FIND!"),
    "rare": ("لقطة نادرة!", "RARE MOMENT!"),
}
_FALLBACK: Final[tuple[str, str]] = ("لحظات لا تفوَّت!", "UNMISSABLE MOMENTS!")

#: Where a bold face that can draw Arabic lives on this platform. Read-only
#: use of the system drive; the standing rule forbids writing to it.
_FONTS: Final[tuple[str, ...]] = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def hook_phrase(moments: Sequence[Any], language: str | None) -> str:
    """The phrase for these moments, in this language.

    The dominant *strong* moment type decides: each moment votes with its
    score, so three weak fails do not outvote one towering clutch.
    """
    votes: Counter[str] = Counter()
    for moment in moments:
        kind = str(getattr(getattr(moment, "moment_type", ""), "value", "") or "")
        if kind:
            votes[kind] += max(float(getattr(moment, "score", 0.0)), 0.05)
    dominant = votes.most_common(1)[0][0] if votes else ""
    arabic, english = _PHRASES.get(dominant, _FALLBACK)
    return arabic if (language or "").startswith("ar") else english


def burn_hook(image_path: Path, text: str) -> bool:
    """Draw ``text`` onto the thumbnail's lower band, in place.

    Returns whether anything was drawn. Never raises: every failure is a log
    line and ``False``, because the plain frame is already a usable thumbnail.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        shaped = _shaped(text)
        font_path = next((path for path in _FONTS if Path(path).is_file()), None)
        if font_path is None:
            logger.warning("No font found for the thumbnail hook; frame left plain")
            return False

        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        width, height = image.size
        size = max(int(height * 0.14), 24)
        font = ImageFont.truetype(font_path, size)
        draw = ImageDraw.Draw(image, "RGBA")

        box = draw.textbbox((0, 0), shaped, font=font)
        text_width = box[2] - box[0]
        while text_width > width * 0.92 and size > 16:
            size = int(size * 0.9)
            font = ImageFont.truetype(font_path, size)
            box = draw.textbbox((0, 0), shaped, font=font)
            text_width = box[2] - box[0]
        text_height = box[3] - box[1]

        band_top = height - int(text_height * 2.2)
        draw.rectangle([(0, band_top), (width, height)], fill=(0, 0, 0, 150))
        x = (width - text_width) // 2 - box[0]
        y = band_top + int(text_height * 0.35) - box[1]
        draw.text(
            (x, y),
            shaped,
            font=font,
            fill=(255, 255, 255),
            stroke_width=max(size // 14, 2),
            stroke_fill=(0, 0, 0),
        )
        image.save(image_path, quality=92)
        return True
    except Exception:
        logger.exception("Could not burn the thumbnail hook; frame left plain")
        return False


def _shaped(text: str) -> str:
    """Arabic-shape and bidi-reorder when the text needs it."""
    if not any("\u0600" <= ch <= "\u06ff" for ch in text):
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display

    return get_display(arabic_reshaper.reshape(text))


__all__ = ["burn_hook", "hook_phrase"]
