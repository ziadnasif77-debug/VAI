"""Moments (Phase 6, SPEC §28-§34).

Three claims carry this phase, and each has its own tests:

* **§28** — a moment is a story fragment, so related events group and the group
  is typed by the most specific thing in it.
* **§29** — context expansion is *adaptive*. A fixed ±20 s is what makes an
  automated highlight video recognisable as one.
* **§32/§33** — a score, with its working shown, that nobody is allowed to
  mistake for a verdict.

No FFmpeg and no models: events are constructed directly, so each test knows
exactly what went in and can assert exactly what came out.
"""

from __future__ import annotations

import pytest

from ai.providers.base import TranscriptSegment
from backend.analysis import frame_state
from backend.analysis.audio_events import GAMEPLAY, MICROPHONE, AudioEvent
from backend.analysis.scenes import Scene
from backend.core.models.enums import (
    AudioEventType,
    FrameState,
    GameEventType,
    MomentType,
)
from backend.gaming.correlation import GameEvent
from backend.moments.context import ROLL_SHAPE, ExpansionSources, expand
from backend.moments.dead_time import dead_time_ratio, detect_dead_time
from backend.moments.formation import Moment, form_moments, moment_type_for
from backend.moments.repetition import (
    detect_repetition,
    saturation_penalties,
    variety_report,
)
from backend.moments.scoring import DIMENSIONS, ScoringContext, score_moments

pytestmark = pytest.mark.unit


def _event(
    event_type: GameEventType,
    at: float,
    *,
    duration: float = 2.0,
    confidence: float = 0.8,
    importance: float = 0.7,
    sources: tuple[str, ...] = ("ocr",),
) -> GameEvent:
    return GameEvent(
        event_type=event_type,
        start_seconds=at,
        end_seconds=at + duration,
        confidence=confidence,
        importance=importance,
        sources=sources,
    )


def _moment(
    at: float,
    *,
    moment_type: MomentType = MomentType.SKILL,
    events: tuple[GameEvent, ...] = (),
    media_id: str = "media_1",
    score: float = 0.5,
) -> Moment:
    resolved = events or (_event(GameEventType.KILL, at),)
    return Moment(
        media_id=media_id,
        moment_type=moment_type,
        start_seconds=at,
        end_seconds=at + 2.0,
        events=resolved,
        context_start=at - 5.0,
        context_end=at + 7.0,
        score=score,
    )


class TestFormation:
    """§28: a moment is a story fragment, not an instant."""

    def test_nearby_events_become_one_moment(self, config) -> None:
        # setup → combat → kill → reaction is ONE moment, not four clips.
        events = [
            _event(GameEventType.LOW_HEALTH, 100.0),
            _event(GameEventType.KILL, 104.0),
            _event(GameEventType.UNKNOWN_EVENT, 107.0),
        ]
        moments = form_moments(events, config.moments.formation, media_id="m")
        assert len(moments) == 1
        assert len(moments[0].events) == 3
        assert moments[0].start_seconds == 100.0
        assert moments[0].end_seconds == 109.0

    def test_distant_events_stay_separate(self, config) -> None:
        events = [_event(GameEventType.KILL, 100.0), _event(GameEventType.KILL, 400.0)]
        assert len(form_moments(events, config.moments.formation, media_id="m")) == 2

    def test_an_overlong_group_splits_at_its_widest_gap(self, config) -> None:
        # Truncating at an arbitrary maximum would end a clip mid-action.
        formation = config.moments.formation.model_copy(
            update={"max_moment_seconds": 30.0, "max_event_gap_seconds": 60.0}
        )
        events = [
            _event(GameEventType.KILL, 0.0),
            _event(GameEventType.KILL, 10.0),
            _event(GameEventType.KILL, 55.0),
            _event(GameEventType.KILL, 62.0),
        ]
        moments = form_moments(events, formation, media_id="m")
        assert len(moments) == 2
        assert moments[0].end_seconds <= 30.0
        assert moments[1].start_seconds >= 50.0

    def test_a_short_moment_is_kept_not_dropped(self, config) -> None:
        # A half-second kill is exactly what a highlight video is made of.
        moments = form_moments(
            [_event(GameEventType.KILL, 100.0, duration=0.4)],
            config.moments.formation,
            media_id="m",
        )
        assert len(moments) == 1

    def test_the_most_specific_type_wins(self) -> None:
        # A multi-kill beside an unnamed audio spike is a multi-kill.
        assert (
            moment_type_for(
                [
                    _event(GameEventType.UNKNOWN_EVENT, 0.0),
                    _event(GameEventType.MULTI_KILL, 1.0),
                ]
            )
            is MomentType.EPIC
        )

    def test_unnamed_events_produce_a_surprise(self) -> None:
        # §23's honest answer when nothing could say what happened.
        assert (
            moment_type_for([_event(GameEventType.UNKNOWN_EVENT, 0.0)])
            is MomentType.SURPRISE
        )

    def test_confidence_is_the_strongest_event_not_the_average(self, config) -> None:
        # A certain kill next to a doubtful spike is a certain kill that
        # happened to be noisy.
        moments = form_moments(
            [
                _event(GameEventType.KILL, 100.0, confidence=0.95),
                _event(GameEventType.UNKNOWN_EVENT, 102.0, confidence=0.2),
            ],
            config.moments.formation,
            media_id="m",
        )
        assert moments[0].confidence == 0.95

    def test_no_events_produce_no_moments(self, config) -> None:
        assert form_moments([], config.moments.formation, media_id="m") == []


class TestContextExpansion:
    """§29: adaptive, not a fixed ±20 seconds."""

    def test_different_moment_types_get_different_rolls(self, config) -> None:
        # A clutch is made by its build-up; a funny moment by the beat after.
        clutch = _moment(100.0, moment_type=MomentType.CLUTCH)
        funny = _moment(100.0, moment_type=MomentType.FUNNY)
        expanded = expand(
            [clutch, funny], config.moments.context, ExpansionSources(duration_seconds=300.0)
        )
        clutch_pre = expanded[0].start_seconds - expanded[0].context_start
        funny_pre = expanded[1].start_seconds - expanded[1].context_start
        assert clutch_pre > funny_pre
        funny_post = expanded[1].context_end - expanded[1].end_seconds
        clutch_post = expanded[0].context_end - expanded[0].end_seconds
        assert funny_post > clutch_post

    def test_the_roll_shape_covers_the_types_that_matter(self) -> None:
        for moment_type in (MomentType.CLUTCH, MomentType.FUNNY, MomentType.TENSION):
            assert moment_type in ROLL_SHAPE

    def test_the_start_snaps_to_a_scene_boundary(self, config) -> None:
        # A cut is where the picture already changed, so a clip that begins
        # there reads as a deliberate edit.
        moment = _moment(100.0)
        scenes = [
            Scene(index=0, start_seconds=0.0, end_seconds=92.0),
            Scene(index=1, start_seconds=92.0, end_seconds=150.0, change_score=40.0),
        ]
        expanded = expand(
            [moment],
            config.moments.context,
            ExpansionSources(scenes=scenes, duration_seconds=300.0),
        )[0]
        assert expanded.context_start == 92.0
        assert expanded.metadata["snapped"] is True

    def test_a_distant_boundary_is_not_chased(self, config) -> None:
        # Opening forty seconds early to catch a cut trades one artefact for a
        # longer one.
        moment = _moment(100.0)
        scenes = [Scene(index=0, start_seconds=0.0, end_seconds=300.0)]
        expanded = expand(
            [moment],
            config.moments.context,
            ExpansionSources(scenes=scenes, duration_seconds=300.0),
        )[0]
        assert expanded.context_start > 80.0

    def test_the_clip_does_not_start_mid_sentence(self, config) -> None:
        # The most audible artefact automated editing produces.
        moment = _moment(100.0)
        transcript = [TranscriptSegment(start=88.0, end=95.0, text="watch this")]
        expanded = expand(
            [moment],
            config.moments.context,
            ExpansionSources(transcript=transcript, duration_seconds=300.0),
        )[0]
        assert expanded.context_start <= 88.0

    def test_the_clip_does_not_end_mid_sentence(self, config) -> None:
        moment = _moment(100.0)
        transcript = [TranscriptSegment(start=106.0, end=110.0, text="did you see that")]
        expanded = expand(
            [moment],
            config.moments.context,
            ExpansionSources(transcript=transcript, duration_seconds=300.0),
        )[0]
        assert expanded.context_end >= 110.0

    def test_context_never_shrinks_the_moment(self, config) -> None:
        moment = _moment(100.0)
        expanded = expand(
            [moment], config.moments.context, ExpansionSources(duration_seconds=300.0)
        )[0]
        assert expanded.context_start <= expanded.start_seconds
        assert expanded.context_end >= expanded.end_seconds

    def test_expansion_is_clamped_to_the_recording(self, config) -> None:
        moment = _moment(2.0)
        expanded = expand(
            [moment], config.moments.context, ExpansionSources(duration_seconds=20.0)
        )[0]
        assert expanded.context_start >= 0.0
        assert expanded.context_end <= 20.0

    def test_expansion_works_with_no_sources_at_all(self, config) -> None:
        # §95: no scenes and no transcript still produces a viewing span.
        expanded = expand(
            [_moment(100.0)], config.moments.context, ExpansionSources(duration_seconds=300.0)
        )[0]
        assert expanded.context_duration > expanded.duration


class TestDeadTime:
    """§30: removed only when removal does not damage context."""

    @staticmethod
    def _silence(start: float, end: float) -> AudioEvent:
        return AudioEvent(
            event_type=AudioEventType.SILENCE,
            start_seconds=start,
            end_seconds=end,
            track_role=GAMEPLAY,
        )

    def test_a_quiet_gap_between_moments_is_dead_time(self, config) -> None:
        moments = [_moment(50.0), _moment(400.0)]
        segments = detect_dead_time(
            600.0,
            config.moments.dead_time,
            moments=moments,
            audio_events=[self._silence(70.0, 380.0)],
        )
        assert segments
        assert any(segment.score > 0.5 for segment in segments)

    def test_dead_time_adjacent_to_a_moment_is_protected(self, config) -> None:
        # The walk up to the ambush is what makes the ambush land.
        moments = [_moment(100.0)]
        segments = detect_dead_time(
            300.0,
            config.moments.dead_time,
            moments=moments,
            audio_events=[self._silence(0.0, 95.0)],
        )
        touching = [s for s in segments if s.end_seconds <= moments[0].context_start + 0.01]
        assert touching
        assert any(segment.protected for segment in touching)
        assert any(not segment.removable for segment in touching)

    def test_commentary_over_a_quiet_stretch_scores_lower(self, config) -> None:
        moments = [_moment(50.0), _moment(400.0)]
        quiet = detect_dead_time(
            600.0,
            config.moments.dead_time,
            moments=moments,
            audio_events=[self._silence(70.0, 380.0)],
        )
        talking = detect_dead_time(
            600.0,
            config.moments.dead_time,
            moments=moments,
            audio_events=[self._silence(70.0, 380.0)],
            transcript=[TranscriptSegment(start=80.0, end=370.0, text="explaining")],
        )
        assert max(s.score for s in talking) < max(s.score for s in quiet)

    def test_a_loading_screen_is_named_by_vision(self, config) -> None:
        from ai.providers.base import VisionObservation
        from backend.database.repositories.vision import StoredObservation

        observation = StoredObservation(
            observation=VisionObservation(
                timestamp=200.0, description="loading", labels=("loading",), confidence=0.9
            ),
            region_start=None, region_end=None, sources=(),
            model_name="m", model_version="1", prompt_id=None, prompt_version=None,
        )
        segments = detect_dead_time(
            600.0,
            config.moments.dead_time,
            moments=[_moment(50.0), _moment(400.0)],
            vision=[observation],
        )
        named = [s for s in segments if s.overlaps(195.0, 205.0)]
        assert named
        assert named[0].category.value == "loading"
        assert named[0].score >= 0.8

    def test_the_pass_can_be_switched_off(self, config) -> None:
        disabled = config.moments.dead_time.model_copy(update={"enabled": False})
        assert detect_dead_time(600.0, disabled, moments=[_moment(50.0)]) == []

    def test_the_ratio_measures_the_viewing_span(self, config) -> None:
        moment = _moment(100.0)
        segments = detect_dead_time(
            600.0,
            config.moments.dead_time,
            moments=[moment, _moment(400.0)],
            audio_events=[self._silence(0.0, 90.0)],
        )
        assert 0.0 <= dead_time_ratio(moment, segments) <= 1.0


class TestRepetition:
    """§31: keep the strongest representative, not the first seen."""

    def test_similar_moments_are_grouped(self, config) -> None:
        moments = [
            _moment(at, moment_type=MomentType.SKILL, score=score)
            for at, score in ((100.0, 0.4), (200.0, 0.9), (300.0, 0.5))
        ]
        result = detect_repetition(moments, config.moments.repetition)
        assert result.groups
        assert result.groups[0].size == 3

    def test_the_strongest_is_kept_not_the_first(self, config) -> None:
        # The fifth attempt at a boss is usually the one that worked.
        moments = [
            _moment(100.0, score=0.3),
            _moment(200.0, score=0.95),
            _moment(300.0, score=0.5),
        ]
        result = detect_repetition(moments, config.moments.repetition)
        kept = result.groups[0].kept
        assert kept[0].start_seconds == 200.0

    def test_the_penalty_grows_with_redundancy(self, config) -> None:
        moments = [
            _moment(100.0, score=0.9),
            _moment(200.0, score=0.5),
            _moment(300.0, score=0.2),
        ]
        result = detect_repetition(moments, config.moments.repetition)
        assert result.score_for(moments[0]) < result.score_for(moments[2])

    def test_different_moment_types_are_not_repeats(self, config) -> None:
        moments = [
            _moment(100.0, moment_type=MomentType.SKILL),
            _moment(200.0, moment_type=MomentType.FUNNY),
        ]
        assert detect_repetition(moments, config.moments.repetition).groups == ()

    def test_nothing_is_deleted(self, config) -> None:
        # The pass scores; the narrative stage decides.
        moments = [_moment(at) for at in (100.0, 200.0, 300.0, 400.0)]
        result = detect_repetition(moments, config.moments.repetition)
        assert result.repeated_moments == 4

    def test_the_pass_can_be_switched_off(self, config) -> None:
        disabled = config.moments.repetition.model_copy(update={"enabled": False})
        assert detect_repetition([_moment(1.0), _moment(2.0)], disabled).groups == ()


class TestVariety:
    """§33: the highest scores alone make a monotonous video."""

    def test_a_dominant_type_is_penalised(self, config) -> None:
        moments = [
            _moment(index * 100.0, moment_type=MomentType.SKILL, score=0.9 - index * 0.01)
            for index in range(10)
        ]
        penalties = saturation_penalties(moments, config.moments.variety)
        assert any(value > 0 for value in penalties.values())

    def test_a_varied_selection_is_not_penalised(self, config) -> None:
        types = [MomentType.SKILL, MomentType.FUNNY, MomentType.CLUTCH, MomentType.FAIL]
        moments = [
            _moment(index * 100.0, moment_type=types[index % len(types)])
            for index in range(4)
        ]
        assert all(value == 0.0 for value in saturation_penalties(moments, config.moments.variety).values())

    def test_the_penalty_never_rejects(self, config) -> None:
        # §33 says the selector must justify picking it, not that it is banned.
        moments = [_moment(index * 100.0) for index in range(10)]
        penalties = saturation_penalties(moments, config.moments.variety)
        assert len(penalties) == len(moments)
        assert all(value < 1.0 for value in penalties.values())

    def test_the_report_describes_the_shortlist(self, config) -> None:
        moments = [
            _moment(0.0, moment_type=MomentType.SKILL),
            _moment(100.0, moment_type=MomentType.FUNNY),
            _moment(200.0, moment_type=MomentType.CLUTCH),
        ]
        report = variety_report(moments, config.moments.variety)
        assert report["distinct_types"] == 3
        assert report["meets_minimum_types"] is True


class TestScoring:
    """§32: ten dimensions, configurable weights, and the working shown."""

    @staticmethod
    def _context(**overrides) -> ScoringContext:
        base = {"duration_seconds": 600.0}
        return ScoringContext(**{**base, **overrides})

    def test_every_dimension_is_scored_and_stored(self, config) -> None:
        scored = score_moments([_moment(100.0)], config.moments.scoring, self._context())
        breakdown = scored[0].score_breakdown
        for dimension in DIMENSIONS:
            assert dimension in breakdown
            assert 0.0 <= breakdown[dimension] <= 1.0

    def test_the_penalties_are_stored_separately(self, config) -> None:
        # §80: a score is not explainable if only the total survives.
        scored = score_moments([_moment(100.0)], config.moments.scoring, self._context())
        breakdown = scored[0].score_breakdown
        assert {"_penalty_dead_time", "_penalty_repetition", "_penalty_low_confidence"} <= set(
            breakdown
        )
        assert "_base" in breakdown
        assert "_multiplier" in breakdown

    def test_every_moment_gets_an_explanation(self, config) -> None:
        scored = score_moments([_moment(100.0)], config.moments.scoring, self._context())
        assert scored[0].explanation
        assert all(isinstance(line, str) and line for line in scored[0].explanation)

    def test_weights_are_configurable(self, config) -> None:
        # §32 requires it, so changing one must change the result.
        moment = _moment(100.0, moment_type=MomentType.FUNNY)
        default = score_moments([moment], config.moments.scoring, self._context())[0].score
        weights = config.moments.scoring.weights.model_copy(update={"emotion": 0.01})
        altered = config.moments.scoring.model_copy(update={"weights": weights})
        changed = score_moments([moment], altered, self._context())[0].score
        assert default != changed

    def test_dead_time_lowers_the_score(self, config) -> None:
        moment = _moment(100.0)
        clean = score_moments([moment], config.moments.scoring, self._context())[0]
        penalised = score_moments(
            [moment],
            config.moments.scoring,
            self._context(dead_time={("media_1", 100.0): 1.0}),
        )[0]
        assert penalised.score < clean.score
        assert penalised.dead_time_score == 1.0

    def test_penalties_compound(self, config) -> None:
        moment = _moment(100.0)
        one = score_moments(
            [moment], config.moments.scoring, self._context(dead_time={("media_1", 100.0): 1.0})
        )[0]
        both = score_moments(
            [moment],
            config.moments.scoring,
            self._context(
                dead_time={("media_1", 100.0): 1.0},
                repetition={("media_1", 100.0): 1.0},
            ),
        )[0]
        assert both.score < one.score

    def test_a_reaction_raises_the_score(self, config) -> None:
        # The person who was there thought it was worth watching.
        moment = _moment(100.0)
        quiet = score_moments([moment], config.moments.scoring, self._context())[0]
        with_reaction = score_moments(
            [moment],
            config.moments.scoring,
            self._context(
                audio_events=[
                    AudioEvent(
                        event_type=AudioEventType.LAUGH,
                        start_seconds=101.0,
                        end_seconds=103.0,
                        track_role=MICROPHONE,
                        confidence=0.9,
                        metadata={"reaction_type": "laugh", "correlation_offset": 0.5},
                    )
                ]
            ),
        )[0]
        assert with_reaction.score > quiet.score

    def test_skill_is_conservative_without_game_knowledge(self, config) -> None:
        # Nothing here can tell a lucky kill from an outplay.
        plain = score_moments(
            [_moment(100.0, events=(_event(GameEventType.KILL, 100.0),))],
            config.moments.scoring,
            self._context(),
        )[0]
        clutch = score_moments(
            [_moment(100.0, events=(_event(GameEventType.CLUTCH, 100.0),))],
            config.moments.scoring,
            self._context(),
        )[0]
        assert plain.score_breakdown["skill"] < clutch.score_breakdown["skill"]
        assert plain.score_breakdown["skill"] <= 0.5

    def test_results_are_ranked_best_first(self, config) -> None:
        moments = [
            _moment(100.0, events=(_event(GameEventType.KILL, 100.0, importance=0.2),)),
            _moment(200.0, events=(_event(GameEventType.CLUTCH, 200.0, importance=0.95),)),
        ]
        scored = score_moments(moments, config.moments.scoring, self._context())
        assert scored[0].score >= scored[1].score

    def test_a_score_stays_inside_zero_and_one(self, config) -> None:
        moments = [_moment(at) for at in (10.0, 100.0, 500.0)]
        for moment in score_moments(moments, config.moments.scoring, self._context()):
            assert 0.0 <= moment.score <= 1.0

    def test_scoring_needs_no_model(self, config) -> None:
        # §95: a machine with no LLM still produces ranked moments.
        scored = score_moments(
            [_moment(100.0)],
            config.moments.scoring,
            ScoringContext(duration_seconds=600.0),
        )
        assert scored[0].score > 0.0
        assert scored[0].explanation

    def test_the_explanation_names_the_penalty_that_applied(self, config) -> None:
        scored = score_moments(
            [_moment(100.0)],
            config.moments.scoring,
            self._context(dead_time={("media_1", 100.0): 0.8}),
        )[0]
        assert any("dead time" in line for line in scored.explanation)

    def test_low_confidence_is_flagged_for_review(self, config) -> None:
        # §79: surfaced, not hidden.
        weak = Moment(
            media_id="media_1",
            moment_type=MomentType.SURPRISE,
            start_seconds=100.0,
            end_seconds=102.0,
            events=(_event(GameEventType.UNKNOWN_EVENT, 100.0, confidence=0.2),),
            context_start=95.0,
            context_end=108.0,
        )
        scored = score_moments([weak], config.moments.scoring, self._context())[0]
        assert any("review" in line.lower() for line in scored.explanation)


class TestMenuFramesAndTheVisualScore:
    """A model sure it sees a menu is an argument AGAINST the clip.

    Menu and loading observations used to count as visual density like any
    other, so a confident "menu" label raised the visual score of the very
    footage a viewer skips. The first real edit had QA flag 28 selected
    moments as menu or loading screens; the first real viewer asked for more
    accurate selection. Same defect, two witnesses.
    """

    def _observation(self, timestamp: float, labels: tuple[str, ...], confidence=0.9):
        from ai.providers.base import VisionObservation
        from backend.database.repositories.vision import StoredObservation

        return StoredObservation(
            observation=VisionObservation(
                timestamp=timestamp,
                description=" ".join(labels),
                labels=labels,
                confidence=confidence,
            ),
            region_start=None, region_end=None, sources=(),
            model_name="m", model_version="1", prompt_id=None, prompt_version=None,
        )

    def _visual_for(self, labels_per_frame, config):
        from backend.moments.scoring import ScoringContext, score_moments

        moment = _moment(100.0)
        vision = [
            self._observation(100.0 + index * 3.0, labels)
            for index, labels in enumerate(labels_per_frame)
        ]
        scored = score_moments(
            [moment],
            config.moments.scoring,
            ScoringContext(duration_seconds=600.0, vision=vision),
        )
        return scored[0].score_breakdown["visual"]

    def test_menu_frames_score_below_gameplay_frames(self, config) -> None:
        gameplay = self._visual_for(
            [("combat",), ("explosion",), ("combat",), ("vehicle",)], config
        )
        menus = self._visual_for(
            [("menu",), ("menu",), ("loading",), ("combat",)], config
        )

        assert menus < gameplay

    def test_a_span_that_is_all_menus_hits_the_floor(self, config) -> None:
        value = self._visual_for([("menu",), ("loading",), ("menu",)], config)

        assert value <= 0.05

    def test_a_confident_menu_label_does_not_raise_the_score(self, config) -> None:
        # The exact old failure: high confidence on "menu" used to *increase*
        # quality. Now more menu frames can only pull the value down.
        some_menus = self._visual_for(
            [("combat",), ("menu",), ("combat",)], config
        )
        no_menus = self._visual_for([("combat",), ("combat",)], config)

        assert some_menus < no_menus


class TestFrameStateKeepsMenusOutOfTheEdit:
    """Phase 0.6: the labels were always there, and nothing read them.

    Lowering a menu clip's *score* was the first half of this fix and was not
    enough — a menu moment with a loud audio spike still outscored quiet
    gameplay. The content QA kept reporting them after the render: 40 moments
    in one real project, 22 in another. The evidence to reject every one was
    sitting in `vision_observations` when the edit was built.
    """

    @staticmethod
    def _observation(timestamp: float, labels: tuple[str, ...], confidence: float = 0.9):
        from ai.providers.base import VisionObservation
        from backend.database.repositories.vision import StoredObservation

        return StoredObservation(
            observation=VisionObservation(
                timestamp=timestamp,
                description=" ".join(labels),
                labels=labels,
                confidence=confidence,
            ),
            region_start=None, region_end=None, sources=(),
            model_name="m", model_version="1", prompt_id=None, prompt_version=None,
        )

    def _spans(self, *frames: tuple[float, tuple[str, ...]]):
        return frame_state.non_gameplay(
            frame_state.spans(
                [self._observation(at, labels) for at, labels in frames],
                duration_seconds=600.0,
            )
        )

    def test_a_moment_inside_a_menu_is_not_a_moment(self, config) -> None:
        spans = self._spans((100.0, ("menu",)), (104.0, ("menu",)), (108.0, ("menu",)))

        formed = form_moments(
            [_event(GameEventType.UNKNOWN_EVENT, 102.0)],
            config.moments.formation,
            media_id="m",
            non_gameplay=spans,
        )

        assert formed == []

    def test_gameplay_beside_a_menu_survives(self, config) -> None:
        # The menu ends and the mission starts. Rejecting this would cost
        # exactly the clips a highlight video is made of.
        spans = self._spans((100.0, ("menu",)), (104.0, ("menu",)))

        formed = form_moments(
            [_event(GameEventType.KILL, 140.0)],
            config.moments.formation,
            media_id="m",
            non_gameplay=spans,
        )

        assert len(formed) == 1

    def test_an_unlabelled_stretch_is_not_a_menu(self, config) -> None:
        # A frame nobody labelled is not evidence of a menu, and treating it
        # as one would silently drop footage for lack of a label.
        spans = self._spans((100.0, ()), (104.0, ()))

        formed = form_moments(
            [_event(GameEventType.UNKNOWN_EVENT, 102.0)],
            config.moments.formation,
            media_id="m",
            non_gameplay=spans,
        )

        assert len(formed) == 1

    def test_the_overlap_is_recorded_on_survivors(self, config) -> None:
        # §80: a moment that is a quarter menu is worth knowing about in
        # review, and the number is what makes the threshold arguable.
        spans = self._spans((100.0, ("loading",)))

        formed = form_moments(
            [_event(GameEventType.KILL, 100.0, duration=30.0)],
            config.moments.formation,
            media_id="m",
            non_gameplay=spans,
        )

        assert len(formed) == 1
        assert 0.0 < formed[0].metadata["non_gameplay_ratio"] < 0.25

    def test_a_menu_over_gameplay_is_still_a_menu(self) -> None:
        # An inventory screen drawn over a firefight is unusable footage; the
        # gameplay label underneath does not rescue it.
        assert frame_state.state_for(("combat", "menu")) is FrameState.MENU
        assert frame_state.state_for(("combat",)) is FrameState.GAMEPLAY

    def test_overlapping_spans_are_counted_once(self) -> None:
        # Two observations a second apart, each padded either side. Counted
        # naively that is more menu than the window contains.
        spans = self._spans((100.0, ("menu",)), (101.0, ("menu",)))
        covered = spans[0]

        assert frame_state.overlap_ratio(90.0, 120.0, spans) <= 1.0
        assert frame_state.overlap_ratio(
            covered.start_seconds, covered.end_seconds, spans
        ) == pytest.approx(1.0)

    def test_a_lone_reading_is_weaker_than_an_agreeing_run(self) -> None:
        # One frame is evidence about one instant. Three readings agreeing
        # across a stretch are evidence the stretch was a menu -- and the
        # rule must not have it backwards, which it did: a lone `loading`
        # frame padded four seconds either side rejected the only moment in
        # a whole project.
        lone = self._spans((100.0, ("menu",)))
        run = self._spans((100.0, ("menu",)), (106.0, ("menu",)), (112.0, ("menu",)))

        assert frame_state.longest_overlap_ratio(95.0, 125.0, lone) < 0.15
        assert frame_state.longest_overlap_ratio(95.0, 125.0, run) > 0.4

    def test_scattered_menu_frames_do_not_reject_a_moment(self, config) -> None:
        # The measured failure this rule replaced: summing scattered readings
        # reaches the same fraction as one real block, and rejected every
        # moment in the test project.
        spans = self._spans(
            (6.0, ("loading",)),
            (12.0, ("menu",)),
            (22.0, ("loading",)),
            (28.0, ("menu",)),
        )

        formed = form_moments(
            [_event(GameEventType.VICTORY, 4.75, duration=27.0)],
            config.moments.formation,
            media_id="m",
            non_gameplay=spans,
        )

        assert len(formed) == 1

    def test_the_guard_can_be_disabled(self, config) -> None:
        formation = config.moments.formation.model_copy(
            update={"max_non_gameplay_core_overlap": 1.0}
        )
        spans = self._spans((100.0, ("menu",)), (104.0, ("menu",)))

        formed = form_moments(
            [_event(GameEventType.UNKNOWN_EVENT, 102.0)],
            formation,
            media_id="m",
            non_gameplay=spans,
        )

        assert len(formed) == 1


class TestDoctrineTiers:
    """docs/DIRECTION.md §4: names on the score, not new arithmetic."""

    def test_the_rungs_name_what_the_score_earned(self) -> None:
        from backend.moments.scoring import tier_for

        assert tier_for(0.95) == "master"
        assert tier_for(0.84) == "major"
        assert tier_for(0.71) == "good"
        assert tier_for(0.61) == "supporting"
        assert tier_for(0.30) == "below"

    def test_every_scored_moment_carries_its_tier(self) -> None:
        from backend.moments.scoring import tier_for

        # Any scored moment's breakdown names the tier its score earned;
        # exercised through the public scorer in the integration tests, and
        # kept honest here at the boundary values.
        for score in (0.9, 0.8, 0.7, 0.6):
            assert tier_for(score) != "below"
        assert tier_for(0.5999) == "below"

@pytest.mark.unit
class TestExclusionsReachFormation:
    """V2-P0.2 at the moment layer: refuse the core, pull back the context."""

    def test_a_core_that_is_a_menu_is_refused(self, config) -> None:
        from backend.moments.formation import form_moments

        assert not form_moments(
            [_event(GameEventType.KILL, 100.0, duration=4.0)],
            config.moments.formation,
            media_id="m",
            excluded_spans=[(95.0, 120.0)],
        )

    def test_context_is_pulled_back_to_the_edge_of_a_menu(self, config) -> None:
        # The rule the older guard was missing, and the one that mattered: the
        # core never touched the menu, and the footage still arrived.
        from backend.moments.formation import form_moments

        (moment,) = form_moments(
            [_event(GameEventType.KILL, 100.0, duration=4.0)],
            config.moments.formation,
            media_id="m",
            excluded_spans=[(112.0, 140.0)],
        )

        assert moment.context_end <= 112.0
        assert moment.start_seconds == pytest.approx(100.0), "the core is untouched"

    def test_expansion_cannot_carry_a_context_back_into_a_menu(self, config) -> None:
        # P0.2 gate, item 6. Formation pulled the context to the menu's edge;
        # expansion then widened it from scenes and speech without knowing
        # the menu, and seven stored contexts on the benchmark reached 24.5 s
        # into exclusions. The pull-back runs again after expansion.
        from backend.moments.formation import form_moments, pull_back_contexts, replace_moment

        (moment,) = form_moments(
            [_event(GameEventType.KILL, 100.0, duration=4.0)],
            config.moments.formation,
            media_id="m",
            excluded_spans=[(112.0, 140.0)],
        )
        widened = replace_moment(moment, context_start=80.0, context_end=130.0)

        (pulled,), count = pull_back_contexts([widened], [(112.0, 140.0)])

        assert count == 1
        assert pulled.context_end <= 112.0
        assert pulled.context_start == pytest.approx(80.0), "the far side is left alone"
        assert pulled.start_seconds == pytest.approx(100.0), "the core is untouched"

    def test_the_pull_back_is_a_no_op_without_exclusions(self, config) -> None:
        from backend.moments.formation import form_moments, pull_back_contexts

        moments = form_moments(
            [_event(GameEventType.KILL, 100.0, duration=4.0)], config.moments.formation, media_id="m"
        )
        same, count = pull_back_contexts(moments, [])
        assert count == 0 and same == list(moments)

    def test_a_clean_recording_forms_exactly_what_it_formed_before(
        self, config
    ) -> None:
        from backend.moments.formation import form_moments

        events = [_event(GameEventType.KILL, 100.0, duration=4.0)]
        before = form_moments(events, config.moments.formation, media_id="m")
        after = form_moments(
            events, config.moments.formation, media_id="m", excluded_spans=[]
        )

        assert len(before) == len(after)
        for one, two in zip(before, after, strict=True):
            assert one.start_seconds == two.start_seconds
            assert one.end_seconds == two.end_seconds
            assert one.context_start == two.context_start
            assert one.context_end == two.context_end

