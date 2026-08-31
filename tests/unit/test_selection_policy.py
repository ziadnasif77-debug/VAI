"""The seam a style reaches the optimiser through, and the promise it keeps.

The optimiser is the most delicate code in this project and it is
deterministic, which is why any plan it produces can be argued with. A style
that changed it would be a style that could break it. So a style produces a
:class:`SelectionPolicy` -- five bounded multipliers on the objective the
optimiser already has -- and the optimiser's own signature and body are
untouched.

The load-bearing test in this file is the first one. Not "the house style
selects a similar set": the identical set, the identical order, the identical
value, byte for byte. Anything less and every regression in this branch would
be invisible behind a shrug about optimisation noise.
"""

from __future__ import annotations

import random

import pytest

from backend.core.models.enums import MomentType, VideoMode
from backend.editorial.policy import (
    MULTIPLIER_CEILING,
    MULTIPLIER_FLOOR,
    NEUTRAL,
    SelectionPolicy,
)
from backend.moments.formation import Moment
from backend.narrative.optimizer import optimise
from backend.narrative.plans import PROFILES, propose

pytestmark = pytest.mark.unit

TARGET = 20 * 60.0


def _pool(count: int = 60, seed: int = 20260831) -> list[Moment]:
    """A fixed corpus. The seed is the point: the same moments every run."""
    random.seed(seed)
    kinds = list(MomentType)
    moments: list[Moment] = []
    cursor = 30.0
    for index in range(count):
        duration = random.uniform(8.0, 45.0)
        kind = kinds[index % len(kinds)]
        moments.append(
            Moment(
                media_id="media-1",
                moment_type=kind,
                start_seconds=cursor,
                end_seconds=cursor + duration,
                events=(),
                context_start=cursor,
                context_end=cursor + duration,
                score=random.uniform(0.2, 0.95),
                score_breakdown={
                    "entertainment": random.uniform(0.1, 0.95),
                    "narrative": random.uniform(0.1, 0.95),
                },
                repetition_score=random.uniform(0.0, 0.35),
                dead_time_score=random.uniform(0.0, 0.25),
            )
        )
        cursor += duration + random.uniform(15.0, 60.0)
    return moments


def _selected(result) -> list[tuple[str, float, float]]:
    """The selection as a comparable fingerprint: which, where, how long.

    A moment has no stored id at this stage -- it is identified by where it
    sits and what it is -- so the fingerprint is exactly that.
    """
    return [
        (
            moment.moment_type.value,
            round(moment.context_start, 6),
            round(moment.context_end, 6),
        )
        for moment in result.moments
    ]


class TestNeutralIsExact:
    """The promise the rest of this work is built on."""

    def test_a_neutral_policy_returns_the_callers_own_config(self, config) -> None:
        # Identity, not equality. A copy that is equal in every field today is
        # a copy that might not be after the next field is added, and the
        # guarantee has to survive that.
        narrative = config.narrative

        assert NEUTRAL.applied_to(narrative) is narrative

    def test_the_house_style_selects_the_identical_moments(self, config) -> None:
        """OLD == NEW, exactly.

        The optimiser is called twice over one corpus: once as every caller
        called it before this seam existed, and once through a neutral policy.
        Same ids, same context bounds, same total, same value.
        """
        moments = _pool()
        before = optimise(
            moments,
            target_seconds=TARGET,
            config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        after = optimise(
            moments,
            target_seconds=TARGET,
            config=NEUTRAL.applied_to(config.narrative).optimizer,
            policy=config.duration_policy,
        )

        assert _selected(after) == _selected(before)
        assert after.total_seconds == before.total_seconds
        assert after.value == before.value

    def test_the_shipped_profile_still_asks_for_nothing(self) -> None:
        # Profile A is the shipped behaviour, and a regression in it would be
        # invisible unless someone asserts it asks for nothing at all.
        assert PROFILES[0].policy().is_neutral

    def test_composing_two_neutral_policies_stays_neutral(self) -> None:
        assert NEUTRAL.then(NEUTRAL).is_neutral


class TestTheProfilesStillSayWhatTheySaid:
    """P6's three profiles, unchanged by being given a type."""

    def test_each_profile_converts_to_its_own_numbers(self) -> None:
        for profile in PROFILES:
            policy = profile.policy()
            assert policy.entertainment == pytest.approx(profile.entertainment)
            assert policy.narrative == pytest.approx(profile.narrative)
            assert policy.variety == pytest.approx(profile.variety)
            assert policy.repetition_penalty == pytest.approx(profile.repetition_penalty)
            assert policy.dead_time_penalty == pytest.approx(profile.dead_time_penalty)

    def test_a_profile_still_bends_the_objective(self, config) -> None:
        bent = PROFILES[1].policy().applied_to(config.narrative)

        assert bent is not config.narrative
        assert bent.optimizer.objective_weights.entertainment == pytest.approx(
            config.narrative.optimizer.objective_weights.entertainment * 1.35
        )

    def test_three_profiles_still_produce_three_plans(self, config) -> None:
        proposed = propose(
            _pool(),
            mode=VideoMode.STORY,
            target_seconds=TARGET,
            config=config.narrative,
            policy=config.duration_policy,
        )

        assert [profile.id for profile, _ in proposed] == ["A", "B", "C"]

    def test_a_style_and_a_profile_are_both_true_at_once(self, config) -> None:
        # A profile asks for a different balance of the same edit; a style asks
        # for a different edit. Neither wins outright.
        composed = SelectionPolicy(variety=1.5).then(PROFILES[2].policy())

        assert composed.variety == pytest.approx(min(MULTIPLIER_CEILING, 1.5 * 1.60))


class TestTheFence:
    """A style may ask for a different edit, not for a different problem."""

    def test_an_absurd_multiplier_is_bounded_rather_than_obeyed(self) -> None:
        assert SelectionPolicy(entertainment=100.0).entertainment == MULTIPLIER_CEILING
        assert SelectionPolicy(variety=0.0).variety == MULTIPLIER_FLOOR

    def test_composing_two_legal_policies_cannot_make_an_illegal_one(self) -> None:
        # Below the floor a term is effectively deleted and the optimiser is
        # solving a different problem; above the ceiling one term dominates and
        # the knapsack becomes a sort.
        strong = SelectionPolicy(entertainment=MULTIPLIER_CEILING)
        composed = strong.then(strong)

        assert composed.entertainment == MULTIPLIER_CEILING

    def test_a_policy_says_what_it_asks_for(self) -> None:
        assert "unchanged" in NEUTRAL.describe()
        assert "variety x1.5" in SelectionPolicy(variety=1.5).describe()


class TestAPolicyActuallyChangesTheSelection:
    """Otherwise the seam is decoration."""

    def test_leaning_hard_on_variety_selects_a_different_set(self, config) -> None:
        moments = _pool()
        house = optimise(
            moments,
            target_seconds=TARGET,
            config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        varied = optimise(
            moments,
            target_seconds=TARGET,
            config=SelectionPolicy(variety=2.5, repetition_penalty=2.5)
            .applied_to(config.narrative)
            .optimizer,
            policy=config.duration_policy,
        )

        assert _selected(varied) != _selected(house)
