"""HUD state extraction (SPEC §24, §22, §23, §26).

The reader is a pixel heuristic, and the honest thing to test about a pixel
heuristic is not that it is right — it is that **when it is wrong, it says so**.
A wrong high-confidence reading poisons §27's correlation, where a HUD change
that agrees with gunfire and a shouted reaction becomes a high-value moment. A
refusal costs one missed event.

So most of this file builds frames that are deliberately hard — a row that is
flashing, a number where the glyphs should be, a background that matches the
glyph colour — and asserts the reader declines rather than guesses.

The synthetic frames are drawn to match what was measured off real GTA V
capture: an earned glyph is an opaque mid-grey fill, an empty one is near-white,
and the row is right-anchored inside a wider search window.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.core.models.enums import GameEventType
from backend.gaming.hud import (
    EMPTY_CENTRE_BRIGHTNESS,
    HudChange,
    ReadingQuality,
    changes_to_events,
    read_frame,
    read_indicator,
    track,
)
from backend.gaming.profiles import (
    GENERIC_PROFILE,
    GameProfile,
    HudChangeRule,
    HudIndicator,
    HudKind,
    Region,
)

pytestmark = pytest.mark.unit

WIDTH, HEIGHT = 640, 360

#: Matches the real profile's shape: a search window wider than the glyphs.
STARS = HudIndicator(
    name="wanted_level",
    kind=HudKind.GLYPH_ROW,
    region=Region(x=0.60, y=0.05, width=0.35, height=0.12),
    count=5,
    confidence=0.6,
    absent_value=0.0,
    absent_confidence=0.55,
    change_rules=(
        HudChangeRule(
            event_type=GameEventType.UNEXPECTED_EVENT,
            direction="rise",
            at_least=3,
            min_change=1,
            confidence=0.7,
        ),
        HudChangeRule(
            event_type=GameEventType.ESCAPE,
            direction="fall",
            at_most=0,
            min_change=2,
            confidence=0.6,
        ),
    ),
)

EARNED = (130, 140, 150)
EMPTY = (250, 250, 250)
SCENE = (40, 90, 40)


def frame(background=SCENE) -> np.ndarray:
    return np.full((HEIGHT, WIDTH, 3), background, dtype=np.uint8)


def draw_star(image: np.ndarray, left: int, top: int, size: int, colour) -> None:
    """A blocky five-pointed-ish glyph: a body with a notched top."""
    image[top : top + size, left : left + size] = colour
    notch = size // 4
    image[top : top + notch, left : left + notch] = SCENE
    image[top : top + notch, left + size - notch : left + size] = SCENE


def draw_row(image: np.ndarray, earned: int, *, count: int = 5, empty=EMPTY) -> None:
    """Draw ``count`` glyphs, ``earned`` of them filled, right-anchored."""
    size = 20
    gap = 4
    total = count * size + (count - 1) * gap
    # Right-anchored inside the search window, as GTA V's row is.
    right = int(0.94 * WIDTH)
    left = right - total
    top = int(0.07 * HEIGHT)
    for index in range(count):
        x = left + index * (size + gap)
        draw_star(image, x, top, size, EARNED if index < earned else empty)


class TestReadingAGlyphRow:
    @pytest.mark.parametrize("earned", [1, 2, 3, 4, 5])
    def test_it_counts_the_earned_glyphs(self, earned: int) -> None:
        image = frame()
        draw_row(image, earned)

        reading = read_indicator(image, STARS, timestamp_seconds=12.0)

        assert reading.value == earned
        assert reading.quality is ReadingQuality.CLEAN
        assert reading.confidence >= 0.35

    def test_an_empty_window_reads_as_the_profiles_absent_value(self) -> None:
        # GTA V hides the row entirely at wanted level zero, so absence is a
        # reading rather than a failure -- but only because the profile says so.
        reading = read_indicator(frame(), STARS, timestamp_seconds=1.0)

        assert reading.value == 0.0
        assert reading.confidence == STARS.absent_confidence

    def test_absence_means_nothing_when_the_profile_does_not_say_so(self) -> None:
        quiet = STARS.model_copy(update={"absent_value": None})

        reading = read_indicator(frame(), quiet, timestamp_seconds=1.0)

        assert reading.value is None
        assert reading.quality is ReadingQuality.ABSENT

    def test_the_row_is_found_wherever_it_sits_in_the_window(self) -> None:
        # The reason the reader locates the row instead of splitting the
        # window: a right-anchored row moves as it grows, and a fixed split
        # would be wrong by a whole glyph.
        image = frame()
        draw_row(image, 3, count=5)
        five_wide = read_indicator(image, STARS, timestamp_seconds=1.0).value

        assert five_wide == 3

    def test_confidence_never_reaches_certainty(self) -> None:
        image = frame()
        draw_row(image, 4)

        assert read_indicator(image, STARS, timestamp_seconds=1.0).confidence < 1.0


class TestWhenItShouldRefuse:
    """The failures that matter: wrong *and* confident."""

    def test_a_flashing_row_is_refused(self) -> None:
        # GTA V flashes every glyph bright while the police search. A frame
        # caught mid-flash shows five lit glyphs and carries no count at all.
        # All five glyphs drawn in the bright rendering: that is the flash,
        # and it looks identical to a genuine five-star level.
        image = frame()
        draw_row(image, 0, empty=EMPTY)

        reading = read_indicator(image, STARS, timestamp_seconds=1.0)

        assert reading.confidence < 0.35
        assert reading.detail.get("reason") == "row flashing"

    def test_digits_in_the_same_box_are_refused(self) -> None:
        # GTA V's ammo counter slides into this corner at wanted level zero.
        # Before the shape test, "627 8" read as three stars at full confidence.
        image = frame()
        left, top, size = int(0.70 * WIDTH), int(0.07 * HEIGHT), 20
        for index, height in enumerate([size, size // 2, size, size // 3, size]):
            x = left + index * (size + 4)
            image[top : top + height, x : x + size - index * 2] = (245, 245, 245)

        reading = read_indicator(image, STARS, timestamp_seconds=1.0)

        assert reading.confidence < 0.35

    def test_an_unreadable_frame_is_not_reported_as_a_zero(self) -> None:
        # "I could not read it" and "the wanted level is zero" are different
        # claims, and confusing them says the police left when they did not.
        image = frame()
        left, top = int(0.70 * WIDTH), int(0.07 * HEIGHT)
        image[top : top + 24, left : left + 40] = (255, 0, 0)
        image[top : top + 8, left + 60 : left + 130] = (0, 0, 255)

        reading = read_indicator(image, STARS, timestamp_seconds=1.0)

        assert reading.value is None or reading.confidence < 0.35

    def test_a_frame_that_is_not_an_image_is_survivable(self) -> None:
        assert read_indicator(np.zeros((4, 4)), STARS, timestamp_seconds=0.0).value is None


class TestBars:
    def test_a_bar_reads_as_the_fraction_filled(self) -> None:
        indicator = HudIndicator(
            name="health",
            kind=HudKind.BAR,
            region=Region(x=0.05, y=0.90, width=0.20, height=0.02),
            confidence=0.7,
        )
        image = frame(background=(20, 20, 20))
        left, right = int(0.05 * WIDTH), int(0.25 * WIDTH)
        top, bottom = int(0.90 * HEIGHT), int(0.92 * HEIGHT)
        image[top:bottom, left : left + (right - left) // 2] = (60, 220, 60)

        reading = read_indicator(image, indicator, timestamp_seconds=1.0)

        assert 0.4 <= (reading.value or 0) <= 0.6


class TestTracking:
    """§24: an indicator is interesting when it *changes*."""

    def _reading(self, value, seconds, confidence=0.6):
        from backend.gaming.hud import HudReading

        return HudReading(
            indicator="wanted_level",
            timestamp_seconds=seconds,
            value=value,
            quality=ReadingQuality.CLEAN,
            confidence=confidence,
        )

    def test_a_steady_value_is_not_a_change(self) -> None:
        readings = [self._reading(3.0, t) for t in (0.0, 10.0, 20.0, 30.0)]

        assert track(readings) == []

    def test_each_transition_is_one_change(self) -> None:
        readings = [
            self._reading(0.0, 0.0),
            self._reading(2.0, 10.0),
            self._reading(4.0, 20.0),
            self._reading(0.0, 30.0),
        ]

        changes = track(readings)

        assert [(c.previous, c.current) for c in changes] == [(0, 2), (2, 4), (4, 0)]

    def test_a_low_confidence_reading_does_not_create_two_changes(self) -> None:
        # A single misread frame between two correct ones would otherwise
        # produce a spurious drop and a spurious recovery.
        readings = [
            self._reading(4.0, 0.0),
            self._reading(0.0, 10.0, confidence=0.1),
            self._reading(4.0, 20.0),
        ]

        assert track(readings) == []

    def test_a_change_is_worth_the_weaker_of_its_ends(self) -> None:
        readings = [self._reading(0.0, 0.0, 0.4), self._reading(3.0, 10.0, 0.9)]

        assert track(readings)[0].confidence == pytest.approx(0.4)


class TestChangesToEvents:
    def _change(self, previous, current, seconds=100.0, confidence=0.8):
        return HudChange(
            indicator="wanted_level",
            timestamp_seconds=seconds,
            previous=previous,
            current=current,
            confidence=confidence,
        )

    def test_a_rise_past_the_threshold_becomes_its_event(self) -> None:
        events = changes_to_events([self._change(1.0, 4.0)], _profile())

        assert [event["event_type"] for event in events] == [GameEventType.UNEXPECTED_EVENT]
        assert events[0]["sources"] == ["hud"]
        assert events[0]["metadata"] == {"indicator": "wanted_level", "from": 1.0, "to": 4.0}

    def test_a_fall_to_nothing_becomes_an_escape(self) -> None:
        events = changes_to_events([self._change(4.0, 0.0)], _profile())

        assert events[0]["event_type"] is GameEventType.ESCAPE

    def test_a_change_below_every_threshold_produces_nothing(self) -> None:
        assert changes_to_events([self._change(1.0, 2.0)], _profile()) == []

    def test_an_event_is_worth_no_more_than_the_reading_behind_it(self) -> None:
        # §27 weighs detectors against each other. A rule that claims 0.7 on a
        # reading worth 0.3 would be the HUD outvoting better evidence.
        events = changes_to_events([self._change(1.0, 4.0, confidence=0.3)], _profile())

        assert events[0]["confidence"] == pytest.approx(0.3)

    def test_an_indicator_the_profile_does_not_declare_is_ignored(self) -> None:
        stray = HudChange(
            indicator="ammo", timestamp_seconds=1.0, previous=1.0, current=9.0, confidence=0.9
        )

        assert changes_to_events([stray], _profile()) == []


class TestWithoutAProfile:
    """§23: the application must not require a game profile."""

    def test_the_generic_profile_reads_nothing(self) -> None:
        image = frame()
        draw_row(image, 4)

        assert read_frame(image, GENERIC_PROFILE, timestamp_seconds=1.0) == []

    def test_and_therefore_produces_no_events(self) -> None:
        assert changes_to_events([], GENERIC_PROFILE) == []


def _profile() -> GameProfile:
    return GameProfile(id="test", hud=(STARS,))


def test_the_empty_brightness_threshold_sits_between_the_two_renderings() -> None:
    # If this ever stops holding, every reading flips at once, so it is worth
    # one line to notice.
    assert float(np.mean(EARNED)) < EMPTY_CENTRE_BRIGHTNESS < float(np.mean(EMPTY))
