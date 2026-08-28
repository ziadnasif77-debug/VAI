"""The thumbnail's hook: a phrase that grabs, burned onto the peak frame.

A thumbnail is the video's one shot at a click, and a bare screenshot spends
it. The phrase comes from what the analysis already knows -- the dominant
moment type -- in the transcript's own language, set in the style Arabic
gaming thumbnails converged on: two stacked lines, white over red, fattened
strokes, a slight rise to the right.

Rendering facts this module exists to know, each measured on this machine:

* PIL draws codepoints left-to-right as given, so Arabic must be reshaped
  (contextual forms) and bidi-reordered first or it renders disconnected and
  backwards. ``arabic_reshaper`` + ``python-bidi`` do exactly that.
* Segoe UI Black ships no Arabic at all (tofu boxes); Simplified Arabic Bold
  breaks on shadda. **Tahoma Bold** is the heaviest clean Arabic face on a
  stock Windows, and drawing the fill with a same-colour stroke fattens it to
  display weight.
* Ordinary faces carry no colour emoji; Segoe UI Emoji's colour tables
  rasterise when PIL is asked (``embedded_color``) -- probed: flame red,
  trophy gold.

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
#: ``|`` marks the line break; the first line goes white, the second red.
_PHRASES: Final[dict[str, tuple[str, str, str]]] = {
    "clutch": ("نجاة|مستحيلة!", "IMPOSSIBLE|CLUTCH!", "🔥"),
    "epic": ("لحظة|أسطورية!", "LEGENDARY|MOMENT!", "🌟"),
    "chaos": ("فوضى|كاملة!", "TOTAL|CHAOS!", "💥"),
    "boss": ("معركة|الزعيم!", "BOSS|FIGHT!", "⚔️"),
    "victory": ("انتصار|ساحق!", "SWEET|VICTORY!", "🏆"),
    "defeat": ("نهاية|قاسية!", "BRUTAL|ENDING!", "💀"),
    "fail": ("لن تصدق|ما حدث!", "YOU WON'T|BELIEVE IT!", "😱"),
    "funny": ("موتت|من الضحك!", "TRY NOT|TO LAUGH!", "😂"),
    "tension": ("لحظات|حبس الأنفاس!", "HOLD YOUR|BREATH!", "😰"),
    "surprise": ("مفاجأة|غير متوقعة!", "UNEXPECTED|TWIST!", "😮"),
    "skill": ("مهارة|خارقة!", "INSANE|SKILL!", "🎯"),
    "rage": ("لحظة|الانهيار!", "RAGE|MOMENT!", "😡"),
    "comeback": ("عودة|مستحيلة!", "EPIC|COMEBACK!", "🚀"),
    "discovery": ("اكتشاف|مذهل!", "AMAZING|FIND!", "🔍"),
    "rare": ("لقطة|نادرة!", "RARE|MOMENT!", "💎"),
}
_FALLBACK: Final[tuple[str, str, str]] = ("لحظات|لا تفوَّت!", "UNMISSABLE|MOMENTS!", "🎮")

#: Line colours, top to bottom -- the white-over-red every Arabic gaming
#: thumbnail tradition converged on. Each is (fill, inner stroke, outer
#: stroke): white gets black edges; red gets a white edge inside a black one.
_LINE_STYLES: Final[tuple[tuple, ...]] = (
    ((255, 255, 255), (255, 255, 255), (12, 12, 12)),
    ((232, 33, 39), (255, 255, 255), (12, 12, 12)),
)

#: The rise to the right, in degrees. Enough to read as designed, not enough
#: to read as broken.
_TILT_DEGREES: Final[float] = 5.0

#: Heaviest clean Arabic faces first, measured by the probe strip.
_FONTS: Final[tuple[str, ...]] = (
    "C:/Windows/Fonts/tahomabd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

_EMOJI_FONTS: Final[tuple[str, ...]] = (
    "C:/Windows/Fonts/seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
)


def hook_phrase(moments: Sequence[Any], language: str | None) -> tuple[str, str]:
    """``(phrase, emoji)`` for these moments, in this language.

    The phrase carries ``|`` where the thumbnail breaks its lines. The
    dominant *strong* moment type decides: each moment votes with its score,
    so three weak fails do not outvote one towering clutch.
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
    """Burn the two-line tilted hook onto the frame, in place.

    Returns whether anything was drawn; never raises.
    """
    try:
        from PIL import Image

        font_path = next((path for path in _FONTS if Path(path).is_file()), None)
        if font_path is None:
            logger.warning("No font found for the thumbnail hook; frame left plain")
            return False
        emoji_path = next((path for path in _EMOJI_FONTS if Path(path).is_file()), None)

        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        width, height = image.size

        lines = [_shaped(part.strip()) for part in text.split("|") if part.strip()]
        layer = _text_layer(lines, width, height, font_path, emoji, emoji_path)
        layer = layer.rotate(
            _TILT_DEGREES, expand=True, resample=Image.BICUBIC
        )

        x = (width - layer.width) // 2
        # Above the duration stamp YouTube draws in the lower corner.
        y = height - layer.height - int(height * 0.06)
        image.paste(layer, (x, max(y, 0)), layer)
        image.save(image_path, quality=92)
        return True
    except Exception:
        logger.exception("Could not burn the thumbnail hook; frame left plain")
        return False


def _text_layer(lines, width, height, font_path, emoji, emoji_path):
    """The stacked, stroked, flanked text on its own transparent layer."""
    from PIL import Image, ImageDraw, ImageFont

    size = max(int(height * 0.20), 30)

    def fitted(points):
        font = ImageFont.truetype(font_path, points)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        widths = [probe.textbbox((0, 0), line, font=font)[2] for line in lines]
        return font, max(widths)

    font, widest = fitted(size)
    while widest > width * 0.72 and size > 24:
        size = int(size * 0.92)
        font, widest = fitted(size)

    outer = max(size // 7, 6)
    inner = max(size // 22, 2)
    fatten = max(size // 26, 2)
    line_height = int(size * 1.28)
    emoji_side = int(size * 0.95) if emoji and emoji_path else 0
    gap = int(size * 0.3) if emoji_side else 0

    block_width = widest + 2 * (outer + fatten) + 2 * (emoji_side + gap)
    block_height = line_height * len(lines) + 2 * (outer + fatten) + int(size * 0.2)
    layer = Image.new("RGBA", (block_width, block_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # A soft dark scrim under the whole block. Research and the owner's own
    # screenshot agree: strokes alone lose over bright, busy frames; the
    # scrim guarantees the contrast the strokes then sharpen.
    pad = int(size * 0.25)
    draw.rounded_rectangle(
        [(max(emoji_side - pad, 0), 0), (block_width - max(emoji_side - pad, 0), block_height)],
        radius=int(size * 0.4),
        fill=(0, 0, 0, 115),
    )

    centre_x = block_width // 2
    y = outer + fatten
    for index, line in enumerate(lines):
        fill, inner_stroke, outer_stroke = _LINE_STYLES[min(index, len(_LINE_STYLES) - 1)]
        # Drop shadow, then outer edge, then the fattened fill with its inner
        # edge: three passes are what "clean and thick" costs.
        draw.text(
            (centre_x + outer // 2, y + outer // 2 + 3),
            line, font=font, anchor="ma", fill=(0, 0, 0, 140),
            stroke_width=outer, stroke_fill=(0, 0, 0, 140),
        )
        draw.text(
            (centre_x, y), line, font=font, anchor="ma",
            fill=outer_stroke, stroke_width=outer, stroke_fill=outer_stroke,
        )
        draw.text(
            (centre_x, y), line, font=font, anchor="ma",
            fill=fill, stroke_width=inner + fatten, stroke_fill=inner_stroke,
        )
        draw.text(
            (centre_x, y), line, font=font, anchor="ma",
            fill=fill, stroke_width=fatten, stroke_fill=fill,
        )
        y += line_height

    if emoji_side:
        emoji_font = ImageFont.truetype(emoji_path, emoji_side)
        emoji_y = (block_height - emoji_side) // 2 - int(size * 0.1)
        draw.text((0, emoji_y), emoji, font=emoji_font, embedded_color=True)
        draw.text(
            (block_width - emoji_side, emoji_y), emoji,
            font=emoji_font, embedded_color=True,
        )
    return layer


def _shaped(text: str) -> str:
    """Arabic-shape and bidi-reorder when the text needs it."""
    if not any("\u0600" <= ch <= "\u06ff" for ch in text):
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display

    return get_display(arabic_reshaper.reshape(text))


__all__ = ["burn_hook", "hook_phrase"]
