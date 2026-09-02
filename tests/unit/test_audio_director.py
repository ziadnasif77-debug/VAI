"""V2-P5: sound stops being the only stage that cannot hear the session.

Cutting, pacing, emphasis and QA all read the Semantic Timeline.
``backend/rendering/`` did not import it at all, and four things followed from
that -- one bed for the whole video, no ducking under game audio, ducking that
depended on captions being switched on, and silence treated as a defect in two
places and as a tool in none.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from backend.audio_director.plan import (
    MIN_SECTION_SECONDS,
    SHELF_FOR,
    SILENCE_BEFORE_PAYOFF,
    AudioPlan,
    plan_audio,
    shelf_directory,
)
from backend.semantic.reader import ShapeSegment

pytestmark = pytest.mark.unit


class _Session:
    """A reader over a shape and an audio lane written by hand."""

    def __init__(self, segments, *, audio=None, hz=2, duration=300.0):
        self.media_id = "programme"
        self.hz = hz
        self.duration_s = duration
        self._segments = [ShapeSegment(*item) for item in segments]
        n = int(duration * hz)
        self._lanes = {
            "audio": list(audio or [0.0] * n),
            "intensity": [0.3] * n,
        }

    def lane(self, name):
        return self._lanes[name]

    def window(self, name, start, end):
        a, b = int(start * self.hz), int(end * self.hz)
        return self._lanes[name][a : b + 1] or [0.0]

    def value_at(self, name, seconds):
        return self._lanes[name][min(len(self._lanes[name]) - 1, int(seconds * self.hz))]

    def intensity_between(self, start, end):
        return 0.3

    def level_for(self, start, end):
        return "normal"

    def shape(self, *, min_segment=None):
        return list(self._segments)

    def summary(self):
        return []


class TestMusicFollowsTheSession:
    def test_a_bed_per_section_rather_than_one_for_the_video(self, config) -> None:
        # A session that opens quiet and ends in a boss fight got the same bed
        # over both, chosen from a single average of the whole edit.
        session = _Session(
            [(0.0, 100.0, "calm"), (100.0, 200.0, "climax"), (200.0, 300.0, "normal")]
        )

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=config
        )

        assert [section.shelf for section in plan.sections] == ["low", "peak", "low"]
        assert plan.sections[-1].end_seconds == pytest.approx(300.0)

    def test_the_shelves_cover_every_level(self) -> None:
        assert set(SHELF_FOR) == {"calm", "normal", "tension", "high", "climax"}

    def test_a_stretch_too_short_to_be_worth_a_swap_is_absorbed(self, config) -> None:
        # Swapping the bed every four seconds is not scoring, it is fidgeting.
        session = _Session(
            [(0.0, 100.0, "calm"), (100.0, 104.0, "climax"), (104.0, 300.0, "calm")]
        )

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=config
        )

        assert len(plan.sections) == 1
        assert MIN_SECTION_SECONDS > 4.0

    def test_neighbouring_sections_never_share_a_bed(self, config) -> None:
        session = _Session(
            [(0.0, 100.0, "calm"), (100.0, 200.0, "normal"), (200.0, 300.0, "climax")]
        )

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=config
        )

        shelves = [section.shelf for section in plan.sections]
        assert all(a != b for a, b in pairwise(shelves))

    def test_with_the_setting_off_it_behaves_as_it_always_did(self, config) -> None:
        static = config.model_copy(
            update={
                "audio": config.audio.model_copy(
                    update={
                        "music": config.audio.music.model_copy(
                            update={"change_on_section": False}
                        )
                    }
                )
            }
        )
        session = _Session([(0.0, 150.0, "calm"), (150.0, 300.0, "climax")])

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=static
        )

        assert plan.sections == ()

    def test_without_a_reader_there_are_no_sections(self, config) -> None:
        plan = plan_audio(
            reader=None, duration_seconds=300.0, spoken=(), beats=(), config=config
        )

        assert plan.sections == ()

    def test_a_shelf_that_does_not_exist_falls_back(self, tmp_path) -> None:
        (tmp_path / "low").mkdir()

        assert shelf_directory(tmp_path, "low") == tmp_path / "low"
        assert shelf_directory(tmp_path, "peak") == tmp_path


class TestDuckingUnderTheGame:
    def test_loud_game_audio_ducks_the_music(self, config) -> None:
        # `event_spans` has existed since Phase 12 and `game_event_duck_db`
        # has been configured beside it; the only caller was a unit test.
        loud = [0.1] * 40 + [0.9] * 20 + [0.1] * 540
        session = _Session([(0.0, 300.0, "normal")], audio=loud)

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=config
        )

        assert plan.event_spans, "the game's own loud moments duck the bed"
        span = plan.event_spans[0]
        assert span.start == pytest.approx(20.0)
        assert span.end == pytest.approx(30.0)
        # And how loud it got travels with it (V2-P2.6). The comparison that
        # defines the span already measured this; it used to be discarded.
        assert span.peak == pytest.approx(0.9)

    def test_the_peak_is_the_loudest_instant_not_the_first(self, config) -> None:
        # A span that starts at 0.78 and builds to 0.99 is an explosion, and
        # reading its opening value would price it as a footstep.
        loud = [0.1] * 40 + [0.78] * 6 + [0.99] * 6 + [0.80] * 8 + [0.1] * 540
        session = _Session([(0.0, 300.0, "normal")], audio=loud)

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=config
        )

        assert plan.event_spans[0].peak == pytest.approx(0.99)

    def test_two_loud_stretches_keep_their_own_peaks(self, config) -> None:
        quiet_then_loud = (
            [0.1] * 20 + [0.78] * 20 + [0.1] * 20 + [1.0] * 20 + [0.1] * 520
        )
        session = _Session([(0.0, 300.0, "normal")], audio=quiet_then_loud)

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=config
        )

        assert [round(span.peak, 2) for span in plan.event_spans] == [0.78, 1.0]

    def test_a_brief_spike_is_not_a_span(self, config) -> None:
        session = _Session([(0.0, 300.0, "normal")], audio=[0.1] * 40 + [0.9] + [0.1] * 559)

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=config
        )

        assert plan.event_spans == ()

    def test_with_ducking_off_nothing_is_planned(self, config) -> None:
        quiet = config.model_copy(
            update={
                "audio": config.audio.model_copy(
                    update={
                        "ducking": config.audio.ducking.model_copy(update={"enabled": False})
                    }
                )
            }
        )
        session = _Session([(0.0, 300.0, "normal")], audio=[0.9] * 600)

        plan = plan_audio(
            reader=session, duration_seconds=300.0, spoken=(), beats=(), config=quiet
        )

        assert plan.event_spans == ()


class TestSilenceAsATool:
    def test_the_music_holds_its_breath_before_a_payoff(self, config) -> None:
        session = _Session([(0.0, 300.0, "normal")])

        plan = plan_audio(
            reader=session,
            duration_seconds=300.0,
            spoken=(),
            beats=[(120.0, 0.9)],
            config=config,
        )

        assert len(plan.silences) == 1
        gap = plan.silences[0]
        assert gap.end_seconds == pytest.approx(120.0)
        assert gap.start_seconds == pytest.approx(120.0 - SILENCE_BEFORE_PAYOFF)
        assert gap.reason

    def test_never_under_a_speaker(self, config) -> None:
        # A gap while somebody is talking is a dropout, not a beat.
        session = _Session([(0.0, 300.0, "normal")])

        plan = plan_audio(
            reader=session,
            duration_seconds=300.0,
            spoken=[(118.0, 123.0)],
            beats=[(120.0, 0.9)],
            config=config,
        )

        assert plan.silences == ()

    def test_never_for_a_beat_nobody_is_sure_of(self, config) -> None:
        # Silence over something that turns out to be nothing is a hole.
        session = _Session([(0.0, 300.0, "normal")])

        plan = plan_audio(
            reader=session,
            duration_seconds=300.0,
            spoken=(),
            beats=[(120.0, 0.2)],
            config=config,
        )

        assert plan.silences == ()

    def test_two_payoffs_too_close_share_one_breath(self, config) -> None:
        session = _Session([(0.0, 300.0, "normal")])

        plan = plan_audio(
            reader=session,
            duration_seconds=300.0,
            spoken=(),
            beats=[(120.0, 0.9), (120.3, 0.85)],
            config=config,
        )

        assert len(plan.silences) == 1


class TestQaKnowsTheDifference:
    def test_a_planned_gap_is_not_reported_as_a_defect(self, config) -> None:
        from backend.core.models.enums import TrackKind, TransitionType
        from backend.qa.content import ContentInputs, _extreme_silence
        from backend.timeline.models import Timeline, TimelineClip, Track

        clip = TimelineClip(
            id="clip-000000000000",
            media_id="media-aaaaaaaaaaaa",
            track=TrackKind.VIDEO,
            clip_index=0,
            source_in=0.0,
            source_out=60.0,
            timeline_start=0.0,
            timeline_end=60.0,
            transition_in=TransitionType.CUT,
            transition_out=TransitionType.CUT,
        )
        timeline = Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
            Track(kind=TrackKind.VIDEO, clips=(clip,))
        )
        long_gap = [("media-aaaaaaaaaaaa", 10.0, 40.0)]

        reported = _extreme_silence(
            timeline, ContentInputs(silences=long_gap), config.qa
        )
        excused = _extreme_silence(
            timeline,
            ContentInputs(silences=long_gap, planned_silences=[(9.0, 41.0)]),
            config.qa,
        )

        assert reported.warned, "an unplanned half-minute of nothing is still a defect"
        assert not excused.warned, "the system does not report its own decision"


def test_an_empty_plan_says_so() -> None:
    assert AudioPlan().is_empty


class TestTheProgrammeReader:
    """Sound works in the finished video's time; every reader before this
    spoke source time, which is why the mix could not ask what the session was
    doing at 2:41 of the edit."""

    def _source(self, values, hz=2):
        class _Reader:
            media_id = "media-aaaaaaaaaaaa"

            def __init__(self):
                self.hz = hz
                self.duration_s = len(values) / hz
                self.lanes = {name: list(values) for name in ("intensity", "tension",
                              "motion", "audio", "events", "speech", "scene_changes",
                              "novelty", "dead_zones")}
                self._robust_range = (0.0, 1.0)

            def value_at(self, name, seconds):
                lane = self.lanes[name]
                return lane[max(0, min(len(lane) - 1, int(seconds * hz)))]

        return _Reader()

    def _clip(self, source_in, source_out, timeline_start):
        from backend.core.models.enums import TrackKind, TransitionType
        from backend.timeline.models import TimelineClip

        return TimelineClip(
            id=f"clip-{int(timeline_start):012d}",
            media_id="media-aaaaaaaaaaaa",
            track=TrackKind.VIDEO,
            clip_index=int(timeline_start),
            source_in=source_in,
            source_out=source_out,
            timeline_start=timeline_start,
            timeline_end=timeline_start + (source_out - source_in),
            transition_in=TransitionType.CUT,
            transition_out=TransitionType.CUT,
        )

    def test_a_programme_second_carries_the_source_second_the_edit_put_there(
        self, config
    ) -> None:
        from backend.semantic.programme import ProgrammeReader

        # Source: quiet for 10s, loud for 10s. The edit keeps only the loud
        # half and puts it at the start of the video.
        source = self._source([0.1] * 20 + [0.9] * 20)
        reader = ProgrammeReader.build(
            [self._clip(10.0, 20.0, 0.0)],
            {"media-aaaaaaaaaaaa": source},
            duration_seconds=10.0,
            config=config,
        )

        assert reader.value_at("intensity", 1.0) == pytest.approx(0.9)

    def test_a_level_is_graded_on_the_session_and_not_on_the_cut(
        self, config
    ) -> None:
        # Selection removes the valleys, so an edit graded against itself
        # reads uniformly normal -- on the real session that produced one
        # music section over a video containing three payoffs.
        from backend.semantic.programme import ProgrammeReader

        source = self._source([0.05] * 40 + [0.95] * 40)
        reader = ProgrammeReader.build(
            [self._clip(20.0, 40.0, 0.0)],
            {"media-aaaaaaaaaaaa": source},
            duration_seconds=20.0,
            config=config,
        )

        assert reader.level_for(1.0, 3.0) in ("high", "climax")

    def test_without_clips_or_readers_there_is_no_programme(self, config) -> None:
        from backend.semantic.programme import ProgrammeReader

        assert ProgrammeReader.build([], {}, duration_seconds=10.0, config=config) is None
