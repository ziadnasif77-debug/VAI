"""V2-P6: three edits from the same moments, and a judge that can be argued with.

The optimiser produced exactly one plan for the whole life of this project:
optimal by its own objective and unfalsifiable, because nothing else was ever
built to compare it with. These tests care about two things -- that the
alternatives are genuinely different, and that the choice between them is
reproducible. A judge whose answer moves between runs is not a judge.
"""

from __future__ import annotations

import pytest

from backend.narrative.judge import AXIS_WEIGHTS, PlanScore, best, judge
from backend.narrative.plans import PROFILES, PlanProfile

pytestmark = pytest.mark.unit


class _Moment:
    def __init__(self, start, end, kind="chaos", score=0.5, media="media-aaaaaaaaaaaa"):
        self.media_id = media
        self.context_start = start
        self.context_end = end
        self.score = score
        self.moment_type = type("K", (), {"value": kind})()
        self.events = ()

    @property
    def context_duration(self):
        return self.context_end - self.context_start


class _Plan:
    def __init__(self, moments, beats=()):
        self.moments = moments
        self.beats = beats
        self.total_seconds = sum(m.context_duration for m in moments)
        self.is_empty = not moments


class TestTheProfiles:
    def test_there_are_three_and_the_first_is_the_shipped_behaviour(self) -> None:
        # A regression in the optimiser shows up as profile A differing from
        # what it used to produce, which only works if A is unmodified.
        assert len(PROFILES) == 3
        first = PROFILES[0]
        assert (first.entertainment, first.narrative, first.variety) == (1.0, 1.0, 1.0)
        assert first.repetition_penalty == 1.0

    def test_every_profile_explains_itself(self) -> None:
        for profile in PROFILES:
            assert profile.why, f"{profile.id} has no stated reason to exist"

    def test_variety_is_expressed_as_a_penalty_on_sameness(self) -> None:
        # Raising the variety *bonus* 1.6x selected the identical twelve
        # clips on the real session. In a knapsack the way to get more kinds
        # of thing is to make sameness expensive.
        variety = next(profile for profile in PROFILES if profile.name == "variety_forward")

        assert variety.repetition_penalty > 1.0


class TestTheJudgeIsReproducible:
    def _scored(self, totals):
        return [
            (PlanProfile(id=letter, name=letter), _Plan([_Moment(0.0, 10.0)]),
             PlanScore(total=value))
            for letter, value in totals
        ]

    def test_the_same_scores_choose_the_same_winner(self) -> None:
        scored = self._scored([("A", 0.71), ("B", 0.69), ("C", 0.73)])

        assert best(scored)[0].id == "C"
        assert best(list(reversed(scored)))[0].id == "C"

    def test_a_difference_no_viewer_could_see_is_a_tie(self) -> None:
        # Two plans differing in the seventh decimal are the same plan, and
        # letting that decide picked A on one run and C on the next.
        scored = self._scored([("A", 0.7270001), ("C", 0.7270009)])

        assert best(scored)[0].id == "A", "the fixed profile order breaks a tie"

    def test_an_empty_field_has_no_winner(self) -> None:
        assert best([]) is None


class TestTheAxes:
    def _judge(self, plan, reader=None, config=None):
        return judge(plan, reader=reader, config=config)

    def test_every_axis_is_scored_and_explained(self) -> None:
        plan = _Plan(
            [_Moment(0.0, 30.0), _Moment(40.0, 70.0, kind="tension")],
            beats=("hook", "build_up", "climax"),
        )

        score = self._judge(plan)

        for axis in AXIS_WEIGHTS:
            assert 0.0 <= getattr(score, axis) <= 1.0, axis
        assert len(score.why) == len(AXIS_WEIGHTS)
        assert 0.0 <= score.total <= 1.0

    def test_a_long_jump_between_clips_costs_coherence(self) -> None:
        # Chronology guarantees the direction of travel, not the distance.
        near = _Plan([_Moment(0.0, 30.0), _Moment(35.0, 65.0)])
        far = _Plan([_Moment(0.0, 30.0), _Moment(900.0, 930.0)])

        assert self._judge(near).coherence > self._judge(far).coherence

    def test_one_kind_of_thing_costs_variety(self) -> None:
        same = _Plan([_Moment(i * 40.0, i * 40.0 + 30.0) for i in range(5)])
        mixed = _Plan(
            [
                _Moment(i * 40.0, i * 40.0 + 30.0, kind=kind)
                for i, kind in enumerate(("chaos", "tension", "skill", "funny", "rare"))
            ]
        )

        assert self._judge(mixed).variety > self._judge(same).variety

    def test_clips_of_one_length_cost_pacing(self) -> None:
        metronome = _Plan([_Moment(i * 40.0, i * 40.0 + 30.0) for i in range(5)])
        varied = _Plan(
            [
                _Moment(0.0, 12.0),
                _Moment(20.0, 70.0),
                _Moment(80.0, 95.0),
                _Moment(100.0, 160.0),
            ]
        )

        assert self._judge(varied).pacing > self._judge(metronome).pacing

    def test_an_edit_with_no_beats_is_a_list_not_a_story(self) -> None:
        listed = _Plan([_Moment(0.0, 30.0)], beats=())
        shaped = _Plan([_Moment(0.0, 30.0)], beats=("hook", "build_up", "climax", "reaction", "ending"))

        assert self._judge(shaped).structure > self._judge(listed).structure

    def test_an_empty_plan_scores_nothing_and_says_so(self) -> None:
        score = self._judge(_Plan([]))

        assert score.total == 0.0
        assert score.why == ("the plan is empty",)

    def test_the_score_survives_a_round_trip_to_a_job_result(self) -> None:
        score = self._judge(_Plan([_Moment(0.0, 30.0)]))

        as_dict = score.as_dict()
        assert set(as_dict) == {*AXIS_WEIGHTS, "total", "why"}
        assert isinstance(as_dict["why"], list)


class TestEndingStrength:
    """"It stops rather than ends" was a real complaint about a real render,
    and it is the axis a length-optimising knapsack is least likely to serve:
    the last clip is whatever happened to fit."""

    class _Reader:
        def __init__(self, hot_after):
            self._hot_after = hot_after
            self.hz = 2
            self.duration_s = 1000.0
            self.media_id = "m"

        def intensity_between(self, start, end):
            return 0.9 if start >= self._hot_after else 0.2

        def window(self, name, start, end):
            return [0.0]

        def lane(self, name):
            return [0.0]

        def value_at(self, name, seconds):
            return 0.0

        def level_for(self, start, end):
            return "normal"

        def shape(self, *, min_segment=None):
            return ()

        def summary(self):
            return []

    def test_ending_on_the_strongest_thing_scores_higher(self) -> None:
        moments = [_Moment(0.0, 30.0), _Moment(100.0, 130.0), _Moment(200.0, 230.0)]
        strong = judge(_Plan(moments), reader=self._Reader(hot_after=200.0), config=None)
        weak = judge(_Plan(moments), reader=self._Reader(hot_after=10_000.0), config=None)

        assert strong.ending > weak.ending
