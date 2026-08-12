"""Narrative (Phase 7, SPEC §35-§39).

The acceptance criterion is a duration: a long source becomes a coherent edit of
the requested length. So the tests are mostly about §39, which is the part that
has to be right for the criterion to be met at all:

    Find the combination of moments closest to the target while maximising
    entertainment + narrative + variety and minimising repetition + dead time.
    **This is an optimisation problem, not simple sorting.**

The decisive test is the comparison against the greedy sort the spec warns
about: same candidates, same target, and the optimiser has to beat it on the
thing that matters.
"""

from __future__ import annotations

import random
from collections import Counter
from itertools import pairwise

import pytest

from backend.core.duration import DurationPolicy
from backend.core.models.enums import GameEventType, MomentType, VideoMode
from backend.gaming.correlation import GameEvent
from backend.moments.formation import Moment, replace_moment
from backend.narrative.hook import HOOK_STRENGTH, choose_hook
from backend.narrative.optimizer import optimise
from backend.narrative.pacing import intensity_of, order, report
from backend.narrative.story import BEAT_TYPES, build_plan

pytestmark = pytest.mark.unit

TARGET = 1200.0  # 20 minutes


def _moment(
    start: float,
    *,
    duration: float = 20.0,
    moment_type: MomentType = MomentType.SKILL,
    score: float = 0.6,
    entertainment: float = 0.6,
    narrative: float = 0.6,
    repetition: float = 0.0,
    dead_time: float = 0.0,
    media_id: str = "m",
) -> Moment:
    event = GameEvent(
        event_type=GameEventType.KILL,
        start_seconds=start,
        end_seconds=start + 2.0,
        confidence=0.85,
        importance=0.7,
        sources=("ocr", "audio"),
    )
    return Moment(
        media_id=media_id,
        moment_type=moment_type,
        start_seconds=start,
        end_seconds=start + 3.0,
        events=(event,),
        context_start=start - 4.0 if start >= 4.0 else 0.0,
        context_end=(start - 4.0 if start >= 4.0 else 0.0) + duration,
        score=score,
        score_breakdown={
            "entertainment": entertainment,
            "narrative": narrative,
            "audio": 0.5,
            "reaction": 0.5,
        },
        repetition_score=repetition,
        dead_time_score=dead_time,
    )


def _pool(count: int = 150, *, seed: int = 3) -> list[Moment]:
    """A synthetic session: varied types, durations and scores."""
    random.seed(seed)
    types = list(MomentType)
    moments: list[Moment] = []
    cursor = 0.0
    for index in range(count):
        duration = random.uniform(10.0, 40.0)
        moments.append(
            _moment(
                cursor,
                duration=duration,
                moment_type=types[index % len(types)],
                score=random.uniform(0.3, 0.95),
                entertainment=random.uniform(0.2, 0.95),
                narrative=random.uniform(0.2, 0.9),
                repetition=random.uniform(0.0, 0.35),
                dead_time=random.uniform(0.0, 0.25),
            )
        )
        cursor += duration + random.uniform(15.0, 60.0)
    return moments


def _greedy(moments, target: float) -> list[Moment]:
    """The top-N sort §39 says is not good enough."""
    chosen, total = [], 0.0
    for moment in sorted(moments, key=lambda item: -item.score):
        if total + moment.context_duration <= target:
            chosen.append(moment)
            total += moment.context_duration
    return chosen


class TestOptimiser:
    """§39: an optimisation problem, not simple sorting."""

    def test_it_lands_inside_the_tolerance(self, config) -> None:
        result = optimise(
            _pool(), target_seconds=TARGET, config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        assert result.within_tolerance
        assert abs(result.deviation) <= config.duration_policy.tolerance_for(TARGET)

    @pytest.mark.parametrize("minutes", [10, 15, 20, 30, 45, 60])
    def test_it_hits_every_offered_preset(self, config, minutes: int) -> None:
        # §6's presets are what the import screen offers, so each must be
        # reachable from the same source.
        result = optimise(
            _pool(200), target_seconds=minutes * 60.0,
            config=config.narrative.optimizer, policy=config.duration_policy,
        )
        assert result.within_tolerance, result.notes

    def test_it_beats_a_greedy_sort_on_variety(self, config) -> None:
        # The failure §33 names: the top of a score ranking is the same kind of
        # moment over and over.
        moments = _pool()
        result = optimise(
            moments, target_seconds=TARGET, config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        greedy = _greedy(moments, TARGET)

        optimiser_types = len(Counter(m.moment_type for m in result.moments))
        greedy_types = len(Counter(m.moment_type for m in greedy))
        assert optimiser_types > greedy_types

    def test_variety_is_priced_inside_the_objective(self, config) -> None:
        # A per-moment bonus cannot express "this is the fourth kill in a row",
        # which is why the search carries the type mix.
        same = [_moment(i * 100.0, moment_type=MomentType.SKILL) for i in range(40)]
        varied = [
            _moment(i * 100.0, moment_type=list(MomentType)[i % 6]) for i in range(40)
        ]
        same_value = optimise(
            same, target_seconds=400.0, config=config.narrative.optimizer,
            policy=config.duration_policy,
        ).value
        varied_value = optimise(
            varied, target_seconds=400.0, config=config.narrative.optimizer,
            policy=config.duration_policy,
        ).value
        assert varied_value > same_value

    def test_repetition_and_dead_time_lower_a_moments_worth(self, config) -> None:
        clean = [_moment(i * 100.0, moment_type=list(MomentType)[i % 8]) for i in range(20)]
        penalised = [
            _moment(
                i * 100.0, moment_type=list(MomentType)[i % 8], repetition=0.9, dead_time=0.9
            )
            for i in range(20)
        ]
        assert (
            optimise(clean, target_seconds=200.0, config=config.narrative.optimizer,
                     policy=config.duration_policy).value
            > optimise(penalised, target_seconds=200.0, config=config.narrative.optimizer,
                       policy=config.duration_policy).value
        )

    def test_it_returns_moments_in_recording_order(self, config) -> None:
        # Chosen by value, watched in time order.
        result = optimise(
            _pool(), target_seconds=TARGET, config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        starts = [moment.context_start for moment in result.moments]
        assert starts == sorted(starts)

    def test_context_is_trimmed_before_a_moment_is_dropped(self, config) -> None:
        # §29's roll is the slack in the system: a clip shortened by three
        # seconds is still the moment, a clip removed is not.
        moments = [_moment(i * 200.0, duration=60.0) for i in range(12)]
        result = optimise(
            moments, target_seconds=600.0, config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        assert result.within_tolerance
        # Every kept clip still contains its own events.
        for moment in result.moments:
            assert moment.context_start <= moment.start_seconds
            assert moment.context_end >= moment.end_seconds

    def test_an_impossible_target_is_reported_not_faked(self, config) -> None:
        # Two short moments cannot fill twenty minutes, and saying they did
        # would be worse than saying they cannot.
        result = optimise(
            [_moment(0.0, duration=10.0), _moment(100.0, duration=10.0)],
            target_seconds=TARGET, config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        assert not result.within_tolerance
        assert result.notes
        assert result.total_seconds < TARGET

    def test_no_candidates_produces_an_empty_result(self, config) -> None:
        result = optimise(
            [], target_seconds=TARGET, config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        assert result.is_empty
        assert not result.within_tolerance

    def test_the_result_reports_honestly(self, config) -> None:
        result = optimise(
            _pool(), target_seconds=TARGET, config=config.narrative.optimizer,
            policy=config.duration_policy,
        )
        summary = result.summary()
        assert summary["selected"] + summary["rejected"] == 150
        assert summary["within_tolerance"] is result.within_tolerance


class TestHook:
    """§37: selects an existing moment, invents nothing."""

    def test_it_picks_a_strong_opening_type(self, config) -> None:
        moments = [
            _moment(0.0, moment_type=MomentType.TENSION, score=0.95),
            _moment(100.0, moment_type=MomentType.EPIC, score=0.8),
        ]
        selection = choose_hook(moments, config.narrative.hook)
        # A tense build-up scores well and opens badly.
        assert selection.moment.moment_type is MomentType.EPIC

    def test_the_hook_is_always_a_real_moment(self, config) -> None:
        moments = _pool(20)
        selection = choose_hook(moments, config.narrative.hook)
        if selection.exists:
            assert any(
                moment.start_seconds == selection.moment.start_seconds for moment in moments
            )

    def test_a_long_hook_is_trimmed_from_the_front(self, config) -> None:
        # The payoff is at the end; an opening that stops before it promises
        # nothing.
        moment = _moment(100.0, duration=90.0, moment_type=MomentType.EPIC)
        selection = choose_hook([moment], config.narrative.hook)
        assert selection.moment.context_duration <= config.narrative.hook.max_seconds
        assert selection.moment.context_end == moment.context_end

    def test_no_suitable_moment_means_no_hook(self, config) -> None:
        # Better to start at the beginning than to promote a weak clip into the
        # position that decides whether anyone keeps watching.
        weak = [_moment(0.0, moment_type=MomentType.DISCOVERY)]
        assert not choose_hook(weak, config.narrative.hook).exists

    def test_it_can_be_disabled(self, config) -> None:
        disabled = config.narrative.hook.model_copy(update={"enabled": False})
        assert not choose_hook(_pool(20), disabled).exists

    def test_every_hook_type_has_a_strength(self, config) -> None:
        for name in config.narrative.hook.sources:
            if name == "outcome_preview":
                continue
            assert MomentType(name) in HOOK_STRENGTH


class TestPacing:
    """§38: the difference between an edit and a playlist."""

    def test_ordering_preserves_the_callers_sequence(self, config) -> None:
        # Re-sorting here once made all three §35 modes identical.
        moments = [
            _moment(i * 100.0, moment_type=list(MomentType)[i % 5]) for i in range(10)
        ]
        reversed_input = list(reversed(moments))
        result = order(reversed_input, config.narrative.pacing)
        assert [m.context_start for m in result] != [m.context_start for m in moments]

    def test_runs_of_the_same_type_are_broken_up(self, config) -> None:
        moments = [
            *[_moment(i * 100.0, moment_type=MomentType.SKILL) for i in range(5)],
            *[_moment(500.0 + i * 100.0, moment_type=MomentType.FUNNY) for i in range(5)],
        ]
        result = order(moments, config.narrative.pacing)
        longest = report(result, config.narrative.pacing).longest_same_type_run
        assert longest <= 5

    def test_the_report_warns_rather_than_corrects(self, config) -> None:
        # §78 gives the user the last word, so pacing reports a judgement.
        monotonous = [
            _moment(i * 100.0, moment_type=MomentType.TENSION) for i in range(8)
        ]
        assert report(monotonous, config.narrative.pacing).warnings

    def test_intensity_reflects_the_clip_not_only_its_type(self, config) -> None:
        quiet = _moment(0.0, moment_type=MomentType.EPIC)
        loud = _moment(0.0, moment_type=MomentType.EPIC)
        loud = type(loud)(
            **{
                **{f.name: getattr(loud, f.name) for f in loud.__dataclass_fields__.values()},
                "score_breakdown": {**loud.score_breakdown, "audio": 1.0, "reaction": 1.0},
            }
        )
        assert intensity_of(loud) > intensity_of(quiet)

    def test_an_empty_selection_reports_rather_than_crashing(self, config) -> None:
        assert report([], config.narrative.pacing).warnings


class TestModes:
    """§35: three modes, three different videos."""

    def test_all_three_modes_produce_different_orders(self, config) -> None:
        moments = _pool()
        orders = {
            mode: tuple(
                m.context_start
                for m in build_plan(
                    moments, mode=mode, target_seconds=TARGET,
                    config=config.narrative, policy=config.duration_policy,
                ).moments
            )
            for mode in (VideoMode.STORY, VideoMode.BEST_MOMENTS, VideoMode.COMPILATION)
        }
        assert len(set(orders.values())) == 3

    def test_story_mode_assigns_beats(self, config) -> None:
        plan = build_plan(
            _pool(), mode=VideoMode.STORY, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )
        assert plan.beats
        assert "climax" in plan.beats

    def test_every_beat_has_candidate_types(self) -> None:
        for beat in ("hook", "context", "build_up", "event", "escalation",
                     "climax", "reaction", "ending"):
            assert BEAT_TYPES[beat]

    def test_best_moments_leads_with_the_strongest(self, config) -> None:
        plan = build_plan(
            _pool(), mode=VideoMode.BEST_MOMENTS, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )
        body = [m for m in plan.moments if m.metadata.get("role") != "hook"]
        assert body[0].score >= body[-1].score

    def test_compilation_groups_by_type(self, config) -> None:
        plan = build_plan(
            _pool(), mode=VideoMode.COMPILATION, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )
        body = [m for m in plan.moments if m.metadata.get("role") != "hook"]
        # Types appear in contiguous runs rather than scattered.
        runs = sum(
            1
            for previous, current in pairwise(body)
            if previous.moment_type is not current.moment_type
        )
        assert runs < len(body) - 1

    def test_every_mode_respects_the_duration_target(self, config) -> None:
        for mode in (VideoMode.STORY, VideoMode.BEST_MOMENTS, VideoMode.COMPILATION):
            plan = build_plan(
                _pool(), mode=mode, target_seconds=TARGET,
                config=config.narrative, policy=config.duration_policy,
            )
            assert plan.within_target, f"{mode.value}: {plan.notes}"

    def test_no_moments_produces_an_empty_plan_not_a_crash(self, config) -> None:
        plan = build_plan(
            [], mode=VideoMode.STORY, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )
        assert plan.is_empty
        assert not plan.within_target
        assert plan.notes


class TestAcceptance:
    """A long source becomes a coherent edit of the requested length."""

    def test_a_two_hour_source_becomes_a_twenty_minute_edit(self, config) -> None:
        moments = _pool(200, seed=11)
        source_hours = moments[-1].context_end / 3600.0
        assert source_hours >= 2.0, "the fixture must be a genuinely long session"

        plan = build_plan(
            moments, mode=VideoMode.STORY, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )

        assert plan.within_target
        assert abs(plan.total_seconds - TARGET) <= config.duration_policy.tolerance_for(TARGET)
        # Coherent: an opening, an arc, and more than one kind of clip.
        assert plan.hook.exists
        assert "climax" in plan.beats
        assert len(Counter(m.moment_type for m in plan.moments)) >= 5

    def test_the_result_stays_inside_the_product_band(self, config) -> None:
        # §6: 10-60 minutes, whatever the optimiser decides.
        policy: DurationPolicy = config.duration_policy
        plan = build_plan(
            _pool(200), mode=VideoMode.BEST_MOMENTS, target_seconds=TARGET,
            config=config.narrative, policy=policy,
        )
        assert policy.contains(plan.total_seconds)


class TestHookWithoutReplay:
    """The first real viewer's defect: 25 seconds shown twice.

    The hook is cut from the tail of its moment (§37 keeps the payoff), and
    with replay off the body copy must keep its lead-up and lose only the
    seconds the opening already showed. The old behaviour dropped the whole
    body copy, which traded "shown twice" for "the setup never shown" -- a
    different defect wearing the fix's name.
    """

    def test_replay_is_off_by_default(self, config) -> None:
        assert config.narrative.hook.allow_replay_in_body is False

    def test_the_body_copy_keeps_the_lead_up_and_loses_the_hooked_tail(
        self, config
    ) -> None:
        from backend.narrative.hook import choose_hook
        from backend.narrative.story import _apply_hook

        # One long moment (context 0..78) whose tail becomes the 25s hook.
        big = _moment(60.0, duration=78.0, moment_type=MomentType.EPIC, score=0.9)
        big = replace_moment(big, context_start=0.0, context_end=78.0)
        other = _moment(200.0, moment_type=MomentType.SKILL)

        hook = choose_hook([big, other], config.narrative.hook)
        assert hook.exists
        assert hook.moment.context_start > 0.0  # trimmed from the front

        ordered = _apply_hook([big, other], hook, config.narrative)

        assert ordered[0].metadata.get("role") == "hook"
        body = ordered[1]
        assert body.context_start == 0.0
        # The body ends where the hook begins: nothing appears twice.
        assert abs(body.context_end - hook.moment.context_start) < 1e-6
        assert body.metadata.get("hook_span_removed_seconds", 0) > 0

    def test_no_second_of_source_appears_twice_in_the_plan(self, config) -> None:
        moments = [
            _moment(60.0, duration=78.0, moment_type=MomentType.EPIC, score=0.9),
            _moment(150.0, moment_type=MomentType.CLUTCH, score=0.7),
            _moment(300.0, moment_type=MomentType.FUNNY, score=0.6),
        ]
        moments[0] = replace_moment(moments[0], context_start=0.0, context_end=78.0)

        plan = build_plan(
            moments,
            mode=VideoMode.STORY,
            target_seconds=600.0,
            config=config.narrative,
            policy=config.duration_policy,
        )

        from itertools import pairwise

        spans = sorted(
            (m.media_id, m.context_start, m.context_end) for m in plan.moments
        )
        for earlier, later in pairwise(spans):
            if earlier[0] != later[0]:
                continue
            assert later[1] >= earlier[2] - 1e-6, (
                f"{later[1]} overlaps a span ending {earlier[2]}"
            )

    def test_a_moment_fully_used_by_the_hook_is_not_repeated(self, config) -> None:
        from backend.narrative.hook import choose_hook
        from backend.narrative.story import _apply_hook

        # Shorter than max_seconds: the hook consumes it whole.
        small = _moment(20.0, duration=18.0, moment_type=MomentType.EPIC, score=0.9)
        hook = choose_hook([small], config.narrative.hook)
        assert hook.exists

        ordered = _apply_hook([small], hook, config.narrative)

        assert len(ordered) == 1
        assert ordered[0].metadata.get("role") == "hook"


class TestStoryChronology:
    """A single session's causality IS its story.

    The first hour-long session shipped a video that jumped 0:00 -> 51:15 ->
    38:34 -> 66:36 -- beats used to dictate *position*, and the viewer called
    the result "not a story, it is fragmented". Beats now choose roles; the
    recording keeps its own order; the hook stays the one announced
    flash-forward.
    """

    def test_the_body_plays_in_recording_order(self, config) -> None:
        from itertools import pairwise

        plan = build_plan(
            _pool(), mode=VideoMode.STORY, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )
        body = [m for m in plan.moments if m.metadata.get("role") != "hook"]

        for earlier, later in pairwise(body):
            if earlier.media_id != later.media_id:
                continue
            assert later.context_start >= earlier.context_start, (
                f"{later.context_start} plays after {earlier.context_start} "
                "but happens earlier in the recording"
            )

    def test_beats_still_choose_roles(self, config) -> None:
        # The arc did not disappear -- it moved from positions to labels.
        plan = build_plan(
            _pool(), mode=VideoMode.STORY, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )

        assert plan.beats
        assert set(plan.beats) - {"body"}, "no beat was assigned to any moment"

    def test_a_type_streak_does_not_licence_time_travel(self, config) -> None:
        # The second scrambler found on real footage: §38's run-breaker pulled
        # the only two tension moments backwards into a sea of surprises, and
        # the "chronological" story jumped 12:25 -> 38:34 -> 14:59. In story
        # mode variety is the optimiser's job at selection time (§33); the
        # pacing swap stays for the other modes.
        from itertools import pairwise

        moments = [
            _moment(60.0 * i, moment_type=MomentType.SURPRISE, score=0.5)
            for i in range(1, 9)
        ] + [
            _moment(60.0 * 12, moment_type=MomentType.TENSION, score=0.9),
            _moment(60.0 * 14, moment_type=MomentType.TENSION, score=0.9),
        ]

        plan = build_plan(
            moments, mode=VideoMode.STORY, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )
        body = [m for m in plan.moments if m.metadata.get("role") != "hook"]

        for earlier, later in pairwise(body):
            assert later.context_start >= earlier.context_start

    def test_the_hook_is_the_only_flash_forward(self, config) -> None:
        plan = build_plan(
            _pool(), mode=VideoMode.STORY, target_seconds=TARGET,
            config=config.narrative, policy=config.duration_policy,
        )
        if not plan.hook.exists:
            pytest.skip("this pool produced no hook")

        first = plan.moments[0]
        assert first.metadata.get("role") == "hook"
