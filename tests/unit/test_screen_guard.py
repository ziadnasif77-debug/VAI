"""Clip boundaries against what the screen was showing.

Both rules exist because the first fully autonomous video shipped their
absence: a hook opening on the record-button click at second 0.0, and a
398-second body slab with no seam in it.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
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
        states = [StateSpan(FrameState.MENU, 10.0, 22.0, observations=2)]

        guarded = guard_clips(
            [_clip(12.0, 60.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            dead_state_pad_seconds=0.5,
        )

        assert guarded[0].source_start == pytest.approx(22.5)

    def test_chained_dead_spans_are_walked_through(self) -> None:
        states = [
            StateSpan(FrameState.MENU, 10.0, 20.0, observations=2),
            StateSpan(FrameState.LOADING, 20.2, 30.0, observations=2),
        ]

        guarded = guard_clips(
            [_clip(12.0, 80.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            dead_state_pad_seconds=0.4,
        )

        assert guarded[0].source_start == pytest.approx(30.4)

    def test_unknown_is_not_evidence_of_a_menu(self) -> None:
        states = [StateSpan(FrameState.UNKNOWN, 0.0, 60.0, observations=2)]

        guarded = guard_clips(
            [_clip(10.0, 60.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
        )

        assert guarded[0].source_start == 10.0

    def test_a_clip_that_is_only_its_dead_opening_is_dropped(self) -> None:
        states = [StateSpan(FrameState.MENU, 0.0, 38.0, observations=2)]

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
            states_by_media={"media-1": [StateSpan(FrameState.MENU, 10.0, 20.0, observations=2)]},
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
        states = [StateSpan(FrameState.DESKTOP, 0.0, 12.0, observations=2)]

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
        states = [StateSpan(FrameState.DESKTOP, 18.5, 24.5, observations=2)]

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
        states = [StateSpan(FrameState.DESKTOP, 15.0, 30.0, observations=2)]

        guarded = guard_clips(
            [_clip(12.9, 60.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
            min_piece_seconds=8.0,
        )

        bounds = [(clip.source_start, clip.source_end) for clip in guarded]
        assert bounds == [(pytest.approx(30.4), 60.0)]

    def test_a_clip_entirely_dead_disappears(self) -> None:
        states = [StateSpan(FrameState.DESKTOP, 10.0, 60.0, observations=2)]

        guarded = guard_clips(
            [_clip(15.0, 40.0)],
            states_by_media={"media-1": states},
            scenes_by_media={},
        )

        assert guarded == []
class TestSourceDeadSpans:
    """The source's own black/frozen stretches: windowed, cached, incremental."""

    class _Runner:
        """Serves canned detector stderr per window and counts decodes."""

        def __init__(self, stderr_by_start=None):
            self.stderr_by_start = stderr_by_start or {}
            self.calls: list[float] = []

        def base_arguments(self, **_):
            return ["ffmpeg"]

        def input_arguments(self, path, *, start, duration):
            self.calls.append(round(start, 1))
            self._current = start
            return ["-ss", str(start), "-t", str(duration), "-i", str(path)]

        def run(self, argv, check=False, **_):
            from types import SimpleNamespace

            return SimpleNamespace(
                ok=True, stderr=self.stderr_by_start.get(self._current, "")
            )

    def test_runs_are_offset_and_freeze_pairs_are_durations(
        self, tmp_path: Path, config
    ) -> None:
        from backend.analysis.source_dead import dead_source_spans

        source = tmp_path / "rec.mkv"
        source.write_bytes(b"x" * 64)
        runner = self._Runner(
            {
                97.0: (
                    "[blackdetect] black_start:4.0 black_end:10.4 black_duration:6.4\n"
                    "[freezedetect] lavfi.freezedetect.freeze_start: 20.0\n"
                    "[freezedetect] lavfi.freezedetect.freeze_duration: 10.7\n"
                )
            }
        )

        spans = dead_source_spans(
            source,
            ffmpeg=runner,
            config=config,
            cache_dir=tmp_path / "cache",
            media_id="media-1",
            windows=[(100.0, 130.0)],
            duration_seconds=1200.0,
        )

        got = sorted((s.state.value, round(s.start_seconds, 1), round(s.end_seconds, 1)) for s in spans)
        # Window opens at 100-3 (pad); runs offset by that seek, padded 0.3.
        assert got == [
            ("pause", 116.7, 128.0),
            ("transition", 100.7, 107.7),
        ]
        assert all(not s.state.is_gameplay for s in spans)

    def test_a_second_call_decodes_nothing_new(self, tmp_path: Path, config) -> None:
        from backend.analysis.source_dead import dead_source_spans

        source = tmp_path / "rec.mkv"
        source.write_bytes(b"x" * 64)
        runner = self._Runner()
        common = {
            "ffmpeg": runner,
            "config": config,
            "cache_dir": tmp_path / "cache",
            "media_id": "media-2",
            "windows": [(50.0, 80.0)],
            "duration_seconds": 600.0,
        }

        dead_source_spans(source, **common)
        first = list(runner.calls)
        dead_source_spans(source, **common)

        assert first and runner.calls == first, "the cache must answer the rerun"

    def test_a_window_that_will_not_decode_is_an_empty_answer(
        self, tmp_path: Path, config
    ) -> None:
        from backend.analysis.source_dead import dead_source_spans

        source = tmp_path / "rec.mkv"
        source.write_bytes(b"x" * 64)

        class _Broken(self._Runner):
            def run(self, argv, check=False, **_):
                from types import SimpleNamespace

                return SimpleNamespace(ok=False, stderr="")

        assert (
            dead_source_spans(
                source,
                ffmpeg=_Broken(),
                config=config,
                cache_dir=tmp_path / "cache",
                media_id="media-3",
                windows=[(10.0, 40.0)],
                duration_seconds=600.0,
            )
            == []
        )
class TestEvidenceBeforeKnives:
    """The mercy rules, born from a 596 s plan that shipped as 189 s."""

    def _clip(self, start: float, end: float) -> PlannedClip:
        return PlannedClip(
            media_id="media-1", source_start=start, source_end=end,
            moment_type=MomentType.CHAOS, score=0.6,
        )

    def _run(self, clips, states, *, events=None, **kw):
        return guard_clips(
            clips,
            states_by_media={"media-1": states},
            scenes_by_media={"media-1": []},
            recording_start_guard_seconds=0.0,
            events_by_media={"media-1": events} if events else None,
            **kw,
        )

    def test_a_single_observation_span_may_warn_but_not_cut(self) -> None:
        # Nine of these did most of the live shredding: one sampled frame
        # misread as a menu, stretched into a span, carving real gameplay.
        clip = self._clip(100.0, 140.0)
        weak = StateSpan(FrameState.MENU, 115.0, 125.0)  # observations=1

        kept = self._run([clip], [weak])

        assert [(c.source_start, c.source_end) for c in kept] == [(100.0, 140.0)]

    def test_a_corroborated_span_still_cuts(self) -> None:
        clip = self._clip(100.0, 140.0)
        strong = StateSpan(FrameState.MENU, 115.0, 125.0, observations=2)

        kept = self._run([clip], [strong])

        assert len(kept) == 2, "real menus are still excised"

    def test_a_short_interior_stretch_is_bridged(self) -> None:
        # A two-second map glance is life; cutting it costs a hard cut plus
        # a sliver the piece floor then kills.
        clip = self._clip(100.0, 140.0)
        glance = StateSpan(FrameState.MENU, 118.0, 120.5, observations=3)

        kept = self._run([clip], [glance])

        assert [(c.source_start, c.source_end) for c in kept] == [(100.0, 140.0)]

    def test_stillness_overlapping_an_event_yields(self) -> None:
        # A sniper scope holds the frame while the game plainly lives.
        clip = self._clip(100.0, 140.0)
        frozen = StateSpan(FrameState.PAUSE, 110.0, 130.0, observations=3)

        kept = self._run([clip], [frozen], events=[(112.0, 118.0)])

        assert [(c.source_start, c.source_end) for c in kept] == [(100.0, 140.0)]

    def test_a_corroborated_menu_ignores_the_event_veto(self) -> None:
        # The veto is for stillness kinds only: a menu with events behind it
        # is still a menu on screen.
        clip = self._clip(100.0, 140.0)
        menu = StateSpan(FrameState.MENU, 110.0, 130.0, observations=3)

        kept = self._run([clip], [menu], events=[(112.0, 118.0)])

        assert len(kept) == 2

    def test_zero_pieces_rescues_the_widest_live_window(self) -> None:
        # Excision that erases a detected moment silently is worse than a
        # short breath of dead time. The widest live stretch survives,
        # widened to the piece floor.
        clip = self._clip(100.0, 130.0)
        states = [
            StateSpan(FrameState.MENU, 100.0, 112.0, observations=3),
            StateSpan(FrameState.MENU, 117.0, 130.0, observations=3),
        ]

        kept = self._run([clip], states)

        assert len(kept) == 1
        piece = kept[0]
        assert piece.seconds >= 8.0 - 1e-6
        assert piece.source_start >= 100.0 and piece.source_end <= 130.0
        # The opening guard advanced the start past dead #1 (to 112.4)
        # before excision, so the rescued window anchors on the live core
        # that remained and widens rightward with the least dead possible.
        assert 112.0 <= piece.source_start <= 113.0
        assert piece.source_end >= 117.0, "the live core stays inside"

    def test_truly_dead_still_dies(self) -> None:
        clip = self._clip(100.0, 130.0)
        wall = StateSpan(FrameState.MENU, 99.0, 131.0, observations=4)

        assert self._run([clip], [wall]) == []
