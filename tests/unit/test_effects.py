"""Effects engine tests (SPEC §68, §69, §70).

The rule that matters most is §69: effects are never applied globally. These
tests assert that budgets actually bind, that the intent's dial is respected,
and that the FFmpeg and Remotion halves of a plan stay separable.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from backend.config.schema import AppConfig
from backend.core.errors import ValidationError
from backend.core.models.enums import (
    EffectCategory,
    EffectEngine,
    EffectType,
    GameEventType,
    MomentType,
)
from backend.effects.library import EffectLibrary
from backend.effects.models import EffectInstance, EffectPlan
from backend.effects.planner import EffectPlanner, PlannedMoment
from backend.interaction.models import EditingIntent, EffectsLevel

pytestmark = pytest.mark.unit


def moment(
    index: int,
    *,
    moment_type: MomentType = MomentType.CLUTCH,
    score: float = 0.9,
    start: float | None = None,
    duration: float = 20.0,
    events: list[GameEventType] | None = None,
) -> PlannedMoment:
    timeline_start = index * 60.0 if start is None else start
    return PlannedMoment(
        id=f"mom-{index:012d}",
        moment_type=moment_type,
        timeline_start=timeline_start,
        timeline_end=timeline_start + duration,
        score=score,
        events=events or [GameEventType.MULTI_KILL],
        clip_id=f"clip-{index:012d}",
    )


def intent(
    *, effects: EffectsLevel = EffectsLevel.MODERATE, style: str = "default"
) -> EditingIntent:
    return EditingIntent(effects=effects, style=style)


class TestLibrary:
    def test_every_configured_effect_is_a_known_type(self, config: AppConfig) -> None:
        for effect in config.effects.library:
            assert isinstance(effect, EffectType)

    def test_library_covers_the_requested_categories(self, config: AppConfig) -> None:
        categories = {spec.category for spec in config.effects.library.values()}
        assert categories >= {
            EffectCategory.CAMERA,
            EffectCategory.TIME,
            EffectCategory.LIGHT,
            EffectCategory.GRAPHIC,
            EffectCategory.TEXT,
            EffectCategory.TRANSITION,
            EffectCategory.AUDIO,
        }

    def test_engine_split_matches_the_two_pass_render(self, config: AppConfig) -> None:
        # Effects that alter the footage are FFmpeg's; overlays are Remotion's.
        library = config.effects.library
        assert library[EffectType.ZOOM].engine is EffectEngine.FFMPEG
        assert library[EffectType.SPEED_RAMP].engine is EffectEngine.FFMPEG
        assert library[EffectType.CINEMATIC_BARS].engine is EffectEngine.FFMPEG
        assert library[EffectType.TEXT_POP].engine is EffectEngine.REMOTION
        assert library[EffectType.KILL_COUNTER].engine is EffectEngine.REMOTION
        assert library[EffectType.HIGHLIGHT_BOX].engine is EffectEngine.REMOTION

    def test_risky_effects_are_off_by_default(self, config: AppConfig) -> None:
        # Memes date fast and crosshair overlays misfire without a detected region.
        assert config.effects.library[EffectType.MEME].enabled is False
        assert config.effects.library[EffectType.CROSSHAIR].enabled is False

    def test_unknown_effect_is_rejected(self, config: AppConfig) -> None:
        library = EffectLibrary(config)
        stripped = config.model_copy(deep=True)
        del stripped.effects.library[EffectType.ZOOM]
        with pytest.raises(ValidationError, match="not defined"):
            EffectLibrary(stripped).definition(EffectType.ZOOM)
        assert library.definition(EffectType.ZOOM).effect is EffectType.ZOOM

    def test_style_suppression_removes_an_effect(self, config: AppConfig) -> None:
        library = EffectLibrary(config)
        available = {item.effect for item in library.available("cinematic")}
        assert EffectType.IMPACT not in available
        assert EffectType.CINEMATIC_BARS in available

    def test_style_boost_raises_priority(self, config: AppConfig) -> None:
        library = EffectLibrary(config)
        assert library.priority_multiplier(EffectType.IMPACT, "gaming_fast") > 1.0
        assert library.priority_multiplier(EffectType.IMPACT, "cinematic") == 0.0

    def test_intent_dial_can_only_lower_the_style_intensity(
        self, config: AppConfig
    ) -> None:
        library = EffectLibrary(config)
        # "fewer effects" must not be overridden by a style that likes them.
        assert library.intensity_for(EffectsLevel.MINIMAL, "gaming_fast") == pytest.approx(0.3)
        assert library.intensity_for(EffectsLevel.NONE, "gaming_fast") == 0.0
        assert library.intensity_for(EffectsLevel.HEAVY, "cinematic") == pytest.approx(0.35)

    def test_params_scale_magnitudes_not_durations(self, config: AppConfig) -> None:
        library = EffectLibrary(config)
        full = library.params_for(EffectType.ZOOM, 1.0)
        half = library.params_for(EffectType.ZOOM, 0.5)
        assert half["scale"] < full["scale"]
        assert half["scale"] > 1.0, "a weaker zoom is still a zoom"
        assert half["duration_seconds"] == full["duration_seconds"]
        assert half["easing"] == full["easing"]


class TestPlanning:
    def test_produces_effects_for_strong_moments(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(index) for index in range(5)], intent(), video_duration_seconds=600
        )
        assert plan.count > 0
        assert all(instance.moment_id for instance in plan.instances)

    def test_every_effect_is_attached_to_a_moment(self, config: AppConfig) -> None:
        # §69: nothing is applied globally.
        plan = EffectPlanner(config).plan(
            [moment(index) for index in range(5)], intent(), video_duration_seconds=600
        )
        assert all(instance.moment_id is not None for instance in plan.instances)

    def test_no_effects_when_the_intent_says_none(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(index) for index in range(5)],
            intent(effects=EffectsLevel.NONE),
            video_duration_seconds=600,
        )
        assert plan.count == 0
        assert plan.rejected

    def test_minimal_produces_fewer_than_heavy(self, config: AppConfig) -> None:
        planner = EffectPlanner(config)
        moments = [moment(index) for index in range(10)]
        minimal = planner.plan(
            moments, intent(effects=EffectsLevel.MINIMAL), video_duration_seconds=600
        )
        heavy = planner.plan(
            moments, intent(effects=EffectsLevel.HEAVY), video_duration_seconds=600
        )
        assert minimal.count < heavy.count

    def test_low_scoring_moments_earn_nothing(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(index, score=0.2) for index in range(5)],
            intent(),
            video_duration_seconds=600,
        )
        assert plan.count == 0

    def test_effects_never_outlast_their_moment(self, config: AppConfig) -> None:
        moments = [moment(index, duration=1.0) for index in range(5)]
        plan = EffectPlanner(config).plan(moments, intent(), video_duration_seconds=600)
        by_id = {item.id: item for item in moments}
        for instance in plan.instances:
            owner = by_id[instance.moment_id or ""]
            assert instance.end_seconds <= owner.timeline_end + 0.01

    def test_plan_is_ordered_by_time(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(index) for index in range(8)], intent(), video_duration_seconds=900
        )
        starts = [instance.start_seconds for instance in plan.instances]
        assert starts == sorted(starts)

    def test_planning_is_deterministic(self, config: AppConfig) -> None:
        planner = EffectPlanner(config)
        moments = [moment(index) for index in range(6)]
        first = planner.plan(moments, intent(), video_duration_seconds=600)
        second = planner.plan(moments, intent(), video_duration_seconds=600)
        assert [item.model_dump() for item in first.instances] == [
            item.model_dump() for item in second.instances
        ]


class TestContentGuards:
    """An effect that names or marks something must have the something.

    Both defects were found by reading the renderer against the rows it was
    about to receive: ``text_pop`` draws nothing without ``params.text``, and
    ``highlight_box`` falls back to a centred default box -- a marker around
    nothing -- when no detector supplied a region.
    """

    def _plan(self, config: AppConfig, *moments_):
        return EffectPlanner(config).plan(
            list(moments_), intent(), video_duration_seconds=600
        )

    @staticmethod
    def _roomy(config: AppConfig) -> AppConfig:
        # TEXT's placement offset is 0.88 -- deliberately after the action --
        # so under the default two-per-moment budget every earlier category
        # outbids it. The guard under test is about the label, not the budget.
        limits = config.effects.global_limits.model_copy(
            update={"max_effects_per_moment": 12}
        )
        effects = config.effects.model_copy(update={"global_limits": limits})
        return config.model_copy(update={"effects": effects})

    def test_text_pop_always_carries_its_text(self, config: AppConfig) -> None:
        plan = self._plan(
            self._roomy(config),
            moment(0, moment_type=MomentType.VICTORY, events=[GameEventType.VICTORY]),
        )

        pops = [item for item in plan.instances if item.effect is EffectType.TEXT_POP]
        assert pops, "a high-scoring victory earns a text_pop"
        assert all(item.params.get("text") == "VICTORY" for item in pops)

    def test_the_label_falls_back_to_the_moment_type(self, config: AppConfig) -> None:
        # No events at all -- built directly, because the module's `moment`
        # helper substitutes a default event list for an empty one. The
        # moment's own detected type is still detected metadata, and the
        # renderer must never receive an empty label.
        eventless = PlannedMoment(
            id="mom-000000000000",
            moment_type=MomentType.EPIC,
            timeline_start=0.0,
            timeline_end=20.0,
            score=0.9,
            events=[],
            clip_id="clip-000000000000",
        )
        plan = self._plan(self._roomy(config), eventless)

        pops = [item for item in plan.instances if item.effect is EffectType.TEXT_POP]
        assert pops
        assert all(item.params.get("text") == "EPIC" for item in pops)

    def test_a_matched_event_beats_the_type_as_the_label(self, config: AppConfig) -> None:
        plan = self._plan(
            self._roomy(config),
            moment(0, moment_type=MomentType.EPIC, events=[GameEventType.MULTI_KILL]),
        )

        pops = [item for item in plan.instances if item.effect is EffectType.TEXT_POP]
        assert pops
        assert all(item.params.get("text") == "MULTI KILL" for item in pops)

    def test_an_unlistened_event_never_becomes_the_label(self, config: AppConfig) -> None:
        # Real footage's event stream is dominated by unexpected_event, which
        # rides along in matched_events but is not in text_pop's trigger
        # list. "UNEXPECTED EVENT" on screen would be the software confessing;
        # the moment's own type is the honest label.
        plan = self._plan(
            self._roomy(config),
            moment(
                0,
                moment_type=MomentType.VICTORY,
                events=[GameEventType.UNEXPECTED_EVENT],
            ),
        )

        pops = [item for item in plan.instances if item.effect is EffectType.TEXT_POP]
        assert pops
        assert all(item.params.get("text") == "VICTORY" for item in pops)

    def test_a_marker_with_no_detected_region_is_never_planned(
        self, config: AppConfig
    ) -> None:
        # highlight_box declares require_detected_region and no detector
        # supplies regions today: the correct count is zero, not a default
        # box drawn around the centre of the screen.
        plan = self._plan(
            config,
            moment(0, moment_type=MomentType.SKILL, events=[GameEventType.KILL]),
        )

        assert not any(
            item.effect is EffectType.HIGHLIGHT_BOX for item in plan.instances
        )

    def test_a_counter_with_no_events_is_never_planned(self, config: AppConfig) -> None:
        # kill_counter's trigger list matches on moment type alone, and its
        # renderer defaults the count to 1 -- an "x1" tally. Without events
        # there is nothing to count.
        plan = self._plan(config, moment(0, moment_type=MomentType.EPIC, events=[]))

        assert not any(
            item.effect is EffectType.KILL_COUNTER for item in plan.instances
        )

    def test_one_countable_event_is_not_a_streak(self, config: AppConfig) -> None:
        # A tally of one is noise; the counter needs at least two events its
        # triggers can count before it earns the corner of the screen.
        plan = self._plan(
            self._roomy(config),
            moment(0, moment_type=MomentType.EPIC, events=[GameEventType.MULTI_KILL]),
        )

        assert not any(
            item.effect is EffectType.KILL_COUNTER for item in plan.instances
        )

    def test_a_real_streak_earns_a_counter_that_carries_its_tally(
        self, config: AppConfig
    ) -> None:
        # The renderer defaults an absent count to 1, so the tally has to
        # travel with the instance or the evidence guard was for nothing.
        # The competitive style is the counter's natural habitat -- it boosts
        # kill_counter past the graphic-category rivals that outbid it under
        # the default profile.
        plan = EffectPlanner(self._roomy(config)).plan(
            [
                moment(
                    0,
                    moment_type=MomentType.EPIC,
                    events=[
                        GameEventType.KILL,
                        GameEventType.KILL,
                        GameEventType.MULTI_KILL,
                    ],
                )
            ],
            intent(style="competitive"),
            video_duration_seconds=600,
        )

        counters = [
            item for item in plan.instances if item.effect is EffectType.KILL_COUNTER
        ]
        assert counters, "three countable events are a streak"
        assert all(item.params.get("count") == 3 for item in counters)


class TestBudgets:
    def test_per_video_density_is_bounded(self, config: AppConfig) -> None:
        # 40 strong moments in a 10-minute video must not yield 40 effects.
        moments = [moment(index, start=index * 15.0, duration=10.0) for index in range(40)]
        plan = EffectPlanner(config).plan(
            moments, intent(effects=EffectsLevel.HEAVY), video_duration_seconds=600
        )
        ceiling = config.effects.global_limits.max_effects_per_minute
        assert plan.density_per_minute(600) <= ceiling + 0.01

    def test_one_moment_cannot_collect_everything(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(0, duration=60.0)],
            intent(effects=EffectsLevel.HEAVY),
            video_duration_seconds=600,
        )
        assert plan.count <= config.effects.global_limits.max_effects_per_moment

    def test_no_two_effects_of_one_category_on_a_moment(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(0, duration=60.0)],
            intent(effects=EffectsLevel.HEAVY),
            video_duration_seconds=600,
        )
        categories = [instance.category for instance in plan.instances]
        assert len(categories) == len(set(categories))

    def test_minimum_gap_is_respected(self, config: AppConfig) -> None:
        moments = [moment(index, start=index * 3.0, duration=2.5) for index in range(20)]
        plan = EffectPlanner(config).plan(
            moments, intent(effects=EffectsLevel.HEAVY), video_duration_seconds=600
        )
        minimum = config.effects.global_limits.min_gap_seconds
        starts = sorted(instance.start_seconds for instance in plan.instances)
        gaps = [second - first for first, second in pairwise(starts)]
        assert all(gap >= minimum - 0.01 for gap in gaps)

    def test_rejections_are_reported_not_silent(self, config: AppConfig) -> None:
        moments = [moment(index, start=index * 5.0, duration=4.0) for index in range(30)]
        plan = EffectPlanner(config).plan(
            moments, intent(effects=EffectsLevel.HEAVY), video_duration_seconds=600
        )
        assert plan.rejected, "dropped candidates must be explained"
        # Every rejection names the effect, when it would have run, and why.
        for reason in plan.rejected:
            assert "@" in reason and ":" in reason


class TestStyleBehaviour:
    def test_cinematic_avoids_impact_effects(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(index) for index in range(6)],
            intent(style="cinematic"),
            video_duration_seconds=600,
        )
        assert EffectType.IMPACT not in {item.effect for item in plan.instances}

    def test_competitive_avoids_decoration(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(index) for index in range(6)],
            intent(style="competitive"),
            video_duration_seconds=600,
        )
        placed = {item.effect for item in plan.instances}
        assert EffectType.MEME not in placed
        assert EffectType.FLASH not in placed
        assert EffectType.CAMERA_SHAKE not in placed

    def test_gaming_fast_is_denser_than_cinematic(self, config: AppConfig) -> None:
        planner = EffectPlanner(config)
        moments = [moment(index) for index in range(10)]
        fast = planner.plan(
            moments, intent(effects=EffectsLevel.HEAVY, style="gaming_fast"),
            video_duration_seconds=600,
        )
        cinematic = planner.plan(
            moments, intent(effects=EffectsLevel.HEAVY, style="cinematic"),
            video_duration_seconds=600,
        )
        assert fast.count > cinematic.count

    def test_unknown_style_falls_back_to_default(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [moment(index) for index in range(4)],
            intent(style="a_style_nobody_configured"),
            video_duration_seconds=600,
        )
        assert plan.intensity > 0


class TestEnginePartition:
    def test_plan_splits_cleanly_between_renderers(self, config: AppConfig) -> None:
        plan = EffectPlanner(config).plan(
            [
                moment(index, moment_type=kind)
                for index, kind in enumerate(
                    [MomentType.CLUTCH, MomentType.EPIC, MomentType.VICTORY, MomentType.SKILL]
                )
            ],
            intent(effects=EffectsLevel.HEAVY),
            video_duration_seconds=600,
        )
        ffmpeg = plan.for_engine(EffectEngine.FFMPEG)
        remotion = plan.for_engine(EffectEngine.REMOTION)
        assert len(ffmpeg) + len(remotion) == plan.count
        assert not {id(item) for item in ffmpeg} & {id(item) for item in remotion}

    def test_a_plan_with_no_overlays_needs_no_remotion_pass(
        self, config: AppConfig
    ) -> None:
        # This is what lets the render stage skip Chromium entirely.
        plan = EffectPlanner(config).plan(
            [moment(index) for index in range(4)],
            intent(style="competitive", effects=EffectsLevel.MINIMAL),
            video_duration_seconds=600,
        )
        if not plan.for_engine(EffectEngine.REMOTION):
            assert plan.for_engine(EffectEngine.FFMPEG) == plan.instances


class TestPlanModel:
    def test_rejects_unordered_instances(self) -> None:
        late = EffectInstance(
            effect=EffectType.ZOOM,
            engine=EffectEngine.FFMPEG,
            category=EffectCategory.CAMERA,
            start_seconds=10.0,
            duration_seconds=1.0,
        )
        early = late.model_copy(update={"start_seconds": 1.0})
        with pytest.raises(Exception, match="ordered by start time"):
            EffectPlan(instances=[late, early], intensity=0.5)

    def test_overlap_detection(self) -> None:
        first = EffectInstance(
            effect=EffectType.ZOOM,
            engine=EffectEngine.FFMPEG,
            category=EffectCategory.CAMERA,
            start_seconds=10.0,
            duration_seconds=2.0,
        )
        overlapping = first.model_copy(update={"start_seconds": 11.0})
        separate = first.model_copy(update={"start_seconds": 13.0})
        assert first.overlaps(overlapping) is True
        assert first.overlaps(separate) is False


class TestRelativeThresholds:
    """§23's promise reaching the effects engine.

    Trigger thresholds were calibrated on footage with a game profile, a
    microphone and detected reactions. Without those, the reaction, skill and
    narrative dimensions have no evidence, the whole distribution shifts down,
    and a real session's best moment scored 0.53 against a lowest threshold of
    0.55 -- zero effects on a twelve-clip video. Ranking each moment against
    its own video is what keeps the engine alive on generic-profile footage.
    """

    def _moments(self, scores):
        from backend.core.models.enums import MomentType
        from backend.effects.planner import PlannedMoment

        return [
            PlannedMoment(
                id=f"m{index}",
                moment_type=MomentType.TENSION,
                timeline_start=index * 60.0,
                timeline_end=index * 60.0 + 30.0,
                score=score,
                clip_id=f"c{index}",
            )
            for index, score in enumerate(scores)
        ]

    def test_a_low_scoring_video_still_gets_effects(self, config) -> None:
        from backend.effects.planner import EffectPlanner
        from backend.interaction.models import EditingIntent

        # The real distribution that produced nothing.
        moments = self._moments([0.53, 0.53, 0.49, 0.48, 0.46, 0.41, 0.33, 0.28])

        plan = EffectPlanner(config).plan(
            moments, EditingIntent(), video_duration_seconds=600.0
        )

        assert plan.instances, "a video below every absolute threshold got no effects"

    def test_the_best_moment_earns_more_than_the_median(self, config) -> None:
        from backend.effects.planner import EffectPlanner
        from backend.interaction.models import EditingIntent

        moments = self._moments([0.53, 0.45, 0.40, 0.35, 0.30])
        plan = EffectPlanner(config).plan(
            moments, EditingIntent(), video_duration_seconds=600.0
        )

        by_moment: dict[str, int] = {}
        for instance in plan.instances:
            by_moment[instance.moment_id or ""] = by_moment.get(instance.moment_id or "", 0) + 1
        assert by_moment.get("m0", 0) >= by_moment.get("m4", 0)

    def test_ranking_never_rescues_a_dead_video(self, config) -> None:
        # Everything below the absolute floor: being "the best of nothing" is
        # not an argument for decoration.
        from backend.effects.planner import EffectPlanner
        from backend.interaction.models import EditingIntent

        moments = self._moments([0.10, 0.08, 0.05, 0.03])

        plan = EffectPlanner(config).plan(
            moments, EditingIntent(), video_duration_seconds=600.0
        )

        decorative = [i for i in plan.instances if i.effect.value not in {"transition", "fade"}]
        assert not decorative

    def test_identical_scores_are_not_all_promoted(self, config) -> None:
        # Ranking a flat distribution would make every moment the best one.
        from backend.effects.planner import EffectPlanner
        from backend.interaction.models import EditingIntent

        flat = EffectPlanner(config).plan(
            self._moments([0.30] * 6), EditingIntent(), video_duration_seconds=600.0
        )
        varied = EffectPlanner(config).plan(
            self._moments([0.53, 0.45, 0.40, 0.35, 0.30, 0.25]),
            EditingIntent(),
            video_duration_seconds=600.0,
        )

        assert len(flat.instances) <= len(varied.instances)

    def test_absolute_thresholds_can_be_restored(self, config) -> None:
        from backend.effects.planner import EffectPlanner
        from backend.interaction.models import EditingIntent

        absolute = config.model_copy(
            update={"effects": config.effects.model_copy(
                update={"relative_thresholds": False}
            )}
        )
        plan = EffectPlanner(absolute).plan(
            self._moments([0.53, 0.45, 0.40]),
            EditingIntent(),
            video_duration_seconds=600.0,
        )

        decorative = [i for i in plan.instances if i.effect.value not in {"transition", "fade"}]
        assert not decorative
