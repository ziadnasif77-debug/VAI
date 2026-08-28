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


class TestRecorderProbe:
    """Frames nobody sampled, read for the recorder's own chrome."""

    class _Ffmpeg:
        def base_arguments(self):
            return ["-y"]

        def run(self, argv, **_kwargs):
            from pathlib import Path

            from PIL import Image

            Image.new("RGB", (8, 8)).save(Path(argv[-1]))

    def test_obs_chrome_becomes_desktop_spans(self, tmp_path) -> None:
        from ai.ocr.fake_provider import FakeOcrProvider
        from backend.analysis.recorder_probe import recorder_spans

        ocr = FakeOcrProvider(default=[("60.00 / 60.00 FPS", 0.9), ("CPU: 3.8%", 0.8)])

        spans = recorder_spans(
            tmp_path / "recording.mkv",
            ffmpeg=self._Ffmpeg(),
            ocr=ocr,
            scratch_dir=tmp_path / "probe",
            offsets=(0.5, 2.0, 4.0),
        )

        assert spans
        assert spans[0].state is FrameState.DESKTOP
        assert spans[0].start_seconds == 0.0
        assert spans[-1].end_seconds >= 4.0

    def test_game_frames_produce_no_spans(self, tmp_path) -> None:
        from ai.ocr.fake_provider import FakeOcrProvider
        from backend.analysis.recorder_probe import recorder_spans

        # One class alone -- a game's own FPS overlay -- must not condemn.
        ocr = FakeOcrProvider(
            default=[("Purchase the Omni Shovel upgrade", 0.9), ("60.00 / 60.00 FPS", 0.9)]
        )

        spans = recorder_spans(
            tmp_path / "recording.mkv",
            ffmpeg=self._Ffmpeg(),
            ocr=ocr,
            scratch_dir=tmp_path / "probe",
            offsets=(0.5, 2.0),
        )

        assert spans == []

    def test_no_ocr_engine_means_no_probe_and_no_crash(self, tmp_path) -> None:
        from backend.analysis.recorder_probe import recorder_spans

        assert (
            recorder_spans(
                tmp_path / "recording.mkv",
                ffmpeg=self._Ffmpeg(),
                ocr=None,
                scratch_dir=tmp_path / "probe",
            )
            == []
        )

    def test_a_desktop_span_blocks_a_clip_opening(self) -> None:
        states = [StateSpan(FrameState.DESKTOP, 0.0, 12.0)]

        guarded = guard_clips(
            [_clip(5.0, 60.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            dead_state_pad_seconds=0.4,
        )

        assert guarded[0].source_start == pytest.approx(12.4)


class TestDeadInteriors:
    """A dead span inside a clip splits it; the opening guard alone let the
    recorder sail through mid-clip on a real rerun."""

    def test_a_mid_clip_dead_span_is_excised(self) -> None:
        states = [StateSpan(FrameState.DESKTOP, 18.5, 24.5)]

        guarded = guard_clips(
            [_clip(12.9, 41.6)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            dead_state_pad_seconds=0.4,
            min_piece_seconds=5.0,
        )

        bounds = [(clip.source_start, clip.source_end) for clip in guarded]
        assert bounds == [
            (12.9, 18.5),
            (pytest.approx(24.9), 41.6),
        ]

    def test_a_short_stub_before_the_dead_span_is_dropped(self) -> None:
        states = [StateSpan(FrameState.DESKTOP, 15.0, 30.0)]

        guarded = guard_clips(
            [_clip(12.9, 60.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            min_piece_seconds=8.0,
        )

        bounds = [(clip.source_start, clip.source_end) for clip in guarded]
        assert bounds == [(pytest.approx(30.4), 60.0)]

    def test_a_clip_entirely_dead_disappears(self) -> None:
        states = [StateSpan(FrameState.DESKTOP, 10.0, 60.0)]

        guarded = guard_clips(
            [_clip(15.0, 40.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
        )

        assert guarded == []
