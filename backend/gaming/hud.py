"""Reading a game's HUD as state over time (SPEC §24, §22, §26, §111).

The events this pipeline is best at are the ones something *says*: a kill feed
line, "MISSION PASSED", a victory banner. OCR reads those, and §25 covers it.

This module is for the other kind — the state a game shows without words. In
GTA V the single strongest excitement signal is the row of wanted stars in the
corner, and no amount of OCR will read it, because it is five glyphs and no
text. §24 lists a dozen indicators of this shape across games: health, armour,
ammo, score, round, team status, boss health.

The design follows from one observation: **a HUD indicator is only interesting
when it changes.** A four-star wanted level held for six minutes is not four
minutes of event; the moment it went from two to four is. So a reader produces
a *series* of readings, and :func:`changes_to_events` turns the transitions
into the §26 event schema. The steady state is context, not content.

Three things this deliberately does not do:

**It does not decide alone.** Every reading carries a confidence, and a reading
the reader is unsure of stays low. §27 merges detectors, and a wanted-level
change that coincides with gunfire and a shouted reaction is worth far more
than either alone. A confident-but-wrong HUD reader would poison that.

**It does not assume a fixed rectangle.** A profile declares roughly where an
indicator lives; the reader *finds* it inside that window. GTA V's star row is
right-anchored and slides as it grows, and a fixed five-cell split misreads it
by a whole star. Anchor first, then classify.

**It does not require a profile.** A game with no HUD declaration produces no
readings and no events, and the rest of the pipeline is unchanged (§23).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.gaming.profiles import GameProfile, HudIndicator, HudKind

logger = get_logger("gaming.hud", LogChannel.PIPELINE)

#: A pixel differs from the local background by at least this much (0-255,
#: mean across channels) before it counts as part of a drawn glyph.
INK_THRESHOLD: Final[float] = 40.0

#: Above this brightness (0-255) a glyph's centre is the *empty* rendering.
#:
#: This is the fourth discriminator tried against real GTA V frames, and the
#: first that works. An earned star is a mid-grey opaque fill -- it reads dark
#: against sky and light against night, which is why brightness alone looked
#: hopeless. An empty star is near-white in every scene. So the test is not
#: "bright or dark" and not "differs from the background", but "is it white":
#: the two states are both opaque, and only one of them is white.
EMPTY_CENTRE_BRIGHTNESS: Final[float] = 200.0

#: A located glyph row must fill at least this fraction of the search window's
#: width, or what was found is more likely a different HUD element that moved
#: into the same space -- in GTA V, the ammo counter when the wanted level is
#: zero.
MIN_ROW_EXTENT: Final[float] = 0.35

#: Cells must be within this fraction of each other in ink coverage for the
#: reading to be treated as clean. Glyphs of one kind are drawn identically;
#: digits are not.
CELL_UNIFORMITY: Final[float] = 0.55

#: Mean pairwise agreement between the cells' ink masks, below which the row is
#: not one glyph repeated. Real GTA V frames: star rows sit above 0.80, the
#: ammo counter below 0.70.
GLYPH_AGREEMENT: Final[float] = 0.75


class ReadingQuality(str, Enum):
    """How much to trust one reading."""

    CLEAN = "clean"
    #: Read, but something was off: uneven cells, a partial row, a marginal
    #: centre. The value is reported and the confidence is low.
    UNCERTAIN = "uncertain"
    #: Nothing of the expected shape was in the window. Not an error -- an
    #: indicator that is not on screen is the commonest state for most of them.
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class HudReading:
    """One indicator, read from one frame.

    Attributes:
        value: what was read. A glyph count for ``glyph_row``, a 0-1 fraction
            for ``bar``. ``None`` when the indicator was not on screen.
        confidence: how much this reading is worth to §27. Never 1.0 -- a
            pixel heuristic that claims certainty is lying.
    """

    indicator: str
    timestamp_seconds: float
    value: float | None
    quality: ReadingQuality
    confidence: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class HudChange:
    """A transition between two readings of the same indicator (§24, §26)."""

    indicator: str
    timestamp_seconds: float
    previous: float | None
    current: float | None
    confidence: float

    @property
    def rising(self) -> bool:
        return (self.current or 0.0) > (self.previous or 0.0)

    @property
    def magnitude(self) -> float:
        return abs((self.current or 0.0) - (self.previous or 0.0))


def read_indicator(
    frame: Any, indicator: HudIndicator, *, timestamp_seconds: float
) -> HudReading:
    """Read one indicator from one decoded frame.

    Args:
        frame: an ``(h, w, 3)`` RGB array. Typed loosely so this module does
            not make numpy a hard import for callers that never read a HUD.
    """
    if indicator.kind is HudKind.GLYPH_ROW:
        return _read_glyph_row(frame, indicator, timestamp_seconds)
    if indicator.kind is HudKind.BAR:
        return _read_bar(frame, indicator, timestamp_seconds)
    return HudReading(
        indicator=indicator.name,
        timestamp_seconds=timestamp_seconds,
        value=None,
        quality=ReadingQuality.ABSENT,
        detail={"unsupported_kind": indicator.kind.value},
    )


def read_frame(
    frame: Any, profile: GameProfile, *, timestamp_seconds: float
) -> list[HudReading]:
    """Read every indicator this profile declares (§22, §23).

    A profile with no HUD section returns nothing, which is the unknown-game
    path and costs nothing.
    """
    return [
        read_indicator(frame, indicator, timestamp_seconds=timestamp_seconds)
        for indicator in profile.hud
    ]


def track(readings: Sequence[HudReading], *, min_confidence: float = 0.35) -> list[HudChange]:
    """Turn a series of readings into the transitions between them.

    Low-confidence readings are skipped rather than treated as a change,
    because a single misread frame between two correct ones would otherwise
    produce two events -- a spurious drop and a spurious recovery.
    """
    ordered = sorted(
        (r for r in readings if r.confidence >= min_confidence),
        key=lambda r: r.timestamp_seconds,
    )
    changes: list[HudChange] = []
    by_indicator: dict[str, HudReading] = {}
    for reading in ordered:
        previous = by_indicator.get(reading.indicator)
        by_indicator[reading.indicator] = reading
        if previous is None or previous.value == reading.value:
            continue
        changes.append(
            HudChange(
                indicator=reading.indicator,
                timestamp_seconds=reading.timestamp_seconds,
                previous=previous.value,
                current=reading.value,
                # Both ends of a transition have to be right for it to be one.
                confidence=min(previous.confidence, reading.confidence),
            )
        )
    return changes


def changes_to_events(
    changes: Sequence[HudChange], profile: GameProfile
) -> list[dict[str, Any]]:
    """Map transitions onto §26 event dictionaries, per the profile's rules.

    Returns plain dictionaries rather than model instances: the caller owns id
    allocation and persistence, and this module stays free of the database.
    """
    events: list[dict[str, Any]] = []
    indicators = {item.name: item for item in profile.hud}
    for change in changes:
        indicator = indicators.get(change.indicator)
        if indicator is None:
            continue
        rule = indicator.rule_for(change)
        if rule is None:
            continue
        events.append(
            {
                "event_type": rule.event_type,
                "start_seconds": change.timestamp_seconds,
                "end_seconds": change.timestamp_seconds + rule.duration_seconds,
                # A rule is worth what it is worth; a reading is worth what it
                # is worth. The event cannot exceed either (§27).
                "confidence": round(min(rule.confidence, change.confidence), 4),
                "sources": ["hud"],
                "metadata": {
                    "indicator": change.indicator,
                    "from": change.previous,
                    "to": change.current,
                },
            }
        )
    return events


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _read_glyph_row(frame: Any, indicator: HudIndicator, timestamp: float) -> HudReading:
    """Count the *earned* glyphs in a row of identical ones.

    GTA V's wanted stars are the case this was written against, and they took
    four attempts, each of which is a comment somewhere below. What finally
    holds: an earned star is a mid-grey opaque fill that reads light against
    night and dark against sky, an empty one is near-white, and the row is a
    single glyph repeated -- so the test is "is this cell white", checked only
    after the row has been located and confirmed to be one glyph repeated.

    What does not hold, and cannot: a count read from a frame captured while
    the row is flashing. That is reported as unreadable rather than guessed.
    """
    import numpy as np

    absent = HudReading(
        indicator=indicator.name,
        timestamp_seconds=timestamp,
        value=None,
        quality=ReadingQuality.ABSENT,
    )

    window = _crop(frame, indicator)
    if window is None or window.size == 0:
        return absent

    located = _locate_row(window, indicator.count)
    if located is None:
        # Nothing of the right shape here. For a wanted-level row that means
        # zero stars, which is a real reading and not a failure -- so the
        # profile decides whether absence has a value (§24).
        if indicator.absent_value is None:
            return absent
        return HudReading(
            indicator=indicator.name,
            timestamp_seconds=timestamp,
            value=indicator.absent_value,
            quality=ReadingQuality.CLEAN,
            confidence=indicator.absent_confidence,
            detail={"reason": "no glyph row found"},
        )

    left, right, top, bottom = located
    row = window[top:bottom, left:right]
    cells = np.array_split(row, indicator.count, axis=1)

    filled = 0
    coverage: list[float] = []
    brightnesses: list[float] = []
    for index, cell in enumerate(cells):
        height, width, _ = cell.shape
        if height < 4 or width < 4:
            return absent

        # The scene behind this glyph, sampled from a band just above the
        # located row -- outside every glyph shape, and close enough to share
        # whatever is behind them. Sampling the cell's own corners fails here:
        # the row is located tightly, so a five-pointed star's arms reach them.
        background = _behind(window, left, right, top, index, indicator.count)
        distance = np.abs(cell - background).mean(axis=2)
        coverage.append(float((distance > INK_THRESHOLD).mean()))

        centre = cell[
            int(height * 0.40) : max(int(height * 0.62), int(height * 0.40) + 1),
            int(width * 0.38) : max(int(width * 0.62), int(width * 0.38) + 1),
        ]
        brightness = float(centre.reshape(-1, 3).mean())
        brightnesses.append(brightness)
        # Drawn, distinct from the scene, and not white: an earned glyph.
        against = float(np.abs(centre.mean(axis=(0, 1)) - background).mean())
        if (coverage[-1] > 0.05 or against > 12.0) and brightness < EMPTY_CENTRE_BRIGHTNESS:
            filled += 1

    agreement = _glyph_agreement(cells)
    if agreement < GLYPH_AGREEMENT:
        # A row of one glyph repeated looks the same in every cell; "627 8"
        # does not. Coverage alone does not separate them -- digits can be as
        # evenly inked as stars -- but their *shapes* never agree. Without
        # this, GTA V's ammo counter reads as three wanted stars at full
        # confidence whenever the wanted level is zero, which is most of the
        # time and is the worst kind of wrong: confident.
        #
        # Note what this does *not* return: ``absent_value``. Something was
        # located and could not be read, which is a different claim from "the
        # indicator is not on screen". Reporting an unreadable frame as a
        # confident zero would say the police left when the truth is that a
        # lamp post was in the way.
        return HudReading(
            indicator=indicator.name,
            timestamp_seconds=timestamp,
            value=None,
            quality=ReadingQuality.UNCERTAIN,
            confidence=round(indicator.confidence * 0.2, 4),
            detail={"reason": "cells are not the same glyph", "agreement": round(agreement, 3)},
        )

    if _is_flashing(brightnesses):
        # GTA V flashes the whole row while the police are searching: every
        # glyph goes bright, then every glyph goes empty, about twice a second.
        # A frame caught mid-flash carries no count at all -- and the flash is
        # itself the interesting state, so it is reported rather than guessed
        # at. This cost three wrong discriminators before it was understood.
        return HudReading(
            indicator=indicator.name,
            timestamp_seconds=timestamp,
            value=None,
            quality=ReadingQuality.UNCERTAIN,
            confidence=indicator.confidence * 0.25,
            detail={
                "reason": "row flashing",
                "centre_brightness": [round(value, 1) for value in brightnesses],
            },
        )

    quality, confidence = _grade(coverage, brightnesses, indicator)
    return HudReading(
        indicator=indicator.name,
        timestamp_seconds=timestamp,
        value=float(filled),
        quality=quality,
        confidence=confidence,
        detail={
            "coverage": [round(value, 3) for value in coverage],
            "centre_brightness": [round(value, 1) for value in brightnesses],
        },
    )


def _read_bar(frame: Any, indicator: HudIndicator, timestamp: float) -> HudReading:
    """Read a horizontal fill bar as a 0-1 fraction (health, armour, boss)."""
    import numpy as np

    window = _crop(frame, indicator)
    if window is None or window.size == 0:
        return HudReading(
            indicator=indicator.name,
            timestamp_seconds=timestamp,
            value=None,
            quality=ReadingQuality.ABSENT,
        )

    # A bar is measured against *itself*, not against a background: its region
    # is the bar, so there is no scene in the corners to sample. Sampling them
    # anyway reads a half-full green bar on black as full, because the mean of
    # "green" and "black" is far enough from both that every column counts.
    columns_rgb = window.mean(axis=0)
    fill = columns_rgb[0]
    matches = np.abs(columns_rgb - fill).mean(axis=1) <= INK_THRESHOLD

    filled = 0
    for value in matches:
        if not value:
            break
        filled += 1
    if filled == len(matches):
        # Every column matches the first, so either the bar is genuinely full
        # or the region does not contain a bar at all. Both are reported as
        # full; the profile's confidence is what a caller weighs.
        return HudReading(
            indicator=indicator.name,
            timestamp_seconds=timestamp,
            value=1.0,
            quality=ReadingQuality.CLEAN,
            confidence=round(indicator.confidence * 0.8, 4),
            detail={"contiguous": 1.0},
        )

    fraction = filled / len(matches)
    contiguous = filled / max(int(matches.sum()), 1)
    quality = ReadingQuality.CLEAN if contiguous > 0.85 else ReadingQuality.UNCERTAIN
    confidence = indicator.confidence * (1.0 if quality is ReadingQuality.CLEAN else 0.5)
    return HudReading(
        indicator=indicator.name,
        timestamp_seconds=timestamp,
        value=round(fraction, 3),
        quality=quality,
        confidence=round(confidence, 4),
        detail={"contiguous": round(contiguous, 3)},
    )


# ---------------------------------------------------------------------------
# Pixel helpers
# ---------------------------------------------------------------------------


def _crop(frame: Any, indicator: HudIndicator) -> Any:
    import numpy as np

    array = np.asarray(frame)
    if array.ndim != 3 or array.shape[2] < 3:
        return None
    height, width, _ = array.shape
    left, top, right, bottom = indicator.region.to_pixels(width, height)
    return array[top:bottom, left:right, :3].astype(float)


def _background(cell: Any) -> Any:
    """The scene behind a glyph, sampled from corners no glyph shape reaches."""
    import numpy as np

    height, width, _ = cell.shape
    band = max(2, int(height * 0.15))
    side = max(2, int(width * 0.14))
    corners = np.concatenate(
        [
            cell[:band, :side].reshape(-1, 3),
            cell[:band, -side:].reshape(-1, 3),
        ]
    )
    return corners.mean(axis=0)


def _behind(window: Any, left: int, right: int, top: int, index: int, count: int) -> Any:
    """The scene behind cell ``index``, sampled from just above the row.

    Above rather than beside: the located row is tight to the glyphs, so a
    star's arms reach the cell corners, and a "background" sample taken there
    is partly the glyph itself.
    """

    span = (right - left) / count
    x0 = int(left + index * span)
    x1 = max(int(left + (index + 1) * span), x0 + 1)
    band = max(2, (right - left) // (count * 3))
    y0 = max(0, top - band)
    strip = window[y0:top, x0:x1]
    if strip.size == 0:
        strip = window[top : top + 2, x0:x1]
    return strip.reshape(-1, 3).mean(axis=0)


def _locate_row(window: Any, count: int) -> tuple[int, int, int, int] | None:
    """Find the glyph row inside the search window.

    The reason this exists rather than trusting the declared rectangle: GTA V's
    wanted row is right-anchored and grows leftwards, so its left edge moves by
    a whole glyph between five stars and three. Splitting the *window* into
    five gives a reading that is wrong by one star -- silently, and in the
    direction that matters.
    """
    import numpy as np
    from scipy.ndimage import uniform_filter

    if window.shape[0] < 6 or window.shape[1] < 6 * count:
        return None

    # The HUD is drawn over the scene, so what differs from a blurred copy of
    # the window is the overlay rather than whatever is behind it.
    smooth = uniform_filter(window, size=(21, 21, 1))
    edge = np.abs(window - smooth).mean(axis=2)
    if edge.max() < INK_THRESHOLD * 0.25:
        return None

    hot = edge > edge.max() * 0.20
    columns = np.where(hot.mean(axis=0) > 0.10)[0]
    rows = np.where(hot.mean(axis=1) > 0.10)[0]
    if columns.size == 0 or rows.size == 0:
        return None

    left, right = int(columns[0]), int(columns[-1]) + 1
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    if (right - left) < MIN_ROW_EXTENT * window.shape[1]:
        return None
    if (right - left) < 4 * count or (bottom - top) < 4:
        return None
    return left, right, top, bottom


def _glyph_agreement(cells: Sequence[Any]) -> float:
    """How alike the cells are, as shapes.

    Each cell's ink is reduced to a small binary mask and compared with every
    other. One glyph repeated agrees with itself; a number does not agree with
    the next digit along.
    """
    import numpy as np

    masks = []
    for cell in cells:
        height, width, _ = cell.shape
        if height < 4 or width < 4:
            return 0.0
        grey = cell.mean(axis=2)
        mask = grey > (grey.min() + grey.max()) / 2
        # An 8x8 signature: enough to tell a star from a 7, cheap enough to do
        # per frame, and coarse enough to survive a pixel of misalignment.
        rows = np.array_split(mask, 8, axis=0)
        signature = np.array(
            [[block.mean() for block in np.array_split(r, 8, axis=1)] for r in rows]
        )
        masks.append(signature > 0.5)

    scores = [
        float((masks[i] == masks[j]).mean())
        for i in range(len(masks))
        for j in range(i + 1, len(masks))
    ]
    return sum(scores) / len(scores) if scores else 0.0


def _is_flashing(brightness: Sequence[float]) -> bool:
    """Whether every glyph is in the same extreme state.

    A real wanted level of 5 and a flash frame both show five lit glyphs; the
    difference is that a flash also drives them all *white*, which an earned
    glyph never is. Five identical near-white readings are therefore the flash,
    not a five-star level -- and five identical readings of any kind carry no
    information about a count that is supposed to be partial.
    """
    if len(brightness) < 3:
        return False
    spread = max(brightness) - min(brightness)
    return spread < 10.0 and min(brightness) > EMPTY_CENTRE_BRIGHTNESS


def _grade(
    coverage: Sequence[float], brightnesses: Sequence[float], indicator: HudIndicator
) -> tuple[ReadingQuality, float]:
    """How much this reading is worth (§24: confidence-based).

    Two things make a reading doubtful: cells that are not drawn alike, which
    means the row is probably not the glyphs we think it is; and centres that
    sit near the filled/hollow boundary, which means the classification could
    have gone either way.
    """
    drawn = [value for value in coverage if value > 0.05]
    if not drawn:
        return ReadingQuality.UNCERTAIN, indicator.confidence * 0.3

    spread = (max(drawn) - min(drawn)) / max(max(drawn), 1e-6)
    margin = min(abs(value - EMPTY_CENTRE_BRIGHTNESS) for value in brightnesses)

    quality = ReadingQuality.CLEAN
    confidence = indicator.confidence
    if spread > CELL_UNIFORMITY:
        # Glyphs of one kind are drawn identically. Digits are not, and in GTA
        # V the ammo counter slides into this box when the wanted level is zero.
        quality = ReadingQuality.UNCERTAIN
        confidence *= 0.4
    if margin < 20.0:
        quality = ReadingQuality.UNCERTAIN
        confidence *= 0.6
    return quality, round(confidence, 4)


__all__ = [
    "CELL_UNIFORMITY",
    "EMPTY_CENTRE_BRIGHTNESS",
    "GLYPH_AGREEMENT",
    "INK_THRESHOLD",
    "MIN_ROW_EXTENT",
    "HudChange",
    "HudReading",
    "ReadingQuality",
    "changes_to_events",
    "read_frame",
    "read_indicator",
    "track",
]
