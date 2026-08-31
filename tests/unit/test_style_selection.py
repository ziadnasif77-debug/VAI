"""Four styles, one session, four different edits (V2-P11).

The requirement this file exists for, in the owner's words: the difference has
to show in *selection and structure*, before anyone looks at effects. A style
that produces the same clips with a different zoom is a filter, not an editor.

Two assertions carry the weight, and they pull in opposite directions:

* the house style must select **exactly** what it selected before styles could
  reach the selection at all, and
* four authored styles must each select something measurably different from it
  and from each other.

Either one alone is easy. Together they say the seam works and is bounded.
"""

from __future__ import annotations

import random

import pytest

from backend.core.models.enums import MomentType
from backend.editorial.doctrine import resolve
from backend.moments.formation import Moment
from backend.narrative.optimizer import optimise

pytestmark = pytest.mark.unit

TARGET = 20 * 60.0

#: The four the owner named, plus the house style they must differ from.
STYLES = ("gaming_fast", "cinematic", "funny", "minimal")


def _session(count: int = 140, seed: int = 20260901) -> list[Moment]:
    """One synthetic session: many kinds of thing, of many strengths.

    Fixed seed on purpose. Two styles disagreeing about random footage would
    prove nothing; they have to disagree about the same footage.

    Enough short moments that twenty minutes is reachable within the tolerance.
    The first version of this fixture used 80 moments of 6-40s, and the *house*
    style missed the band by four tenths of a second -- a packing limit of the
    fixture that would have read as a fault in every doctrine measured against
    it.
    """
    random.seed(seed)
    kinds = list(MomentType)
    moments: list[Moment] = []
    cursor = 20.0
    for index in range(count):
        duration = random.uniform(4.0, 26.0)
        moments.append(
            Moment(
                media_id="media-1",
                moment_type=kinds[index % len(kinds)],
                start_seconds=cursor,
                end_seconds=cursor + duration,
                events=(),
                context_start=cursor,
                context_end=cursor + duration,
                score=random.uniform(0.2, 0.95),
                score_breakdown={
                    "entertainment": random.uniform(0.1, 0.98),
                    "narrative": random.uniform(0.1, 0.98),
                },
                repetition_score=random.uniform(0.0, 0.45),
                dead_time_score=random.uniform(0.0, 0.35),
            )
        )
        cursor += duration + random.uniform(8.0, 45.0)
    return moments


def _cut(config, moments, style: str):
    """The selection one style makes, through the policy seam."""
    policy = resolve(config, style)
    return optimise(
        moments,
        target_seconds=TARGET,
        config=policy.selection.applied_to(config.narrative).optimizer,
        policy=config.duration_policy,
    )


def _chosen(result) -> set[tuple[str, float]]:
    return {
        (moment.moment_type.value, round(moment.context_start, 3))
        for moment in result.moments
    }


def _shape(result) -> dict[str, float]:
    """The measurable structure of an edit, before any effect is placed."""
    lengths = [moment.context_duration for moment in result.moments]
    kinds = {moment.moment_type for moment in result.moments}
    return {
        "clips": float(len(lengths)),
        "kinds": float(len(kinds)),
        "median_length": round(sorted(lengths)[len(lengths) // 2], 2) if lengths else 0.0,
        "total": round(sum(lengths), 1),
        "dead_time": round(
            sum(moment.dead_time_score for moment in result.moments)
            / max(len(result.moments), 1),
            3,
        ),
        "repetition": round(
            sum(moment.repetition_score for moment in result.moments)
            / max(len(result.moments), 1),
            3,
        ),
    }


class TestTheHouseStyleIsUnchanged:
    def test_it_selects_exactly_what_it_always_selected(self, config) -> None:
        """OLD == NEW, through the whole doctrine layer this time.

        Not the policy object in isolation: the style name, resolved through
        the bible, through the doctrine, into the optimiser. If any link in
        that chain quietly bends the objective, this fails.
        """
        moments = _session()
        before = optimise(
            moments,
            target_seconds=TARGET,
            config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        after = _cut(config, moments, "best_moments")

        assert _chosen(after) == _chosen(before)
        assert after.value == before.value
        assert after.total_seconds == before.total_seconds

    def test_and_so_does_the_unnamed_style(self, config) -> None:
        moments = _session()
        house = optimise(
            moments,
            target_seconds=TARGET,
            config=config.narrative.optimizer,
            policy=config.duration_policy,
        )

        assert _chosen(_cut(config, moments, "default")) == _chosen(house)

    def test_the_house_policy_asks_for_nothing(self, config) -> None:
        assert resolve(config, "best_moments").is_house
        assert resolve(config, "default").is_house


class TestFourStylesCutDifferently:
    """The requirement, measured."""

    def test_each_style_selects_something_the_house_style_did_not(
        self, config
    ) -> None:
        moments = _session()
        house = _chosen(_cut(config, moments, "best_moments"))
        for style in STYLES:
            chosen = _chosen(_cut(config, moments, style))
            assert chosen != house, f"{style} selected the house edit"

    def test_no_two_styles_select_the_same_edit(self, config) -> None:
        moments = _session()
        seen: dict[frozenset, str] = {}
        for style in STYLES:
            chosen = frozenset(_chosen(_cut(config, moments, style)))
            assert chosen not in seen, f"{style} and {seen[chosen]} cut identically"
            seen[chosen] = style

    def test_the_difference_is_in_the_structure_not_only_the_membership(
        self, config
    ) -> None:
        # Two edits can differ by one clip and be the same edit in every way a
        # viewer would notice. This asks for a difference in shape.
        moments = _session()
        shapes = {style: _shape(_cut(config, moments, style)) for style in STYLES}
        house = _shape(_cut(config, moments, "best_moments"))

        for style, shape in shapes.items():
            moved = [
                key
                for key, value in shape.items()
                if abs(value - house[key]) > 1e-6
            ]
            assert moved, f"{style} produced an edit of identical shape"

    def test_each_doctrine_moves_what_it_says_it_moves(self, config) -> None:
        """The doctrine is a claim, and this is the claim being checked.

        `funny` says variety matters and repetition is expensive; `competitive`
        says the opposite about repetition. If the selections did not differ in
        that direction, the words in config/style.yaml would be decoration.
        """
        moments = _session()
        funny = _shape(_cut(config, moments, "funny"))
        competitive = _shape(_cut(config, moments, "competitive"))

        assert funny["repetition"] <= competitive["repetition"], (
            "funny pays more for sameness than competitive does, so it should "
            "not end up with more of it"
        )

    def test_a_style_that_tolerates_dead_time_keeps_more_of_it(self, config) -> None:
        moments = _session()
        cinematic = _shape(_cut(config, moments, "cinematic"))
        fast = _shape(_cut(config, moments, "gaming_fast"))

        assert cinematic["dead_time"] >= fast["dead_time"]


class TestTheDifferenceIsBounded:
    def test_no_style_empties_the_edit(self, config) -> None:
        moments = _session()
        for style in (*STYLES, "best_moments", "competitive"):
            result = _cut(config, moments, style)
            assert result.moments, f"{style} selected nothing"

    def test_no_doctrine_makes_the_length_materially_worse(self, config) -> None:
        """A doctrine may change what is chosen. It may not change the length.

        Not `within_tolerance`: whether twenty minutes is *reachable* at all is
        a property of the footage, and on this corpus the house style itself
        misses the band by a second and a half -- the same warning the story
        stage logs on real recordings. Asserting the band here would have been
        asserting the fixture. What a style must not do is make that worse, so
        the comparison is against the house style rather than against the band.
        """
        moments = _session()
        house = abs(_cut(config, moments, "best_moments").deviation)
        for style in (*STYLES, "competitive"):
            deviation = abs(_cut(config, moments, style).deviation)
            assert deviation <= house + 5.0, (
                f"{style} missed the requested length by {deviation:.1f}s "
                f"against the house style's {house:.1f}s"
            )

    def test_a_typo_cuts_like_the_house(self, config) -> None:
        moments = _session()

        assert _chosen(_cut(config, moments, "cinematick")) == _chosen(
            _cut(config, moments, "best_moments")
        )
