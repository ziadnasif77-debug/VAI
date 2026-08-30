"""V2-P2: what a moment is doing, second by second.

The rule the phase classifier answers to is the owner's: when the system does
not know what happened, it says so. So these tests are as much about what it
refuses to name as about what it names.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from backend.moments.phases import (
    FLAT_SPREAD,
    MIN_PHASE_SECONDS,
    MomentPhase,
    classify_phases,
    phase_named,
)

pytestmark = pytest.mark.unit


class _Lanes:
    """A reader over lanes written by hand, so a shape can be stated exactly."""

    def __init__(self, intensity, *, hz=2, dead=None, level="high"):
        self.media_id = "media-aaaaaaaaaaaa"
        self.hz = hz
        self.duration_s = len(intensity) / hz
        self._lanes = {
            "intensity": list(intensity),
            "dead_zones": list(dead or [0.0] * len(intensity)),
        }
        self._level = level

    def _index(self, t):
        return max(0, min(len(self._lanes["intensity"]) - 1, int(t * self.hz)))

    def lane(self, name):
        return self._lanes[name]

    def window(self, name, start, end):
        a, b = self._index(start), self._index(end)
        return self._lanes[name][a : b + 1] if b > a else [self._lanes[name][a]]

    def value_at(self, name, seconds):
        return self._lanes[name][self._index(seconds)]

    def intensity_between(self, start, end):
        window = self.window("intensity", start, end)
        return sum(window) / len(window)

    def level_for(self, start, end):
        return self._level

    def shape(self, *, min_segment=None):
        return ()

    def summary(self):
        return []


def _arc(*, quiet=20, rise=20, peak=10, fall=20):
    """A shape with a real story in it: flat, climbing, peaking, coming down."""
    return (
        [0.05] * quiet
        + [0.05 + 0.75 * (i + 1) / rise for i in range(rise)]
        + [0.9] * peak
        + [0.9 - 0.8 * (i + 1) / fall for i in range(fall)]
    )


class TestItNamesWhatItMeasures:
    def test_an_arc_becomes_a_build_a_payoff_and_a_come_down(self) -> None:
        phases = classify_phases(
            _Lanes(_arc()), start_seconds=0.0, end_seconds=35.0
        )

        names = [phase.name for phase in phases]
        assert "payoff" in names
        assert names.index("payoff") > 0, "something leads into it"
        assert any(name in ("setup", "anticipation", "escalation") for name in names)
        assert phase_named(phases, "payoff").seconds >= MIN_PHASE_SECONDS

    def test_the_payoff_is_a_plateau_and_not_a_sample(self) -> None:
        # Measured as one half-second bin, a "payoff" is a sample of a payoff.
        # Every consumer that anchors to it would anchor to noise.
        phases = classify_phases(_Lanes(_arc(peak=16)), start_seconds=0.0, end_seconds=38.0)

        assert phase_named(phases, "payoff").seconds >= 4.0

    def test_the_phases_cover_the_moment_exactly(self) -> None:
        phases = classify_phases(_Lanes(_arc()), start_seconds=10.0, end_seconds=45.0)

        assert phases[0].start_seconds == pytest.approx(10.0)
        assert phases[-1].end_seconds == pytest.approx(45.0, abs=0.6)
        for before, after in pairwise(phases):
            assert before.end_seconds == pytest.approx(after.start_seconds)

    def test_no_two_neighbours_share_a_name(self) -> None:
        phases = classify_phases(_Lanes(_arc()), start_seconds=0.0, end_seconds=35.0)

        names = [phase.name for phase in phases]
        assert all(a != b for a, b in pairwise(names))


class TestItRefusesToNarrate:
    def test_a_flat_moment_is_unknown_not_a_story(self) -> None:
        # The lanes cannot separate a build from a payoff here. Naming one
        # would be inventing the only interesting thing about the clip.
        phases = classify_phases(
            _Lanes([0.4] * 60), start_seconds=0.0, end_seconds=30.0
        )

        assert [phase.name for phase in phases] == ["unknown"]
        assert phases[0].confidence == 0.0

    def test_a_barely_moving_moment_is_still_unknown(self) -> None:
        drift = [0.40 + 0.001 * i for i in range(60)]
        assert max(drift) - min(drift) < FLAT_SPREAD

        phases = classify_phases(_Lanes(drift), start_seconds=0.0, end_seconds=30.0)

        assert [phase.name for phase in phases] == ["unknown"]

    def test_with_no_reader_it_says_unknown_rather_than_guessing(self) -> None:
        phases = classify_phases(None, start_seconds=0.0, end_seconds=30.0)

        assert [phase.name for phase in phases] == ["unknown"]
        assert phases[0].seconds == pytest.approx(30.0)

    def test_a_dead_screen_is_dead_and_carries_no_confidence(self) -> None:
        lanes = _Lanes([0.05] * 60, dead=[1.0] * 60)

        phases = classify_phases(lanes, start_seconds=0.0, end_seconds=30.0)

        assert [phase.name for phase in phases] == ["dead"]
        assert phases[0].confidence == 0.0

    def test_a_long_flat_tail_is_not_called_a_reaction(self) -> None:
        # Ninety seconds of nothing after the payoff is a badly-formed moment,
        # and the Critic reads these to decide what to trim. Dressing it as a
        # story beat would hide exactly the thing worth trimming.
        shape = _arc(quiet=10, rise=10, peak=6, fall=8) + [0.03] * 160
        phases = classify_phases(_Lanes(shape), start_seconds=0.0, end_seconds=97.0)

        tail = phases[-1]
        assert tail.name in ("unknown", "dead")
        assert tail.seconds > 30.0
        reaction = phase_named(phases, "reaction")
        assert reaction is None or reaction.seconds < 30.0


class TestConfidenceMeansSomething:
    def test_a_tall_peak_is_surer_than_a_bump(self) -> None:
        tall = classify_phases(
            _Lanes(_arc()), start_seconds=0.0, end_seconds=35.0
        )
        bump = classify_phases(
            _Lanes([0.30] * 20 + [0.30 + 0.2 * (i + 1) / 10 for i in range(10)]
                   + [0.5] * 10 + [0.3] * 20),
            start_seconds=0.0,
            end_seconds=30.0,
        )

        assert phase_named(tall, "payoff").confidence > phase_named(bump, "payoff").confidence

    def test_a_level_that_agrees_raises_it(self) -> None:
        hot = classify_phases(
            _Lanes(_arc(), level="climax"), start_seconds=0.0, end_seconds=35.0
        )
        mild = classify_phases(
            _Lanes(_arc(), level="normal"), start_seconds=0.0, end_seconds=35.0
        )

        assert phase_named(hot, "payoff").confidence > phase_named(mild, "payoff").confidence


class TestContextComesFromTheShape:
    """Selection reads the semantics: how much footage a moment gets is its
    own build-up and come-down, not a constant chosen by its type."""

    def _moment(self, start=40.0, end=45.0):
        from backend.core.models.enums import MomentType
        from backend.moments.formation import Moment

        return Moment(
            media_id="media-aaaaaaaaaaaa",
            moment_type=MomentType.CHAOS,
            start_seconds=start,
            end_seconds=end,
            events=(),
        )

    def test_the_lead_in_reaches_back_to_where_the_build_began(self, config) -> None:
        from backend.moments.context import ExpansionSources, expand

        # Quiet until 20s, climbing to a peak at 40-45s, then down.
        lanes = _Lanes([0.05] * 40 + [0.05 + 0.85 * (i + 1) / 40 for i in range(40)]
                       + [0.9] * 10 + [0.1] * 40)
        moment = self._moment()

        with_shape = expand(
            [moment],
            config.moments.context,
            ExpansionSources(duration_seconds=65.0, reader=lanes),
        )[0]

        assert with_shape.metadata["roll_from"] == "shape"
        assert with_shape.context_start < moment.start_seconds

    def test_without_a_reader_it_expands_the_way_it_always_did(self, config) -> None:
        from backend.moments.context import ExpansionSources, expand

        moment = self._moment()

        plain = expand(
            [moment], config.moments.context, ExpansionSources(duration_seconds=65.0)
        )[0]

        assert plain.metadata["roll_from"] == "type"
        assert plain.context_start < moment.start_seconds

    def test_the_ceilings_still_hold(self, config) -> None:
        from backend.moments.context import ExpansionSources, expand

        # A build that starts far earlier than §29 would ever allow.
        lanes = _Lanes([0.05] * 4 + [0.5] * 116 + [0.9] * 10)
        moment = self._moment(start=58.0, end=62.0)

        expanded = expand(
            [moment],
            config.moments.context,
            ExpansionSources(duration_seconds=65.0, reader=lanes),
        )[0]

        pre = moment.start_seconds - expanded.context_start
        assert pre <= config.moments.context.max_pre_roll_seconds + 1e-6


class TestTheHonestName:
    def test_the_label_says_unknown_rather_than_unexpected(self) -> None:
        # An "unexpected event" claims a surprise the detectors never
        # established; what the correlator means is that it could not name
        # what several sources agreed was there. It is 59% of the events on
        # the gate session -- the majority of what the system "knows".
        from backend.core.models.enums import GameEventType

        assert GameEventType.UNKNOWN_EVENT.value == "unknown_event"
        assert not hasattr(GameEventType, "UNEXPECTED_EVENT")


def test_a_phase_reports_itself_for_the_record() -> None:
    phase = MomentPhase("payoff", 10.0, 14.25, 0.812)

    assert phase.as_dict() == {
        "name": "payoff",
        "start_seconds": 10.0,
        "end_seconds": 14.25,
        "confidence": 0.812,
    }
