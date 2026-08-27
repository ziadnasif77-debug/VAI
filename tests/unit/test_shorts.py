"""Shorts planning and command construction (§35 delivered at 9:16).

The planning is the product rule — which moments become Shorts — and it is
tested without FFmpeg in the room. The command builders are tested as strings
because their geometry arithmetic (even-pixel crops, anchors, the seek shape)
is exactly where a silent mistake becomes a refused filter graph or a
half-frame shift nobody can debug from a log.
"""

from __future__ import annotations

import pytest

from backend.config.schema import ShortsConfig
from backend.core.models.enums import GameEventType, MomentType
from backend.gaming.correlation import GameEvent
from backend.moments.formation import Moment
from backend.rendering.shorts import (
    crop_filter,
    filename_for,
    plan_shorts,
    short_timeline,
)

pytestmark = pytest.mark.unit


def _moment(
    start: float,
    *,
    seconds: float = 30.0,
    score: float = 0.6,
    context_lead: float = 10.0,
    media_id: str = "media-000",
    kind: MomentType = MomentType.SKILL,
) -> Moment:
    event = GameEvent(
        event_type=GameEventType.KILL,
        start_seconds=start,
        end_seconds=start + 2.0,
        confidence=0.8,
        importance=0.7,
        sources=("ocr",),
    )
    return Moment(
        media_id=media_id,
        moment_type=kind,
        start_seconds=start,
        end_seconds=start + seconds,
        context_start=max(0.0, start - context_lead),
        context_end=start + seconds + 5.0,
        score=score,
        score_breakdown={"entertainment": score},
        events=(event,),
    )


def _config(**overrides) -> ShortsConfig:
    return ShortsConfig(**overrides)


class TestPlanning:
    def test_the_strongest_moments_win_in_rank_order(self) -> None:
        plans = plan_shorts(
            [
                _moment(0.0, score=0.4),
                _moment(200.0, score=0.9),
                _moment(400.0, score=0.7),
                _moment(600.0, score=0.6),
            ],
            _config(count=2),
        )

        assert [plan.start_seconds for plan in plans] == [200.0, 400.0]
        assert [plan.index for plan in plans] == [0, 1]

    def test_a_long_moment_keeps_its_beginning(self) -> None:
        # Moments are formed to start where the action starts; the cap trims
        # the tail, never the opening.
        plans = plan_shorts([_moment(100.0, seconds=90.0)], _config(max_seconds=60))

        assert plans[0].start_seconds == 100.0
        assert plans[0].duration == pytest.approx(60.0)

    def test_a_thin_moment_takes_its_run_up_from_the_context(self) -> None:
        plans = plan_shorts(
            [_moment(100.0, seconds=8.0, context_lead=20.0)], _config(min_seconds=15)
        )

        assert plans[0].duration == pytest.approx(15.0)
        # Extended backwards into context, never past it.
        assert plans[0].start_seconds == pytest.approx(93.0)

    def test_a_moment_too_thin_even_with_context_is_skipped(self) -> None:
        plans = plan_shorts([_moment(2.0, seconds=4.0, context_lead=2.0)], _config(min_seconds=15))

        assert plans == []

    def test_overlapping_footage_ships_once(self) -> None:
        # The weaker of two overlapping moments is dropped, not nudged: a
        # nudged moment is no longer the moment the score measured.
        plans = plan_shorts(
            [
                _moment(100.0, score=0.9),
                _moment(110.0, score=0.8),
                _moment(300.0, score=0.5),
            ],
            _config(count=3),
        )

        assert [plan.start_seconds for plan in plans] == [100.0, 300.0]

    def test_recordings_do_not_collide(self) -> None:
        # The same seconds on two different recordings are different footage.
        plans = plan_shorts(
            [_moment(100.0, media_id="media-a"), _moment(100.0, media_id="media-b")],
            _config(count=2),
        )

        assert len(plans) == 2


class TestGeometry:
    def test_the_crop_is_a_9_16_window_centred(self) -> None:
        text = crop_filter(_config(), source_width=1920, source_height=1080)

        # 1080 * 9/16 = 607.5 -> 606 even; centred x = (1920-606)//2 = 657 -> 656
        assert text.startswith("crop=606:1080:656:0,")
        assert "scale=1080:1920" in text

    @pytest.mark.parametrize(("anchor", "x"), [("left", 0), ("center", 656), ("right", 1314)])
    def test_every_anchor_lands_where_it_says(self, anchor: str, x: int) -> None:
        text = crop_filter(_config(anchor=anchor), source_width=1920, source_height=1080)
        assert f":{x}:0," in text

    def test_a_720p_source_crops_by_its_own_height(self) -> None:
        text = crop_filter(_config(), source_width=1280, source_height=720)
        assert text.startswith("crop=404:720:")

    def test_every_dimension_is_even(self) -> None:
        # yuv420 chroma refuses odd geometry, loudly, and at render time.
        for width, height in ((1919, 1079), (1280, 719), (854, 480)):
            text = crop_filter(_config(), source_width=width, source_height=height)
            numbers = [int(n) for n in text.split("crop=")[1].split(",")[0].split(":")]
            assert all(n % 2 == 0 for n in numbers[:2]), (width, height, text)


class TestTheMiniTimeline:
    def test_the_cut_becomes_a_one_clip_timeline_from_zero(self) -> None:
        plan = plan_shorts([_moment(100.0, seconds=30.0)], _config())[0]
        timeline = short_timeline(plan, "proj-x")

        clip = timeline.video_clips()[0]
        assert clip.source_in == pytest.approx(100.0)
        assert clip.source_out == pytest.approx(130.0)
        # Timeline time starts at zero: the caption engine times the Short's
        # own speech from its own first frame.
        assert clip.timeline_start == 0.0
        assert clip.timeline_end == pytest.approx(30.0)

    def test_filenames_are_windows_safe_and_ranked(self) -> None:
        plan = plan_shorts([_moment(100.0, kind=MomentType.FUNNY)], _config())[0]
        name = filename_for(plan, "Night in Grounded: part 2?")

        assert name.endswith("-short-01-funny.mp4")
        assert ":" not in name
        assert "?" not in name
