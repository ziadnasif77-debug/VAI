"""Rendering only the frames that have something drawn on them (§66, D-008).

The measurement this exists for, from a real ten-minute edit: 18,287 frames
rasterised through Chromium to draw 98 of them, and 599 seconds spent producing
a file that is transparent for 99.5% of its length.

Two properties have to hold for the shortcut to be safe, and they are what the
tests here check:

* **The same overlay ends up on the same frames.** Elements move inside the
  rendered file and move back in the composite; nothing shifts in the finished
  video.
* **A plan that cannot be honoured is not honoured.** Every unclear case ends
  at the whole-composition render this replaced, because a caption silently
  missing from a video is the one failure nobody would think to look for.
"""

from __future__ import annotations

import pytest

from backend.rendering.composite import _placed_segments
from backend.rendering.composition import Composition, OverlaySpan
from backend.rendering.overlay_plan import OverlayPlan, Segment, compact, plan_overlay, whole

pytestmark = pytest.mark.unit

FPS = 30


def _element(start: int, frames: int, identifier: str = "e") -> dict:
    return {"id": identifier, "from": start, "durationInFrames": frames}


def _composition(*elements: dict, total: int = 18000, spans=None) -> Composition:
    ranges = (
        spans
        if spans is not None
        else [(e["from"], e["from"] + e["durationInFrames"]) for e in elements]
    )
    return Composition(
        width=1920,
        height=1080,
        fps=FPS,
        duration_in_frames=total,
        effects=tuple(elements),
        spans=tuple(OverlaySpan(start_frame=a, end_frame=b) for a, b in ranges),
    )


def _plan(composition: Composition, *, gap: int = 60, segments: int = 24, saving: float = 0.15):
    return plan_overlay(
        composition,
        merge_gap_frames=gap,
        max_segments=segments,
        min_saved_ratio=saving,
    )


# -- the measurement that started this ---------------------------------------


def test_a_sparse_overlay_costs_what_it_draws() -> None:
    # The real composition, to the frame: three graphics in a ten-minute edit.
    composition = _composition(
        _element(3362, 32, "text_pop"),
        _element(4302, 9, "impact-a"),
        _element(8125, 9, "impact-b"),
        total=18287,
        spans=[(3354, 3402), (4294, 4319), (8117, 8142)],
    )
    plan = _plan(composition)

    assert len(plan.segments) == 3
    assert plan.render_frames == 98
    assert plan.saved_frames == 18189
    assert compact(composition, plan).duration_in_frames == 98


def test_an_overlay_that_covers_the_video_is_rendered_whole() -> None:
    # Captions on nearly every frame: splitting buys a filter graph and saves
    # nothing.
    composition = _composition(_element(0, 17800), total=18000)
    plan = _plan(composition)

    assert plan.is_whole
    assert "cost more than it saves" in plan.reason


# -- what the plan is allowed to do ------------------------------------------


def test_short_gaps_are_paid_for_rather_than_split_at() -> None:
    composition = _composition(
        _element(100, 30),
        _element(160, 30),  # a 30-frame hole: shorter than the merge gap
        _element(9000, 30),
        total=18000,
    )
    plan = _plan(composition, gap=60)

    assert len(plan.segments) == 2
    assert plan.segments[0].source_start == 100
    assert plan.segments[0].source_end == 190


def test_the_segment_count_is_capped_by_buying_back_the_narrowest_gaps() -> None:
    # Twelve elements spread out, but only three branches allowed.
    composition = _composition(
        *(_element(index * 600, 30, f"e{index}") for index in range(12)), total=18000
    )
    plan = _plan(composition, gap=0, segments=3)

    assert len(plan.segments) == 3
    # Still cheaper than everything, and still in order.
    assert plan.render_frames < composition.duration_in_frames
    assert [segment.source_start for segment in plan.segments] == sorted(
        segment.source_start for segment in plan.segments
    )


def test_zero_segments_switches_the_whole_thing_off() -> None:
    composition = _composition(_element(100, 30), total=18000)
    plan = _plan(composition, segments=0)

    assert plan.is_whole
    assert "switched off" in plan.reason


@pytest.mark.parametrize(
    ("composition", "fragment"),
    [
        (Composition(width=1920, height=1080, fps=FPS, duration_in_frames=0), "no frames"),
        (
            Composition(width=1920, height=1080, fps=FPS, duration_in_frames=900),
            "no drawn spans",
        ),
    ],
)
def test_anything_unclear_renders_everything(composition: Composition, fragment: str) -> None:
    plan = _plan(composition)
    assert plan.is_whole
    assert fragment in plan.reason


# -- moving the elements, and moving them back -------------------------------


def test_compaction_moves_every_element_into_its_segment() -> None:
    composition = _composition(
        _element(3362, 32, "a"),
        _element(8125, 9, "b"),
        total=18000,
        spans=[(3354, 3402), (8117, 8142)],
    )
    plan = _plan(composition)
    rendered = compact(composition, plan)

    moved = {element["id"]: element["from"] for element in rendered.effects}
    # Each element keeps its offset inside its own stretch, which is what makes
    # the shift invisible: 3362 - 3354 == 8, 8125 - 8117 == 8.
    assert moved == {"a": 8, "b": 56}
    assert rendered.metadata["fullDurationInFrames"] == 18000
    assert len(rendered.metadata["renderedSegments"]) == 2
    # Durations are untouched. Only the start moves.
    assert [element["durationInFrames"] for element in rendered.effects] == [32, 9]


def test_an_element_no_segment_covers_cancels_the_plan() -> None:
    # The spans and the elements disagree, which should never happen -- and if
    # it does, a video missing a caption is worse than a slow render.
    composition = _composition(_element(3362, 32), total=18000, spans=[(9000, 9100)])
    plan = _plan(composition)
    rendered = compact(composition, plan)

    assert rendered is composition


def test_compacting_a_whole_plan_changes_nothing() -> None:
    composition = _composition(_element(100, 30), total=18000)
    assert compact(composition, whole(18000, fps=FPS, reason="test")) is composition


# -- and the filter graph that puts them back --------------------------------


def _two_segments() -> OverlayPlan:
    return OverlayPlan(
        segments=(
            Segment(source_start=60, source_end=90, render_start=0),
            Segment(source_start=210, source_end=240, render_start=30),
        ),
        total_frames=300,
        fps=FPS,
    )


def test_each_segment_is_trimmed_and_shifted_to_where_it_belongs() -> None:
    filters, label = _placed_segments(_two_segments())
    graph = ";".join(filters)

    assert "[1:v]split=2[seg0][seg1]" in graph
    # 60 frames at 30 fps is 2 seconds, 210 is 7 -- and the trim reads the
    # rendered file's own frame numbers, which are the compacted ones.
    assert "trim=start_frame=0:end_frame=30,setpts=PTS-STARTPTS+2.000000/TB" in graph
    assert "trim=start_frame=30:end_frame=60,setpts=PTS-STARTPTS+7.000000/TB" in graph
    assert label == "over1"


def test_the_gaps_stay_gaps() -> None:
    # Without both of these the default overlay behaviour freezes the last
    # overlay frame across the footage that follows it, and ends the graph when
    # the overlay runs out.
    graph = ";".join(_placed_segments(_two_segments())[0])
    assert graph.count("eof_action=pass") == 2
    assert graph.count("repeatlast=0") == 2


def test_a_single_segment_needs_no_split() -> None:
    plan = OverlayPlan(
        segments=(Segment(source_start=60, source_end=90, render_start=0),),
        total_frames=300,
        fps=FPS,
    )
    graph = ";".join(_placed_segments(plan)[0])
    assert "split" not in graph
    assert "[1:v]trim=start_frame=0:end_frame=30" in graph


def test_a_plan_without_its_own_frame_rate_is_refused() -> None:
    # The overlay runs at 30 while the video runs at 60; guessing which clock a
    # frame number belongs to would put every caption in the wrong place.
    plan = OverlayPlan(
        segments=(Segment(source_start=60, source_end=90, render_start=0),),
        total_frames=300,
    )
    with pytest.raises(ValueError, match="fps"):
        _placed_segments(plan)
