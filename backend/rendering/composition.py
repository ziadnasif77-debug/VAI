"""The composition description Remotion receives (SPEC §64, §66).

§64 is the boundary this module enforces: **Remotion must not decide which
clips are good. AI decides; Remotion executes visual composition.** So what
crosses into the renderer is a finished description — every element with its
position, its duration and its parameters already settled — and nothing that
would let it choose.

Two consequences shape the format.

**Frames, not seconds.** Remotion counts in frames, and a caption at 1.234 s of
a 60 fps video is frame 74.04. Rounding it in TypeScript would put the decision
somewhere untestable and let two elements disagree about which frame they share.
It happens here, once, where a test can check it.

**Only this renderer's work.** §68 makes the engine a property of the effect,
so an effect that alters the gameplay footage is FFmpeg's and never appears
here. What crosses is captions and the overlay half of the effects library --
things drawn *on top of* a transparent canvas (D-008).

The description is deterministic: the same timeline and the same effects
produce byte-identical JSON, which is what lets §48 cache a render and §127
re-edit without re-rendering what did not change.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.config.schema import CaptionsConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import EffectEngine
from backend.effects.models import EffectInstance
from backend.timeline import retime
from backend.timeline.captions import Caption, wrap
from backend.timeline.models import Timeline

logger = get_logger("rendering.composition", LogChannel.RENDERING)

#: Format version of the description. Bumped when the shape changes, so a
#: Remotion project and a backend that disagree fail loudly rather than
#: rendering something subtly wrong.
COMPOSITION_VERSION: Final[int] = 1

#: 16:9 heights to widths. The timeline carries no resolution -- it describes
#: an edit, not a file -- so the render config decides, and this maps it.
_ASPECT: Final[dict[str, float]] = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0}


@dataclass(frozen=True, slots=True)
class OverlaySpan:
    """A stretch of the timeline where something is actually drawn.

    The reason to compute these: a twenty-minute video with four minutes of
    captions should cost four minutes of Chromium, not twenty. Phase 10 renders
    these spans and composites them; everything between them is untouched
    gameplay.
    """

    start_frame: int
    end_frame: int

    @property
    def frames(self) -> int:
        return self.end_frame - self.start_frame

    def overlaps(self, other: OverlaySpan, *, gap: int = 0) -> bool:
        return (
            self.start_frame - gap <= other.end_frame
            and other.start_frame - gap <= self.end_frame
        )


@dataclass(frozen=True, slots=True)
class Composition:
    """Everything Remotion needs, and nothing that would let it decide."""

    width: int
    height: int
    fps: int
    duration_in_frames: int
    captions: tuple[dict[str, Any], ...] = ()
    effects: tuple[dict[str, Any], ...] = ()
    style: dict[str, Any] = field(default_factory=dict)
    spans: tuple[OverlaySpan, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Whether there is anything to draw at all.

        The basis for ``remotion.skip_when_no_overlays``: a caption-free video
        with no overlay effects never pays the Chromium cost (D-008).
        """
        return not self.captions and not self.effects

    @property
    def drawn_frames(self) -> int:
        return sum(span.frames for span in self.spans)

    def as_dict(self) -> dict[str, Any]:
        """The JSON Remotion reads. Key order is fixed so the file is stable."""
        return {
            "version": COMPOSITION_VERSION,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "durationInFrames": self.duration_in_frames,
            "style": self.style,
            "captions": list(self.captions),
            "effects": list(self.effects),
            "spans": [
                {"startFrame": span.start_frame, "endFrame": span.end_frame}
                for span in self.spans
            ],
            "metadata": self.metadata,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "frames": self.duration_in_frames,
            "captions": len(self.captions),
            "effects": len(self.effects),
            "spans": len(self.spans),
            "drawn_frames": self.drawn_frames,
        }


def build_composition(
    timeline: Timeline,
    *,
    captions: Sequence[Caption] = (),
    effects: Sequence[EffectInstance] = (),
    caption_config: CaptionsConfig,
    width: int,
    height: int,
    fps: int,
    span_padding_seconds: float = 0.25,
) -> Composition:
    """Describe the overlay layer for one edit (§64, §66).

    Args:
        timeline: the finished edit. Only its duration and clip positions are
            read -- the overlay never touches the gameplay footage (D-008).
        captions: §71 captions, already timed against the transcript.
        effects: the whole effect plan. Only the Remotion-engine half is used.
        caption_config: §71 appearance and layout.
        width, height, fps: the output format, from the render config.
        span_padding_seconds: how much to extend each drawn span at either end,
            so a fade-in is not clipped by the span that contains it.

    Returns:
        A :class:`Composition`. ``is_empty`` is the signal to skip the pass.
    """
    if fps <= 0:
        raise ValueError("fps must be positive to convert seconds into frames")

    duration = timeline.duration
    total_frames = max(1, _to_frames(duration, fps))

    described_captions = tuple(
        _describe_caption(caption, index, fps, caption_config, width, height)
        for index, caption in enumerate(captions)
    )
    overlay_effects = tuple(
        _describe_effect(effect, index, fps, timeline)
        for index, effect in enumerate(effects)
        if effect.engine is EffectEngine.REMOTION
    )

    spans = _spans(
        (*described_captions, *overlay_effects),
        fps=fps,
        padding=_to_frames(span_padding_seconds, fps),
        limit=total_frames,
    )

    composition = Composition(
        width=width,
        height=height,
        fps=fps,
        duration_in_frames=total_frames,
        captions=described_captions,
        effects=overlay_effects,
        style=_style(caption_config, width, height),
        spans=spans,
        metadata={
            "projectId": timeline.project_id,
            "durationSeconds": round(duration, 3),
            # Recorded so a rendered overlay can be matched to the edit it was
            # made for, rather than assumed to still fit (§48).
            "clips": len(timeline.video_clips()),
        },
    )
    logger.info("Described the overlay composition", extra=composition.summary())
    return composition


def resolution_for(height: int, aspect_ratio: str) -> tuple[int, int]:
    """Width and height for a target height, rounded to even numbers.

    Even because every codec worth using wants chroma subsampling to divide,
    and an odd dimension is the kind of thing that fails at the encoder rather
    than here.
    """
    ratio = _ASPECT.get(aspect_ratio)
    if ratio is None:
        raise ValueError(f"Unsupported aspect ratio {aspect_ratio!r}")
    width = round(height * ratio)
    return width + (width % 2), height + (height % 2)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _to_frames(seconds: float, fps: int) -> int:
    """Seconds to a frame index, rounding to nearest.

    Nearest rather than floor: an element that should start at 1.999 s of a
    30 fps video belongs on frame 60, and flooring it to 59 shows it a frame
    early every time the arithmetic lands just under.
    """
    return round(max(0.0, seconds) * fps)


def _describe_caption(
    caption: Caption,
    index: int,
    fps: int,
    config: CaptionsConfig,
    width: int,
    height: int,
) -> dict[str, Any]:
    """One caption, in frames, with its words and its lines (§71)."""
    start = _to_frames(caption.timeline_start, fps)
    end = max(start + 1, _to_frames(caption.timeline_end, fps))
    language = caption.language or config.languages.default
    return {
        "id": caption.id,
        "index": index,
        "from": start,
        "durationInFrames": end - start,
        "text": caption.text,
        # Wrapped here rather than in the browser: the line budget is a config
        # value, and a renderer measuring its own text would silently disagree
        # with the sidecar files carrying the same captions.
        "lines": wrap(caption.text, config),
        "words": [
            {
                "word": word,
                "from": _to_frames(word_start, fps),
                "to": max(_to_frames(word_start, fps) + 1, _to_frames(word_end, fps)),
            }
            for word, word_start, word_end in caption.words
        ],
        "language": language,
        "rtl": config.is_rtl(language) if language else False,
        "clipId": caption.clip_id,
    }


def _describe_effect(
    effect: EffectInstance, index: int, fps: int, timeline: Timeline
) -> dict[str, Any]:
    """One overlay effect, positioned on the programme timeline.

    Effects are stored relative to their clip, so the clip's position is added
    back here: the overlay is drawn on the finished video, and the finished
    video is where the clip now sits. The addition goes through the clip's
    warp mapping -- a graphic anchored after a freeze point belongs after the
    hold, where the frame it decorates is actually on screen.
    """
    start_seconds = effect.start_seconds
    if effect.clip_id:
        clip = timeline.clip(effect.clip_id)
        if clip is not None:
            start_seconds = clip.timeline_start + retime.output_offset(
                clip, effect.start_seconds
            )
    start = _to_frames(start_seconds, fps)
    duration = max(1, _to_frames(effect.duration_seconds, fps))
    return {
        "id": f"{effect.effect.value}-{index:03d}",
        "type": effect.effect.value,
        "category": effect.category.value,
        "from": start,
        "durationInFrames": duration,
        "strength": round(effect.strength, 4),
        "params": dict(effect.params),
        "clipId": effect.clip_id,
        # §80: the reason travels with the effect, so a mystery zoom in the
        # finished video can be traced to the moment that earned it.
        "reason": effect.reason,
    }


def _spans(
    elements: Iterable[dict[str, Any]], *, fps: int, padding: int, limit: int
) -> tuple[OverlaySpan, ...]:
    """Merge every element's frame range into the stretches actually drawn."""
    ranges: list[tuple[int, int]] = []
    for element in elements:
        start = int(element["from"])
        end = start + int(element["durationInFrames"])
        ranges.append((max(0, start - padding), min(limit, end + padding)))
    if not ranges:
        return ()

    ranges.sort()
    merged: list[list[int]] = [list(ranges[0])]
    for start, end in ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple(OverlaySpan(start_frame=start, end_frame=end) for start, end in merged)


def _style(config: CaptionsConfig, width: int, height: int) -> dict[str, Any]:
    """§71's appearance, resolved into pixels the renderer can use directly.

    ``font_size_ratio`` is a fraction of frame height so the same configuration
    reads the same at 720p and 4K; turning it into pixels here means the
    browser never has to know the rule.
    """
    appearance = config.appearance
    layout = config.layout
    return {
        "fontFamily": appearance.font_family,
        "fontWeight": appearance.font_weight,
        "fontSizePx": max(8, round(height * appearance.font_size_ratio)),
        "primaryColor": appearance.primary_color,
        "highlightColor": appearance.highlight_color,
        "outlineColor": appearance.outline_color,
        "outlineWidthPx": appearance.outline_width,
        "shadowOpacity": appearance.shadow_opacity,
        "position": layout.position,
        "maxLines": layout.max_lines,
        "safeAreaBottomPx": round(height * layout.safe_area_bottom),
        "safeAreaHorizontalPx": round(width * layout.safe_area_horizontal),
        "wordHighlighting": config.word_highlighting,
        "animated": config.style == "animated",
        "emphasis": config.emphasis,
    }


def frames_to_seconds(frames: int, fps: int) -> float:
    """Inverse of :func:`_to_frames`, for reporting and for tests."""
    return frames / float(fps) if fps else 0.0


def seconds_to_frames(seconds: float, fps: int) -> int:
    """Public form of the rounding rule, so callers cannot invent their own."""
    return _to_frames(seconds, fps)


def ceil_frames(seconds: float, fps: int) -> int:
    """Frames needed to *contain* a duration, rather than to land on it."""
    return math.ceil(max(0.0, seconds) * fps)


__all__ = [
    "COMPOSITION_VERSION",
    "Composition",
    "OverlaySpan",
    "build_composition",
    "ceil_frames",
    "frames_to_seconds",
    "resolution_for",
    "seconds_to_frames",
]
