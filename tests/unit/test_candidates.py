"""The candidate cascade (Phase 4, SPEC §15, §16, §27).

§15's constraint is arithmetic before it is anything else: a two-hour recording
sampled every three seconds is 2 400 frames, and a local 7B vision model spends
seconds on each. These tests are about the ceiling that stops that from
happening, and about the ranking that decides what fills it.

No FFmpeg and no model: triggers are constructed directly, so each test knows
exactly what was nominated and can assert exactly what the cascade did with it.
"""

from __future__ import annotations

import pytest

from backend.analysis.audio_events import AudioEvent
from backend.analysis.candidates import (
    AUDIO_SPIKE,
    FRAME_DIFFERENCE,
    SCENE_CHANGE,
    SPEECH_ACTIVITY,
    Trigger,
    build_candidates,
    triggers_from_audio,
    triggers_from_frame_difference,
    triggers_from_scenes,
    triggers_from_transcript,
)
from backend.analysis.scenes import Scene, SceneResult
from backend.core.models.enums import AudioEventType

pytestmark = pytest.mark.unit

HOUR = 3600.0


def _spike(at: float, *, confidence: float = 0.9) -> AudioEvent:
    return AudioEvent(
        event_type=AudioEventType.SPIKE,
        start_seconds=at,
        end_seconds=at + 0.5,
        confidence=confidence,
    )


def _trigger(source: str, at: float, *, confidence: float = 0.9) -> Trigger:
    return Trigger(source=source, start_seconds=at, end_seconds=at + 0.5, confidence=confidence)


class TestTriggerExtraction:
    def test_only_events_are_nominated_not_states(self, config) -> None:
        # Silence and speech are states. A detector that nominated every voiced
        # second would nominate the whole recording.
        events = [
            _spike(10.0),
            AudioEvent(event_type=AudioEventType.SILENCE, start_seconds=20.0, end_seconds=30.0),
            AudioEvent(event_type=AudioEventType.SPEECH, start_seconds=40.0, end_seconds=50.0),
            AudioEvent(
                event_type=AudioEventType.TRANSIENT, start_seconds=60.0, end_seconds=60.4
            ),
        ]
        triggers = triggers_from_audio(events, config.analysis)
        assert {round(t.start_seconds) for t in triggers} == {10, 60}

    def test_scene_confidence_scales_with_the_measured_change(self, config) -> None:
        result = SceneResult(
            scenes=(
                Scene(index=0, start_seconds=0.0, end_seconds=10.0),
                Scene(index=1, start_seconds=10.0, end_seconds=20.0, change_score=30.0),
                Scene(index=2, start_seconds=20.0, end_seconds=30.0, change_score=110.0),
            ),
            duration_seconds=30.0,
            detector="content",
            threshold=27.0,
        )
        triggers = triggers_from_scenes(result, config.analysis)
        # The first scene has no boundary before it.
        assert len(triggers) == 2
        assert triggers[1].confidence > triggers[0].confidence

    def test_a_boundary_without_a_score_still_nominates(self, config) -> None:
        result = SceneResult(
            scenes=(
                Scene(index=0, start_seconds=0.0, end_seconds=5.0),
                Scene(index=1, start_seconds=5.0, end_seconds=10.0, change_score=None),
            ),
            duration_seconds=10.0,
            detector="content",
            threshold=27.0,
        )
        assert len(triggers_from_scenes(result, config.analysis)) == 1

    def test_frame_difference_respects_its_threshold(self, config) -> None:
        threshold = config.analysis.vision.frame_difference_threshold
        scores = [(1.0, threshold - 0.01), (2.0, threshold + 0.01), (3.0, 0.9)]
        triggers = triggers_from_frame_difference(scores, config.analysis)
        assert [t.start_seconds for t in triggers] == [2.0, 3.0]

    def test_a_disabled_detector_nominates_nothing(self, config) -> None:
        # §91: the cascade is tuned in configuration, not in code.
        vision = config.analysis.vision.model_copy(update={"candidate_detectors": ["scene_change"]})
        analysis = config.analysis.model_copy(update={"vision": vision})
        assert triggers_from_audio([_spike(10.0)], analysis) == []
        assert triggers_from_frame_difference([(1.0, 0.9)], analysis) == []

    def test_transcript_segments_nominate_speech(self, config) -> None:
        from ai.providers.base import TranscriptSegment

        segments = [
            TranscriptSegment(start=5.0, end=7.0, text="got him", confidence=0.8),
            TranscriptSegment(start=9.0, end=9.0, text="", confidence=0.1),
        ]
        triggers = triggers_from_transcript(segments, config.analysis)
        assert len(triggers) == 1
        assert triggers[0].source == SPEECH_ACTIVITY


class TestFrameBudget:
    """§15's ceiling: model work per hour of source is bounded, always."""

    @pytest.mark.parametrize("hours", [0.5, 1, 2, 4, 6, 8])
    def test_the_ceiling_holds_at_every_source_length(self, config, hours: float) -> None:
        duration = hours * HOUR
        # One spike every 25 seconds: relentless, and far more than the budget.
        triggers = triggers_from_audio(
            [_spike(t) for t in range(10, int(duration), 25)], config.analysis
        )
        plan = build_candidates(triggers, config.analysis, duration_seconds=duration)

        allowed = config.analysis.vision.max_frames_per_source_hour * hours
        assert plan.frames_planned <= allowed + 1
        assert plan.frames_planned == len(plan.keyframes)

    def test_an_uneventful_recording_stays_well_under_the_ceiling(self, config) -> None:
        # The budget is a ceiling, not a target: nothing is invented to fill it.
        triggers = triggers_from_audio([_spike(t) for t in (300, 900, 1500)], config.analysis)
        plan = build_candidates(triggers, config.analysis, duration_seconds=2 * HOUR)
        assert plan.frames_planned == 12
        assert not plan.was_capped
        assert plan.coverage < 0.05

    def test_dropped_regions_are_reported_not_hidden(self, config) -> None:
        # A silent truncation would read as "we looked at everything".
        # Eight hours allows 1 920 frames, i.e. 480 regions at four each.
        # Eight hundred nominations is comfortably more than that.
        triggers = triggers_from_audio(
            [_spike(index * 35.0) for index in range(800)], config.analysis
        )
        plan = build_candidates(triggers, config.analysis, duration_seconds=8 * HOUR)
        assert plan.was_capped
        assert plan.dropped_regions > 0
        assert len(plan.analysed_regions) + plan.dropped_regions == len(plan.regions)
        assert plan.summary()["dropped_regions"] == plan.dropped_regions

    def test_a_dropped_region_keeps_its_evidence(self, config) -> None:
        # It stays in the plan without keyframes, so a re-run with a larger
        # budget can reach it and nothing about why it mattered is lost.
        triggers = triggers_from_audio(
            [_spike(index * 35.0) for index in range(800)], config.analysis
        )
        plan = build_candidates(triggers, config.analysis, duration_seconds=8 * HOUR)
        dropped = [region for region in plan.regions if not region.is_analysed]
        assert dropped
        assert all(region.triggers for region in dropped)
        assert all(region.sources for region in dropped)

    def test_a_source_with_no_triggers_plans_nothing(self, config) -> None:
        plan = build_candidates([], config.analysis, duration_seconds=HOUR)
        assert len(plan) == 0
        assert plan.frames_planned == 0
        assert plan.coverage == 0.0


class TestRegionShape:
    def test_a_region_never_swallows_the_recording(self, config) -> None:
        # The failure this prevents: constant activity merging into one region
        # that spans two hours and receives four keyframes, while the plan
        # reports full coverage.
        triggers = triggers_from_audio(
            [_spike(t) for t in range(10, 7200, 30)], config.analysis
        )
        plan = build_candidates(triggers, config.analysis, duration_seconds=2 * HOUR)
        assert len(plan) > 50
        assert max(region.duration for region in plan.regions) <= 70.0

    def test_overlapping_nominations_become_one_region(self, config) -> None:
        # Two detectors noticing the same explosion must not cost the budget
        # twice.
        triggers = [_trigger(AUDIO_SPIKE, 100.0), _trigger(SCENE_CHANGE, 101.0)]
        plan = build_candidates(triggers, config.analysis, duration_seconds=HOUR)
        assert len(plan) == 1
        assert plan.regions[0].sources == frozenset({AUDIO_SPIKE, SCENE_CHANGE})

    def test_pre_roll_and_post_roll_widen_the_span(self, config) -> None:
        sampling = config.analysis.frame_sampling
        plan = build_candidates(
            [_trigger(AUDIO_SPIKE, 100.0)], config.analysis, duration_seconds=HOUR
        )
        region = plan.regions[0]
        assert region.start_seconds == pytest.approx(100.0 - sampling.candidate_pre_roll_seconds)
        assert region.end_seconds == pytest.approx(
            100.5 + sampling.candidate_post_roll_seconds
        )

    def test_a_region_is_clamped_to_the_source(self, config) -> None:
        plan = build_candidates(
            [_trigger(AUDIO_SPIKE, 2.0), _trigger(AUDIO_SPIKE, 118.0)],
            config.analysis,
            duration_seconds=120.0,
        )
        assert plan.regions[0].start_seconds == 0.0
        assert plan.regions[-1].end_seconds <= 120.0

    def test_keyframes_sit_inside_their_region(self, config) -> None:
        plan = build_candidates(
            [_trigger(AUDIO_SPIKE, 100.0)], config.analysis, duration_seconds=HOUR
        )
        region = plan.regions[0]
        assert region.keyframes
        assert all(
            region.start_seconds < timestamp < region.end_seconds
            for timestamp in region.keyframes
        )

    def test_regions_are_returned_in_chronological_order(self, config) -> None:
        triggers = triggers_from_audio(
            [_spike(t) for t in (600.0, 100.0, 1200.0)], config.analysis
        )
        plan = build_candidates(triggers, config.analysis, duration_seconds=HOUR)
        starts = [region.start_seconds for region in plan.regions]
        assert starts == sorted(starts)


class TestRanking:
    """§27's principle applied before any model runs: agreement beats intensity."""

    def test_agreement_outranks_a_single_loud_signal(self, config) -> None:
        agreed = [
            _trigger(AUDIO_SPIKE, 100.0, confidence=0.5),
            _trigger(SCENE_CHANGE, 100.0, confidence=0.5),
            _trigger(FRAME_DIFFERENCE, 100.0, confidence=0.5),
        ]
        loud = [_trigger(AUDIO_SPIKE, 1000.0, confidence=1.0)]
        plan = build_candidates(agreed + loud, config.analysis, duration_seconds=HOUR)
        by_start = {round(region.start_seconds): region for region in plan.regions}
        assert by_start[80].priority > by_start[980].priority

    def test_the_budget_is_spent_on_the_strongest_regions_first(self, config) -> None:
        weak = [_trigger(AUDIO_SPIKE, index * 200.0, confidence=0.2) for index in range(1, 40)]
        strong = [
            _trigger(AUDIO_SPIKE, 100.0, confidence=1.0),
            _trigger(SCENE_CHANGE, 100.0, confidence=1.0),
            _trigger(FRAME_DIFFERENCE, 100.0, confidence=1.0),
            _trigger(SPEECH_ACTIVITY, 100.0, confidence=1.0),
        ]
        plan = build_candidates(
            weak + strong, config.analysis, duration_seconds=2 * HOUR, frame_budget=4
        )
        analysed = plan.analysed_regions
        assert len(analysed) == 1
        assert analysed[0].agreement == 4

    def test_priority_is_bounded(self, config) -> None:
        triggers = [
            _trigger(source, 100.0, confidence=1.0)
            for source in config.analysis.vision.candidate_detectors
        ]
        plan = build_candidates(triggers, config.analysis, duration_seconds=HOUR)
        assert 0.0 <= plan.regions[0].priority <= 1.0


class TestCoverageReporting:
    def test_coverage_describes_what_was_actually_looked_at(self, config) -> None:
        # The honest headline for §60: the model saw a fraction of the
        # recording, chosen by detectors.
        triggers = triggers_from_audio([_spike(1800.0)], config.analysis)
        plan = build_candidates(triggers, config.analysis, duration_seconds=2 * HOUR)
        assert 0.0 < plan.coverage < 0.02
        assert plan.summary()["coverage"] == round(plan.coverage, 4)

    def test_coverage_counts_only_analysed_regions(self, config) -> None:
        triggers = triggers_from_audio(
            [_spike(index * 120.0) for index in range(200)], config.analysis
        )
        plan = build_candidates(
            triggers, config.analysis, duration_seconds=8 * HOUR, frame_budget=8
        )
        assert len(plan.analysed_regions) == 2
        assert plan.coverage < 0.01
