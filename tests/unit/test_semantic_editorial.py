"""V2 P1: the Semantic Timeline, dynamic pacing, and the constitution.

The design ("المخرج داخل الزمن") in tests: meaning per half-second fused
from stored evidence, cut lengths that follow the session's own heat, and
the one law no engine may break -- chronology is immutable after selection.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from backend.core.errors import ValidationError
from backend.editorial import pacing_engine
from backend.semantic.timeline import build_timeline
from backend.timeline.builder import PlannedClip
from backend.timeline.validation import ensure_chronological

pytestmark = pytest.mark.unit


def _timeline(config, **overrides):
    world = {
        "media_id": "m",
        "duration_seconds": 120.0,
        "frames": [(t, 0.1) for t in range(0, 121, 3)],
        "audio_events": [],
        "game_events": [],
        "scenes": [],
        "words": [],
        "dead_spans": [],
        "config": config,
    }
    world.update(overrides)
    return build_timeline(**world)


class TestSemanticTimeline:
    def test_events_and_motion_make_a_peak_where_they_happen(self, config) -> None:
        quiet_frames = [(t, 0.05) for t in range(0, 121, 3)]
        hot = [(t, 0.9) for t in range(60, 75, 3)]
        frames = [f for f in quiet_frames if not 60 <= f[0] < 75] + hot
        timeline = _timeline(
            config,
            frames=frames,
            game_events=[(62.0, 68.0, 0.9, "boss_fight")],
            audio_events=[(61.0, 69.0, -8.0, "spike"), (10.0, 12.0, -40.0, "hum")],
        )

        assert timeline.intensity_between(60, 70) > timeline.intensity_between(10, 20)
        assert timeline.level_for(60, 70) in ("high", "climax")
        assert timeline.level_for(10, 20) in ("calm", "normal")

    def test_a_flat_session_still_has_a_middle_not_a_zero(self, config) -> None:
        # §23's lesson generalised: percentiles inside the session.
        timeline = _timeline(config)  # identical motion everywhere

        value = timeline.intensity_between(30, 40)
        assert 0.05 < value < 0.6, "flat evidence ranks mid, never zero"

    def test_dead_screens_carry_no_intensity(self, config) -> None:
        timeline = _timeline(
            config,
            frames=[(t, 0.9) for t in range(0, 121, 3)],
            dead_spans=[(50.0, 60.0)],
        )

        assert timeline.intensity_between(51, 59) == 0.0

    def test_shape_merges_slivers_and_names_levels(self, config) -> None:
        frames = [(t, 0.9 if 40 <= t < 80 else 0.05) for t in range(0, 121, 3)]
        timeline = _timeline(
            config,
            frames=frames,
            game_events=[(50.0, 70.0, 0.95, "chaos")],
            audio_events=[(45.0, 75.0, -5.0, "roar"), (5.0, 8.0, -50.0, "hum")],
        )

        shape = timeline.shape()
        assert len(shape) >= 2
        levels = [segment.level for segment in shape]
        assert any(level in ("high", "climax") for level in levels)
        assert all(segment.seconds >= 3.9 for segment in shape[1:]), "slivers merged"
        # summary is the §80 face of the same thing
        summary = timeline.summary()
        assert summary[0]["start_seconds"] == 0.0
        assert summary[-1]["end_seconds"] == 120.0


class TestDynamicPacing:
    def _clip(self, start, end):
        return PlannedClip(media_id="m", source_start=start, source_end=end)

    def test_a_hot_stretch_gets_the_climax_cap(self, config) -> None:
        timeline = _timeline(
            config,
            frames=[(t, 0.95 if 40 <= t < 80 else 0.02) for t in range(0, 121, 3)],
            game_events=[(45.0, 75.0, 0.95, "chaos")],
            audio_events=[(45.0, 75.0, -5.0, "roar"), (5.0, 8.0, -50.0, "hum")],
        )

        hot_cap = pacing_engine.cap_for(
            self._clip(50, 70), timeline, config, fallback=75.0
        )
        calm_cap = pacing_engine.cap_for(
            self._clip(5, 20), timeline, config, fallback=75.0
        )

        assert hot_cap <= config.editorial.pacing.bands.high.max
        assert calm_cap >= config.editorial.pacing.bands.normal.max
        assert hot_cap < calm_cap, "the session's heat sets the cut length"

    def test_dynamic_off_returns_the_static_fallback(self, config) -> None:
        quiet = _timeline(config)
        static = config.model_copy(
            update={
                "editorial": config.editorial.model_copy(
                    update={
                        "pacing": config.editorial.pacing.model_copy(
                            update={"dynamic": False}
                        )
                    }
                )
            }
        )

        assert pacing_engine.cap_for(self._clip(0, 50), quiet, static, fallback=75.0) == 75.0
        assert pacing_engine.cap_for(self._clip(0, 50), None, config, fallback=75.0) == 75.0


class TestChronologyConstitution:
    """The owner's law, as a test that tries to break it."""

    def _clip(self, start, role="body"):
        return PlannedClip(
            media_id="m", source_start=start, source_end=start + 10, role=role
        )

    def test_time_only_runs_forward(self) -> None:
        ensure_chronological([self._clip(10), self._clip(30), self._clip(50)])

    def test_a_reordered_plan_is_refused_not_warned(self) -> None:
        with pytest.raises(ValidationError, match="chronology_violated"):
            ensure_chronological([self._clip(10), self._clip(50), self._clip(30)])

    def test_the_hook_is_the_single_sanctioned_exception(self) -> None:
        ensure_chronological(
            [self._clip(300, role="hook"), self._clip(10), self._clip(50)]
        )

    def test_a_hook_anywhere_else_obeys_time(self) -> None:
        with pytest.raises(ValidationError, match="chronology_violated"):
            ensure_chronological(
                [self._clip(10), self._clip(300, role="hook"), self._clip(50)]
            )

    def test_two_recordings_keep_their_own_clocks(self) -> None:
        a = PlannedClip(media_id="a", source_start=100, source_end=110)
        b = PlannedClip(media_id="b", source_start=5, source_end=15)
        ensure_chronological([a, b])


class TestJumpCutTightening:
    """Pieces of a hot slab skip a sliver of source between them: played
    contiguously they would be one unbroken shot no viewer could feel."""

    def test_a_seamless_hot_slab_is_capped_and_tightened(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        pieces = _split_long_clips(
            [PlannedClip(media_id="m", source_start=100.0, source_end=140.0)],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=lambda piece: 2.5,
            jump_cut_gap=0.35,
            jump_cut_below=8.0,
        )

        assert len(pieces) >= 12, "40s at cap 2.5 cuts into many pieces"
        for piece in pieces[:-1]:
            assert piece.source_end - piece.source_start <= 2.5 + 1e-6
        skips = [b.source_start - a.source_end for a, b in pairwise(pieces)]
        assert all(abs(skip - 0.35) < 1e-6 for skip in skips), "every seam skips"


class TestExclusiveKeepsShortDisjointPieces:
    def test_a_short_disjoint_piece_is_an_editorial_decision(self) -> None:
        from backend.timeline.builder import _exclusive

        pieces = [
            PlannedClip(media_id="m", source_start=10.0, source_end=11.2),
            PlannedClip(media_id="m", source_start=11.5, source_end=12.9),
        ]
        kept, notes = _exclusive(pieces)

        assert len(kept) == 2, "disjoint climax pieces survive whole"
        assert notes == []

    def test_a_deduped_fragment_below_the_floor_still_drops(self) -> None:
        from backend.timeline.builder import _exclusive

        clips = [
            PlannedClip(media_id="m", source_start=10.0, source_end=30.0),
            PlannedClip(media_id="m", source_start=28.5, source_end=31.0),
        ]
        kept, notes = _exclusive(clips)

        assert len(kept) == 1, "a 1s leftover of a swallowed clip is a glitch"
        assert any("already in the edit" in note for note in notes)


class TestTheWalkFollowsTheHeat:
    """A planned clip that spans a calm setup AND the fight it leads into is
    two editorial things; the cut length answers to the second it starts on,
    not to an average of the whole slab."""

    def test_one_clip_two_paces(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        clip = PlannedClip(media_id="m", source_start=0.0, source_end=60.0)

        def cap(piece):
            # calm until 40s, climax after it
            return 15.0 if piece.source_start < 40.0 else 1.5

        pieces = _split_long_clips(
            [clip],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=cap,
            jump_cut_gap=0.35,
            jump_cut_below=8.0,
        )

        calm = [p for p in pieces if p.source_start < 40.0]
        hot = [p for p in pieces if p.source_start >= 40.0]
        assert calm and hot
        assert max(p.source_end - p.source_start for p in calm) > 8.0, "setup breathes"
        assert max(p.source_end - p.source_start for p in hot) <= 1.5 + 1e-6
        # The piece that straddles the change is graded where it STARTS, so
        # the first hot piece begins one calm cap in: the pace turns within
        # a piece of the heat, not before it.
        assert len(hot) >= 9, "the climax is many pieces, not one slab"

    def test_the_pieces_cover_the_clip_in_order(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        pieces = _split_long_clips(
            [PlannedClip(media_id="m", source_start=100.0, source_end=140.0)],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=lambda piece: 3.0,
        )

        assert pieces[0].source_start == 100.0
        assert pieces[-1].source_end == 140.0
        for before, after in pairwise(pieces):
            assert after.source_start >= before.source_end, "time only runs forward"

    def test_a_calm_walk_leaves_no_skips(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        pieces = _split_long_clips(
            [PlannedClip(media_id="m", source_start=0.0, source_end=60.0)],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=lambda piece: 15.0,
            jump_cut_gap=0.35,
            jump_cut_below=8.0,
        )

        skips = [b.source_start - a.source_end for a, b in pairwise(pieces)]
        assert all(skip == 0.0 for skip in skips), "calm pieces stay contiguous"

    def test_the_static_path_still_ships_a_seamless_slab_whole(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        pieces = _split_long_clips(
            [PlannedClip(media_id="m", source_start=100.0, source_end=190.0)],
            scenes_by_media={},
            max_seconds=75.0,
        )

        assert len(pieces) == 1, "V1 judgement: no seam, no arithmetic midpoint"


class TestAShotDoesNotSpanALevelChange:
    def test_the_piece_ends_where_the_session_turns(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        # Calm until 40s, climax after. Without the stop, the piece starting
        # at 30 would run to 45 at the calm pace, seven seconds deep into the
        # heat it cannot feel.
        pieces = _split_long_clips(
            [PlannedClip(media_id="m", source_start=0.0, source_end=60.0)],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=lambda piece: 15.0 if piece.source_start < 40.0 else 1.5,
            level_stops_by_media={"m": [0.0, 40.0]},
            jump_cut_gap=0.35,
            jump_cut_below=8.0,
        )

        crossing = [
            p for p in pieces if p.source_start < 40.0 < p.source_end
        ]
        assert not crossing, "no shot straddles the turn"
        assert any(abs(p.source_end - 40.0) < 1e-6 for p in pieces), "one ends on it"


class TestTheFinerShape:
    def test_a_short_burst_is_no_section_but_is_a_turn(self, config) -> None:
        frames = [(t, 0.95 if 40 <= t < 43 else 0.05) for t in range(0, 121, 1)]
        timeline = _timeline(
            config,
            frames=frames,
            game_events=[(40.0, 43.0, 0.95, "burst")],
            audio_events=[(40.0, 43.0, -5.0, "bang"), (5.0, 8.0, -50.0, "hum")],
        )

        narrative = timeline.shape()
        pacing = timeline.shape(min_segment=2.0)

        assert len(pacing) > len(narrative), "the finer shape sees the burst"
        assert any(
            level in ("high", "climax") for level in (s.level for s in pacing)
        )


class TestQaSpeaksV2sGrammar:
    """QA judged every shot by one flat floor, so V2's climax band -- 0.8 to
    1.8s on purpose -- read as a defect, and the plan's clip density was
    relayed as a verdict on a render the guard had since split."""

    def _timeline_of(self, durations):
        from backend.core.models.enums import TrackKind, TransitionType
        from backend.timeline.models import Timeline, TimelineClip, Track

        clips = []
        cursor = 0.0
        for index, seconds in enumerate(durations):
            clips.append(
                TimelineClip(
                    id=f"clip-{index:012d}",
                    media_id="media-aaaaaaaaaaaa",
                    track=TrackKind.VIDEO,
                    clip_index=index,
                    source_in=100.0 + cursor,
                    source_out=100.0 + cursor + seconds,
                    timeline_start=cursor,
                    timeline_end=cursor + seconds,
                    transition_in=TransitionType.CUT,
                    transition_out=TransitionType.CUT,
                )
            )
            cursor += seconds
        return Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
            Track(kind=TrackKind.VIDEO, clips=tuple(clips))
        )

    def test_a_climax_shot_is_the_pace_not_a_flash(self) -> None:
        from backend.qa.content import _bad_transitions

        timeline = self._timeline_of([0.9, 1.0, 5.0])

        deliberate = _bad_transitions(timeline, {0: "climax", 1: "high", 2: "normal"})
        blind = _bad_transitions(timeline, {})

        assert not deliberate.warned, "the hot band's own lengths are intended"
        assert blind.warned, "without levels the flat floor still applies"

    def test_density_is_measured_on_the_edit_not_on_the_plan(self) -> None:
        from backend.qa.content import _clip_density

        dense = _clip_density(self._timeline_of([3.0] * 20))
        dragging = _clip_density(self._timeline_of([60.0, 60.0]))

        assert not dense.warned
        assert dragging.warned
        assert "shots per minute" in dragging.message

    def test_the_plans_density_note_is_not_relayed_as_a_verdict(self) -> None:
        from backend.qa.content import ContentInputs, _broken_sequence

        superseded = _broken_sequence(
            ContentInputs(pacing_warnings=("only 1.0 clips per minute; the edit may drag",))
        )
        real = _broken_sequence(
            ContentInputs(pacing_warnings=("intensity does not build across the video",))
        )

        assert not superseded.warned, "the guard answers density; the plan cannot"
        assert real.warned


class TestTheCriticTightensButDoesNotErase:
    def test_a_trim_that_would_leave_a_flash_is_refused(self, config) -> None:
        from backend.semantic.levels import floor_for

        # A climax shot's band starts at 0.8s; half of that is the readability
        # floor, which is where the Critic must stop.
        assert floor_for("climax", config) == pytest.approx(0.8)
        assert floor_for("calm", config) == pytest.approx(4.0)
        assert floor_for(None, config) == pytest.approx(0.8), "no level, no licence"

    def test_the_floor_is_read_from_the_configured_bands(self, config) -> None:
        from backend.semantic.levels import floor_for

        bands = config.editorial.pacing.bands
        for level in ("calm", "normal", "tension", "high", "climax"):
            expected = max(
                config.editorial.pacing.min_piece_seconds,
                getattr(bands, level).min * 0.5,
            )
            assert floor_for(level, config) == pytest.approx(expected)
