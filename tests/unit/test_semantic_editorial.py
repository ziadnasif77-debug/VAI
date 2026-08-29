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
    """A hot slab with no natural seams still honours its cap -- the pieces
    skip a sliver of source between them, which is the felt jump-cut."""

    def _slab(self, seconds=40.0):
        return PlannedClip(media_id="m", source_start=100.0, source_end=100.0 + seconds)

    def test_a_seamless_hot_slab_is_capped_and_tightened(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        pieces = _split_long_clips(
            [self._slab()],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=lambda clip: 2.5,
            jump_cut_gap=0.35,
            jump_cut_below=8.0,
        )

        assert len(pieces) >= 14, "40s at cap 2.5 cuts into many pieces"
        for piece in pieces:
            assert piece.source_end - piece.source_start <= 2.5 + 1e-6
        skips = [
            after.source_start - before.source_end
            for before, after in pairwise(pieces)
        ]
        assert all(abs(skip - 0.35) < 1e-6 for skip in skips), "every seam skips"

    def test_a_calm_slab_keeps_its_breath(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        pieces = _split_long_clips(
            [self._slab()],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=lambda clip: 15.0,
            jump_cut_gap=0.35,
            jump_cut_below=8.0,
        )

        assert 2 <= len(pieces) <= 3, "40s at cap 15 divides evenly, no shred"
        skips = [
            after.source_start - before.source_end
            for before, after in pairwise(pieces)
        ]
        assert all(skip == 0.0 for skip in skips), "calm pieces stay contiguous"

    def test_the_static_path_still_ships_a_seamless_slab_whole(self) -> None:
        from backend.timeline.screen_guard import _split_long_clips

        pieces = _split_long_clips(
            [self._slab(90.0)],
            scenes_by_media={},
            max_seconds=75.0,
        )

        assert len(pieces) == 1, "V1 judgement: no seam, no arithmetic midpoint"


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
