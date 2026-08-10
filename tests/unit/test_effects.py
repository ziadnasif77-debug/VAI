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
