"""Clip boundaries against what the screen was showing.

Both rules exist because the first fully autonomous video shipped their
absence: a hook opening on the record-button click at second 0.0, and a
398-second body slab with no seam in it.
"""

from __future__ import annotations

from itertools import pairwise
from types import SimpleNamespace

import pytest

from backend.analysis.frame_state import StateSpan
from backend.core.models.enums import FrameState, MomentType
from backend.timeline.builder import PlannedClip
from backend.timeline.screen_guard import guard_clips

pytestmark = pytest.mark.unit


def _clip(start: float, end: float, media_id: str = "media-1") -> PlannedClip:
    return PlannedClip(
        media_id=media_id,
        source_start=start,
        source_end=end,
        role="body",
        moment_type=MomentType.CHAOS,
    )


def _scene(at: float):
    return SimpleNamespace(start_seconds=at)


class TestDeadOpenings:
    def test_no_clip_opens_behind_the_record_button(self) -> None:
        guarded = guard_clips(
            [_clip(0.0, 40.0)],
            states_by_media={},
            scenes_by_media={},
            recording_start_guard_seconds=4.0,
        )

        assert guarded[0].source_start == 4.0
        assert guarded[0].source_end == 40.0

    def test_an_opening_inside_a_menu_advances_past_it(self) -> None:
        states = [StateSpan(FrameState.MENU, 10.0, 22.0)]

        guarded = guard_clips(
            [_clip(12.0, 60.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            dead_state_pad_seconds=0.5,
        )

        assert guarded[0].source_start == pytest.approx(22.5)

    def test_chained_dead_spans_are_walked_through(self) -> None:
        states = [
            StateSpan(FrameState.MENU, 10.0, 20.0),
            StateSpan(FrameState.LOADING, 20.2, 30.0),
        ]

        guarded = guard_clips(
            [_clip(12.0, 80.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            dead_state_pad_seconds=0.4,
        )

        assert guarded[0].source_start == pytest.approx(30.4)

    def test_unknown_is_not_evidence_of_a_menu(self) -> None:
        states = [StateSpan(FrameState.UNKNOWN, 0.0, 60.0)]

        guarded = guard_clips(
            [_clip(10.0, 60.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
        )

        assert guarded[0].source_start == 10.0

    def test_a_clip_that_is_only_its_dead_opening_is_dropped(self) -> None:
        states = [StateSpan(FrameState.MENU, 0.0, 38.0)]

        guarded = guard_clips(
            [_clip(5.0, 42.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            min_piece_seconds=8.0,
        )

        assert guarded == []

    def test_a_clean_opening_is_untouched(self) -> None:
        guarded = guard_clips(
            [_clip(120.0, 160.0)],
            states_by_media={"media-1": [StateSpan(FrameState.MENU, 10.0, 20.0)]},
            scenes_by_media={},
        )

        assert guarded[0].source_start == 120.0


class TestSlabSplitting:
    def test_a_slab_splits_at_stored_scene_seams(self) -> None:
        scenes = [_scene(at) for at in (100.0, 160.0, 230.0, 290.0)]

        guarded = guard_clips(
            [_clip(60.0, 340.0)],
            states_by_media={},
            scenes_by_media={"media-1": scenes},
            max_clip_seconds=75.0,
            min_piece_seconds=8.0,
        )

        assert len(guarded) > 1
        bounds = [(clip.source_start, clip.source_end) for clip in guarded]
        assert bounds[0][0] == pytest.approx(60.0)
        assert bounds[-1][1] == pytest.approx(340.0)
        for start, end in bounds:
            assert end - start >= 8.0
        for (_, first_end), (second_start, _) in pairwise(bounds):
            assert first_end == pytest.approx(second_start)

    def test_a_clip_inside_the_cap_stays_whole(self) -> None:
        guarded = guard_clips(
            [_clip(60.0, 120.0)],
            states_by_media={},
            scenes_by_media={"media-1": [_scene(90.0)]},
            max_clip_seconds=75.0,
        )

        assert len(guarded) == 1

    def test_no_seams_means_the_slab_ships_whole(self) -> None:
        # An arithmetic midpoint is not a seam; cutting mid-action is worse
        # than the slab.
        guarded = guard_clips(
            [_clip(60.0, 340.0)],
            states_by_media={},
            scenes_by_media={"media-1": []},
            max_clip_seconds=75.0,
        )

        assert len(guarded) == 1

    def test_pieces_inherit_the_clip_identity(self) -> None:
        scenes = [_scene(150.0)]

        guarded = guard_clips(
            [_clip(60.0, 240.0)],
            states_by_media={},
            scenes_by_media={"media-1": scenes},
            max_clip_seconds=100.0,
        )

        assert {clip.media_id for clip in guarded} == {"media-1"}
        assert all(clip.moment_type is MomentType.CHAOS for clip in guarded)
