"""The thumbnail's hook: a phrase that grabs, burned onto the peak frame.

A thumbnail is the video's one shot at a click, and a bare screenshot spends
it. The phrase comes from what the analysis already knows -- the dominant
moment type -- in the transcript's own language, drawn in the yellow every
thumbnail tradition converged on, flanked by the moment's own emoji.

Two rendering facts this module exists to know:

* PIL draws codepoints left-to-right as given, so Arabic must be reshaped
  (contextual letter forms) and bidi-reordered first or it renders as
  disconnected letters backwards. ``arabic_reshaper`` and ``python-bidi`` do
  exactly that and nothing else.
* Ordinary TTF faces carry no colour emoji. Windows ships Segoe UI Emoji with
  colour tables PIL can rasterise (``embedded_color=True``) -- probed on this
  machine before this was written: a flame renders red, a trophy gold. The
  emoji are drawn as their own pass beside the phrase, never inside the bidi
  string, so shaping and symbols cannot fight.

Degradation is §95's: no font, no emoji face, or any drawing failure leaves
what is already there -- a thumbnail without a hook is a product, a crash in
a suggestion endpoint is not.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger

logger = get_logger("metadata.hooks", LogChannel.PIPELINE)

#: One phrase per dominant moment type, written to be read in half a second.
#: Keyed by MomentType value: (arabic, english, emoji).
_PHRASES: Final[dict[str, tuple[str, str, str]]] = {
    "clutch": ("نجاة مستحيلة!", "IMPOSSIBLE CLUTCH!", "🔥"),
    "epic": ("لحظة أسطورية!", "LEGENDARY MOMENT!", "🌟"),
    "chaos": ("فوضى كاملة!", "TOTAL CHAOS!", "💥"),
    "boss": ("معركة الزعيم!", "BOSS FIGHT!", "⚔️"),
    "victory": ("انتصار ساحق!", "SWEET VICTORY!", "🏆"),
    "defeat": ("نهاية قاسية!", "BRUTAL ENDING!", "💀"),
    "fail": ("لن تصدق ما حدث!", "YOU WON'T BELIEVE IT!", "😱"),
    "funny": ("لن تتوقف عن الضحك!", "TRY NOT TO LAUGH!", "😂"),
    "tension": ("لحظات حبس الأنفاس!", "HOLD YOUR BREATH!", "😰"),
    "surprise": ("مفاجأة غير متوقعة!", "UNEXPECTED TWIST!", "😮"),
    "skill": ("مهارة خارقة!", "INSANE SKILL!", "🎯"),
    "rage": ("لحظة الانهيار!", "RAGE MOMENT!", "😡"),
    "comeback": ("عودة مستحيلة!", "EPIC COMEBACK!", "🚀"),
    "discovery": ("اكتشاف مذهل!", "AMAZING FIND!", "🔍"),
    "rare": ("لقطة نادرة!", "RARE MOMENT!", "💎"),
}
_FALLBACK: Final[tuple[str, str, str]] = ("لحظات لا تفوَّت!", "UNMISSABLE MOMENTS!", "🎮")

#: The thumbnail yellow. Chosen loud on purpose; the black stroke keeps it
#: readable over any footage.
_FILL: Final[tuple[int, int, int]] = (255, 217, 0)

#: Where a bold face that can draw Arabic lives on this platform. Read-only
#: use of the system drive; the standing rule forbids writing to it.
_FONTS: Final[tuple[str, ...]] = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

#: Colour-emoji faces, tried in order. Missing everywhere means the phrase
#: simply goes un-flanked.
_EMOJI_FONTS: Final[tuple[str, ...]] = (
    "C:/Windows/Fonts/seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
)

#: Segoe UI Emoji rasterises its colour glyphs at fixed strikes; PIL scales
#: whatever it gets, but asking near a strike keeps edges clean.
_EMOJI_GAP_RATIO: Final[float] = 0.35


def hook_phrase(moments: Sequence[Any], language: str | None) -> tuple[str, str]:
    """``(phrase, emoji)`` for these moments, in this language.

    The dominant *strong* moment type decides: each moment votes with its
    score, so three weak fails do not outvote one towering clutch.
    """
    votes: Counter[str] = Counter()
    for moment in moments:
        kind = str(getattr(getattr(moment, "moment_type", ""), "value", "") or "")
        if kind:
            votes[kind] += max(float(getattr(moment, "score", 0.0)), 0.05)
    dominant = votes.most_common(1)[0][0] if votes else ""
    arabic, english, emoji = _PHRASES.get(dominant, _FALLBACK)
    return (arabic if (language or "").startswith("ar") else english, emoji)


def burn_hook(image_path: Path, text: str, emoji: str = "") -> bool:
    """Draw ``text`` in thumbnail yellow onto the lower band, in place.

    ``emoji`` is drawn once on each side of the phrase, in colour, from a
    colour-capable face. Returns whether anything was drawn; never raises.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        shaped = _shaped(text)
        font_path = next((path for path in _FONTS if Path(path).is_file()), None)
        if font_path is None:
            logger.warning("No font found for the thumbnail hook; frame left plain")
            return False
        emoji_path = next((path for path in _EMOJI_FONTS if Path(path).is_file()), None)

        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        width, height = image.size
        size = max(int(height * 0.14), 24)
        draw = ImageDraw.Draw(image, "RGBA")

        def measure(points: int):
            face = ImageFont.truetype(font_path, points)
            box = draw.textbbox((0, 0), shaped, font=face)
            emoji_side = int(points * 1.05) if emoji and emoji_path else 0
            gap = int(points * _EMOJI_GAP_RATIO) if emoji_side else 0
            total = (box[2] - box[0]) + 2 * (emoji_side + gap)
            return face, box, emoji_side, gap, total

        font, box, emoji_side, gap, total_width = measure(size)
        while total_width > width * 0.94 and size > 16:
            size = int(size * 0.9)
            font, box, emoji_side, gap, total_width = measure(size)
        text_height = box[3] - box[1]

        band_top = height - int(text_height * 2.2)
        draw.rectangle([(0, band_top), (width, height)], fill=(0, 0, 0, 150))
        left = (width - total_width) // 2
        x = left + emoji_side + gap - box[0]
        y = band_top + int(text_height * 0.35) - box[1]
        draw.text(
            (x, y),
            shaped,
            font=font,
            fill=_FILL,
            stroke_width=max(size // 12, 2),
            stroke_fill=(0, 0, 0),
        )

        if emoji_side:
            emoji_font = ImageFont.truetype(emoji_path, emoji_side)
            emoji_y = band_top + int(text_height * 0.25)
            for emoji_x in (left, left + total_width - emoji_side):
                draw.text(
                    (emoji_x, emoji_y), emoji, font=emoji_font, embedded_color=True
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
