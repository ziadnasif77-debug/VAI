"""Locating the thing a moment is about (V2-P2.2).

The case this layer exists for is a real one on this machine. A moment runs
from 2935.0 to 3112.9 — nearly three minutes — and is labelled `victory`. For
most of this project's life there was nothing to say about the inside of it,
because the loader was dropping 56 % of event references and the three that
survived all claimed the moment's own span. With them restored the moment
carries thirteen events, and the victory sits at [3000.8, 3011.7] with an
importance of 0.901: 43 % of the way in, occupying 6 % of the span.

`TestTheVictoryThatLookedLikeThreeMinutes` is that case, and the assertion that
matters most in it is the negative one — the resolution is the victory event's
own end, and **not** a timestamp derived from the moment's midpoint, its
duration, or the fact that the word "victory" appears.

The other rule these tests hold is that a boundary with no evidence is not
produced. An earlier version returned an aftermath at the moment's end
whenever a resolution existed, at a confidence of 0.5 and a reason reading
"the moment runs on after the resolution" — which is the raw end wearing a
boundary's name. It added a label without adding a fact, and
`TestABoundaryWithoutEvidenceIsNotInvented` is what stops it coming back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from backend.core.models.enums import GameEventType, MomentType
from backend.editorial.event_span import (
    SETTLED,
    WORTH_LOCATING,
    EditorialEventSpan,
    read,
)
from backend.gaming.correlation import GameEvent
from backend.moments.formation import Moment

pytestmark = pytest.mark.unit


def _event(kind: GameEventType, start: float, end: float, importance: float = 0.4,
           confidence: float = 0.9) -> GameEvent:
    return GameEvent(
        event_type=kind,
        start_seconds=start,
        end_seconds=end,
        confidence=confidence,
        importance=importance,
        sources=("audio",),
    )


def _moment(events, start: float = 2935.0, end: float = 3112.9) -> Moment:
    return Moment(
        media_id="media-1",
        moment_type=MomentType.VICTORY,
        start_seconds=start,
        end_seconds=end,
        events=tuple(events),
        context_start=start,
        context_end=end,
        metadata={"id": "mom-1"},
    )


@dataclass
class _Said:
    start: float
    end: float


@dataclass
class _After:
    said: tuple = field(default_factory=tuple)


class _Lanes:
    """Tension held high, then released at a nameable second."""

    def __init__(self, falls_at: float | None):
        self._falls_at = falls_at

    def window(self, lane: str, start: float, end: float):
        if self._falls_at is None or lane != "tension":
            return [0.5] * max(1, int(end - start))
        return [
            0.8 if at < self._falls_at else 0.1
            for at in range(int(start), max(int(start) + 1, int(end)))
        ]


class TestTheVictoryThatLookedLikeThreeMinutes:
    """The 177.9-second moment with an 11-second victory inside it."""

    @staticmethod
    def _real_shape():
        return _moment(
            [
                _event(GameEventType.UNKNOWN_EVENT, 2935.0, 2944.1, 0.488),
                _event(GameEventType.UNKNOWN_EVENT, 2950.8, 2957.2, 0.488),
                _event(GameEventType.NEAR_DEATH, 2976.1, 2979.1, 0.433, 0.67),
                _event(GameEventType.VICTORY, 3000.8, 3011.7, 0.901, 0.89),
                _event(GameEventType.UNKNOWN_EVENT, 3026.2, 3040.1, 0.428),
                _event(GameEventType.NEAR_DEATH, 3101.2, 3112.9, 0.522, 0.74),
            ]
        )

    def test_the_resolution_is_the_victory_event_s_own_end(self) -> None:
        span = read(self._real_shape())

        assert span.resolution is not None
        assert span.resolution.seconds == 3011.7
        assert span.resolution.source == "events"

    def test_it_is_not_derived_from_the_moment_s_shape(self) -> None:
        """The negative assertion this whole layer turns on. None of these is
        the answer, and each is what an invented boundary would have been."""
        moment = self._real_shape()
        span = read(moment)
        midpoint = (moment.start_seconds + moment.end_seconds) / 2

        assert span.resolution.seconds != pytest.approx(midpoint)
        assert span.resolution.seconds != moment.end_seconds
        assert span.resolution.seconds != moment.start_seconds

    def test_the_action_is_the_heaviest_event_not_the_first(self) -> None:
        span = read(self._real_shape())

        assert span.action.seconds == 3000.8, "the victory, at importance 0.901"
        assert span.onset.seconds == 2935.0, "and the onset is still the first event"

    def test_every_boundary_says_where_it_came_from(self) -> None:
        span = read(self._real_shape())

        for boundary in (span.onset, span.action, span.resolution):
            assert boundary.source
            assert boundary.reason
            assert 0.0 <= boundary.confidence <= 1.0

    def test_the_editorial_span_is_shorter_than_the_raw_one(self) -> None:
        span = read(self._real_shape())

        assert span.raw_duration == pytest.approx(177.9, abs=0.1)
        assert span.editorial_duration < span.raw_duration
        assert span.raw_start == 2935.0, "and the raw span is untouched"
        assert span.raw_end == pytest.approx(3112.9)


class TestAResolutionNeedsEvidence:
    """Rule: the type of a moment is not evidence about its inside."""

    def test_a_victory_labelled_moment_with_no_victory_event_gets_none(self) -> None:
        span = read(
            _moment(
                [
                    _event(GameEventType.COMBAT, 2940.0, 2960.0),
                    _event(GameEventType.COMBAT, 2980.0, 3000.0),
                ]
            )
        )

        assert span.resolution is None
        assert "resolution" in span.unknown

    def test_the_lanes_may_supply_one_the_events_did_not_name(self) -> None:
        moment = _moment(
            [_event(GameEventType.COMBAT, 2940.0, 2960.0)], start=2935.0, end=3000.0
        )
        span = read(moment, reader=_Lanes(falls_at=2970.0))

        assert span.resolution is not None
        assert span.resolution.source == "lanes"
        assert "tension falls" in span.resolution.reason

    def test_a_lane_that_never_settles_supplies_nothing(self) -> None:
        moment = _moment(
            [_event(GameEventType.COMBAT, 2940.0, 2960.0)], start=2935.0, end=3000.0
        )
        assert read(moment, reader=_Lanes(falls_at=None)).resolution is None


class TestABoundaryWithoutEvidenceIsNotInvented:
    def test_no_aftermath_without_something_happening_after(self) -> None:
        """This returned the moment's end at confidence 0.5 in its first
        version -- a label with no fact under it."""
        span = read(TestTheVictoryThatLookedLikeThreeMinutes._real_shape(), after=_After())

        assert span.aftermath is None
        assert "aftermath" in span.unknown

    def test_an_aftermath_is_speech_that_starts_after_the_resolution(self) -> None:
        span = read(
            TestTheVictoryThatLookedLikeThreeMinutes._real_shape(),
            after=_After(said=(_Said(3014.0, 3020.0),)),
        )

        assert span.aftermath is not None
        assert span.aftermath.seconds == 3014.0
        assert span.aftermath.source == "audio"

    def test_speech_before_the_resolution_is_not_its_aftermath(self) -> None:
        span = read(
            TestTheVictoryThatLookedLikeThreeMinutes._real_shape(),
            after=_After(said=(_Said(2990.0, 2995.0),)),
        )

        assert span.aftermath is None

    def test_an_aftermath_never_appears_without_a_resolution(self) -> None:
        span = read(
            _moment([_event(GameEventType.COMBAT, 2940.0, 2960.0)]),
            after=_After(said=(_Said(3050.0, 3060.0),)),
        )

        assert span.resolution is None
        assert span.aftermath is None


class TestNothingToLocateIsSaidRatherThanFilled:
    def test_a_moment_of_one_event_spanning_it_locates_nothing(self) -> None:
        """A moment formed from a single event *is* that event. Splitting it
        into four boundaries would be inventing structure out of rounding, and
        it is also exactly what a pre-V2-P2.0 load looked like."""
        span = read(_moment([_event(GameEventType.VICTORY, 2935.0, 3112.9, 0.9)]))

        assert not span.is_located
        assert span.unknown == ("onset", "action", "resolution", "aftermath")
        assert span.decisive_seconds is None

    def test_an_event_filling_almost_all_of_the_moment_is_not_located(self) -> None:
        moment = _moment([_event(GameEventType.VICTORY, 2936.0, 3110.0, 0.9)])
        assert (3110.0 - 2936.0) > (3112.9 - 2935.0) * (1.0 - WORTH_LOCATING)
        assert not read(moment).is_located

    def test_a_moment_with_no_events_locates_nothing(self) -> None:
        assert not read(_moment([])).is_located

    def test_the_editorial_span_falls_back_to_the_raw_one(self) -> None:
        """Every consumer keeps working when nothing could be placed, which is
        what every consumer did before this layer existed."""
        span = read(_moment([]))

        assert span.editorial_start == span.raw_start
        assert span.editorial_end == span.raw_end


class TestTheRawSpanIsNeverReplaced:
    def test_the_raw_boundaries_survive_every_reading(self) -> None:
        moment = TestTheVictoryThatLookedLikeThreeMinutes._real_shape()
        span = read(moment, after=_After(said=(_Said(3014.0, 3020.0),)))

        assert span.raw_start == moment.start_seconds
        assert span.raw_end == moment.end_seconds

    def test_an_empty_span_is_still_readable(self) -> None:
        span = EditorialEventSpan(raw_start=10.0, raw_end=20.0)

        assert span.editorial_start == 10.0
        assert span.editorial_end == 20.0
        assert not span.is_located
        assert span.as_dict()["raw"] == [10.0, 20.0]
