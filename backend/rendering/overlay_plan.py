"""Which frames Chromium is actually asked to draw (SPEC section 66, D-008).

Remotion rasterises every frame of the composition it is handed, whether that
frame has a caption on it or nothing at all. Measured on a real ten-minute
edit: 18,287 frames rendered to draw 98 of them -- three graphics totalling
3.3 seconds -- and 599 seconds of Chromium to produce a file that is
transparent for 99.5% of its length.

The composition already knows where its elements are; :class:`OverlaySpan`
has recorded the drawn stretches since Phase 9 and nothing read them. This
module turns them into a render plan:

    spans -> merge the cheap gaps -> a bounded number of segments
          -> a shorter composition -> one Chromium pass -> offsets for FFmpeg

Two costs pull against each other, which is why this is a plan rather than a
rule. Every frame between segments is a frame Chromium does not render, so
more segments is cheaper. Every segment is also a branch in the composite's
filter graph, and a graph with two hundred branches is slower to run and
harder to trust than the frames it saves. So gaps shorter than a threshold are
paid for rather than split at, and when the segment count is still too high the
narrowest remaining gaps are bought back until it fits.

The plan is *lossless with respect to the picture*: the same overlay ends up on
the same frames. What changes is how much transparency was encoded to get
there.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from backend.core.logging import LogChannel, get_logger
from backend.rendering.composition import Composition, OverlaySpan

logger = get_logger("rendering.overlay_plan", LogChannel.RENDERING)


@dataclass(frozen=True, slots=True)
class Segment:
    """One stretch that is rendered, and where it belongs in the video."""

    #: Frame range in the finished video. Half-open, like every other range in
    #: this codebase.
    source_start: int
    source_end: int
    #: Where the same stretch sits inside the rendered overlay file, which is
    #: the segments back to back with the gaps removed.
    render_start: int

    @property
    def frames(self) -> int:
        return self.source_end - self.source_start

    @property
    def render_end(self) -> int:
        return self.render_start + self.frames

    @property
    def shift(self) -> int:
        """Frames to add to a rendered position to get the real one."""
        return self.source_start - self.render_start

    def contains(self, frame: int) -> bool:
        return self.source_start <= frame < self.source_end

    def summary(self) -> dict[str, int]:
        return {
            "source_start": self.source_start,
            "source_end": self.source_end,
            "render_start": self.render_start,
        }


@dataclass(frozen=True, slots=True)
class OverlayPlan:
    """What to render, and where each piece of it goes back."""

    segments: tuple[Segment, ...]
    #: Length of the finished video, so the saving can be stated honestly.
    total_frames: int
    #: The overlay's own frame rate, which is not always the video's: captions
    #: at 30 composite invisibly over 60 fps gameplay, and the layer is
    #: rendered at half the frames on purpose. Every frame number here counts
    #: at this rate, so the plan carries it rather than letting the composite
    #: guess which of the two clocks it is holding.
    fps: int = 0
    #: Why the plan is what it is, for the render notes (SPEC section 80).
    reason: str = ""

    @property
    def render_frames(self) -> int:
        return sum(segment.frames for segment in self.segments)

    @property
    def is_whole(self) -> bool:
        """Whether this is the old behaviour: one pass over everything.

        The composite has a shorter, better-tested path for this case, and a
        plan that saves nothing should take it.
        """
        return (
            len(self.segments) == 1
            and self.segments[0].source_start == 0
            and self.segments[0].source_end >= self.total_frames
        )

    @property
    def saved_frames(self) -> int:
        return max(0, self.total_frames - self.render_frames)

    def summary(self) -> dict[str, Any]:
        return {
            "segments": len(self.segments),
            "render_frames": self.render_frames,
            "total_frames": self.total_frames,
            "saved_frames": self.saved_frames,
            "saved_ratio": (
                round(self.saved_frames / self.total_frames, 4) if self.total_frames else 0.0
            ),
            "fps": self.fps,
            "reason": self.reason,
        }


def whole(total_frames: int, *, fps: int = 0, reason: str) -> OverlayPlan:
    """The plan that renders everything -- what this module falls back to."""
    return OverlayPlan(
        segments=(Segment(source_start=0, source_end=total_frames, render_start=0),),
        total_frames=total_frames,
        fps=fps,
        reason=reason,
    )


def plan_overlay(
    composition: Composition,
    *,
    merge_gap_frames: int,
    max_segments: int,
    min_saved_ratio: float,
) -> OverlayPlan:
    """Decide which stretches of ``composition`` are worth rendering.

    Args:
        composition: the full description, with its spans already computed.
        merge_gap_frames: gaps this short are rendered rather than split at.
            Splitting has a fixed cost per segment and a gap of a few frames
            is not worth one.
        max_segments: ceiling on the composite's filter branches. The
            narrowest gaps are bought back until the count fits.
        min_saved_ratio: below this much saving, render everything. A plan
            that trims 4% of the frames has bought a more complex filter graph
            for nothing.

    Returns:
        A plan. It is always valid: when anything is unclear the whole
        composition is rendered, which is what this code did before the plan
        existed.
    """
    total = composition.duration_in_frames
    fps = composition.fps
    if total <= 0:
        return whole(max(0, total), fps=fps, reason="the composition has no frames")
    if not composition.spans:
        # Nothing recorded where the elements are. Rendering everything is
        # wasteful but correct, and correct is the requirement.
        return whole(total, fps=fps, reason="the composition recorded no drawn spans")
    if max_segments < 1:
        return whole(total, fps=fps, reason="segmented overlay rendering is switched off")

    merged = _merge(composition.spans, gap=max(0, merge_gap_frames), limit=total)
    merged = _fit(merged, max_segments=max_segments)

    rendered = sum(span.frames for span in merged)
    saved = (total - rendered) / total
    if saved < min_saved_ratio:
        return whole(
            total,
            fps=fps,
            reason=(
                f"the overlay covers {100 * (1 - saved):.0f}% of the video, "
                "so splitting it would cost more than it saves"
            ),
        )

    segments: list[Segment] = []
    cursor = 0
    for span in merged:
        segments.append(
            Segment(
                source_start=span.start_frame,
                source_end=span.end_frame,
                render_start=cursor,
            )
        )
        cursor += span.frames

    return OverlayPlan(
        segments=tuple(segments),
        total_frames=total,
        fps=fps,
        reason=f"{len(segments)} stretch(es) carry every overlay element",
    )


def compact(composition: Composition, plan: OverlayPlan) -> Composition:
    """Rewrite ``composition`` so its elements sit inside the planned segments.

    Remotion needs no change to read this. Every element is drawn inside a
    ``<Sequence from=...>`` and reads a sequence-relative clock, so moving an
    element earlier moves the whole element -- its animation included -- and
    nothing inside it can tell the difference.

    An element that no segment covers would be silently dropped, which is the
    one outcome worth failing over: the spans are computed *from* the elements,
    so an unmapped element means the plan and the composition disagree. That
    returns the composition untouched and says so, rather than shipping a video
    missing a caption nobody will think to look for.
    """
    if plan.is_whole:
        return composition

    captions, ok = _remapped(composition.captions, plan)
    effects, effects_ok = _remapped(composition.effects, plan)
    if not (ok and effects_ok):
        logger.warning(
            "An overlay element fell outside every planned segment; rendering the whole layer",
            extra=plan.summary(),
        )
        return composition

    return dataclasses.replace(
        composition,
        duration_in_frames=max(1, plan.render_frames),
        captions=captions,
        effects=effects,
        spans=tuple(
            OverlaySpan(start_frame=segment.render_start, end_frame=segment.render_end)
            for segment in plan.segments
        ),
        metadata={
            **composition.metadata,
            # The full length is what the composite puts the overlay back
            # into, and a description that has forgotten it is not readable on
            # its own (SPEC section 81).
            "renderedSegments": [segment.summary() for segment in plan.segments],
            "fullDurationInFrames": plan.total_frames,
        },
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _merge(spans: tuple[OverlaySpan, ...], *, gap: int, limit: int) -> list[OverlaySpan]:
    """Order the spans and close every gap no wider than ``gap``."""
    ordered = sorted(spans, key=lambda span: (span.start_frame, span.end_frame))
    merged: list[list[int]] = []
    for span in ordered:
        start = max(0, span.start_frame)
        end = min(limit, span.end_frame)
        if end <= start:
            continue
        if merged and start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [OverlaySpan(start_frame=start, end_frame=end) for start, end in merged]


def _fit(spans: list[OverlaySpan], *, max_segments: int) -> list[OverlaySpan]:
    """Buy back the narrowest gaps until the segment count fits the ceiling.

    Narrowest first because a gap is bought at the price of rendering it: the
    cheapest way to lose a branch is to pay for the fewest frames.
    """
    while len(spans) > max_segments:
        gaps = [
            (spans[index + 1].start_frame - spans[index].end_frame, index)
            for index in range(len(spans) - 1)
        ]
        _, index = min(gaps)
        spans[index : index + 2] = [
            OverlaySpan(
                start_frame=spans[index].start_frame,
                end_frame=spans[index + 1].end_frame,
            )
        ]
    return spans


def _remapped(
    elements: tuple[dict[str, Any], ...], plan: OverlayPlan
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """Move each element into its segment's place in the rendered file."""
    moved: list[dict[str, Any]] = []
    for element in elements:
        start = int(element["from"])
        segment = next((one for one in plan.segments if one.contains(start)), None)
        if segment is None:
            return (), False
        moved.append({**element, "from": start - segment.shift})
    return tuple(moved), True


__all__ = ["OverlayPlan", "Segment", "compact", "plan_overlay", "whole"]
