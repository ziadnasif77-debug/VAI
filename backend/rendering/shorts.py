"""Vertical Shorts cut from the analysis the long video already paid for.

Every competitor in this market is built on one product shape: strong moments
as 9:16 clips. Here that shape costs no second analysis and no second caption
engine, because a Short **is a tiny edit** run through the same stack:

    strongest moments (§35's ranking, already scored and stored)
        -> one NVENC cut per moment, centre-cropped 16:9 -> 9:16
        -> captions from the same transcript, timed to the cut
        -> the same Remotion overlay, at the vertical frame size
        -> one composite per Short

The planning half is pure — moments in, cut plans out — because which moments
become Shorts is a product rule worth testing without FFmpeg in the room. The
rendering half builds argv lists the way the whole rendering layer does:
returned, inspectable, and executed by the caller's runner.

The crop drops the HUD corners, and for a Short that is a feature: a health
bar readable at 1080 wide is noise at 607, and the centre of the frame is
where the game puts the thing that matters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from backend.config.schema import AppConfig, ShortsConfig
from backend.core.duration import format_duration
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import TrackKind
from backend.rendering.encoder import EncoderChoice, EncodeTarget, video_arguments
from backend.timeline.models import Timeline, TimelineClip, Track

logger = get_logger("rendering.shorts", LogChannel.RENDERING)

#: Even-pixel floor for the crop width: yuv420 chroma needs even dimensions,
#: and an odd crop makes FFmpeg refuse the whole graph.
_EVEN: Final[int] = 2


@dataclass(frozen=True, slots=True)
class ShortPlan:
    """One vertical cut: which recording, which seconds, and its rank."""

    index: int
    media_id: str
    moment_id: str | None
    start_seconds: float
    end_seconds: float
    score: float
    moment_type: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)

    def summary(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "media_id": self.media_id,
            "span": f"{format_duration(self.start_seconds)}–{format_duration(self.end_seconds)}",
            "seconds": round(self.duration, 2),
            "score": round(self.score, 3),
            "type": self.moment_type,
        }


def plan_shorts(moments: Sequence[Any], config: ShortsConfig) -> list[ShortPlan]:
    """Choose which moments become Shorts, strongest first.

    The rules, each for a reason:

    * **Strongest first** — §35's ranking already measured entertainment; a
      Short is that ranking's top slice, not a second opinion about it.
    * **The core span, not the context.** Context exists so a long video can
      breathe; a Short has no room to breathe in. A core longer than the cap
      keeps its *beginning* — moments are formed to start where the action
      starts — and one shorter than the floor takes its run-up back from the
      context, never past it.
    * **No overlaps.** Two Shorts sharing footage read as the same upload
      twice; the weaker one is dropped rather than nudged, because a nudged
      moment is no longer the moment the score measured.
    """
    ranked = sorted(moments, key=lambda moment: -float(getattr(moment, "score", 0.0)))
    plans: list[ShortPlan] = []
    taken: dict[str, list[tuple[float, float]]] = {}

    for moment in ranked:
        if len(plans) >= config.count:
            break
        start = float(moment.start_seconds)
        end = float(moment.end_seconds)
        if end - start > config.max_seconds:
            end = start + config.max_seconds
        if end - start < config.min_seconds:
            # Take the run-up from the context, never inventing footage.
            context_start = float(getattr(moment, "context_start", start))
            start = max(context_start, end - config.min_seconds)
        if end - start < config.min_seconds:
            continue  # even with its context this moment is too thin

        media_id = str(moment.media_id)
        spans = taken.setdefault(media_id, [])
        if any(start < b and a < end for a, b in spans):
            continue
        spans.append((start, end))
        plans.append(
            ShortPlan(
                index=len(plans),
                media_id=media_id,
                moment_id=_moment_id(moment),
                start_seconds=start,
                end_seconds=end,
                score=float(getattr(moment, "score", 0.0)),
                moment_type=str(getattr(getattr(moment, "moment_type", ""), "value", "")),
            )
        )
    return plans


def _moment_id(moment: Any) -> str | None:
    metadata = getattr(moment, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("id")
        return str(value) if value else None
    return None


def crop_filter(config: ShortsConfig, *, source_width: int, source_height: int) -> str:
    """The 9:16 window over a 16:9 frame, in FFmpeg's words.

    The window's width comes from the source height and the target ratio, so a
    720p source and a 1080p source both crop correctly. Even-pixel floors on
    every number: yuv420 chroma refuses odd geometry, loudly.
    """
    ratio = config.width / config.height
    crop_w = min(source_width, int(source_height * ratio) // _EVEN * _EVEN)
    crop_h = source_height // _EVEN * _EVEN
    if config.anchor == "left":
        x = 0
    elif config.anchor == "right":
        x = source_width - crop_w
    else:
        x = (source_width - crop_w) // 2 // _EVEN * _EVEN
    return f"crop={crop_w}:{crop_h}:{x}:0,scale={config.width}:{config.height}:flags=lanczos"


def cut_arguments(
    plan: ShortPlan,
    *,
    source: Path,
    destination: Path,
    config: ShortsConfig,
    encoder: EncoderChoice,
    render_config,
    source_width: int,
    source_height: int,
    fps: int,
) -> list[str]:
    """The argv that turns one plan into one vertical cut, audio included.

    ``-ss`` before ``-i`` for the fast seek, then an output ``-t``: on a
    77-minute source the difference is decoding four seconds instead of
    forty minutes to reach the moment.
    """
    target = EncodeTarget(width=config.width, height=config.height, fps=fps)
    return [
        "-ss",
        f"{plan.start_seconds:.3f}",
        "-i",
        str(source),
        "-t",
        f"{plan.duration:.3f}",
        "-vf",
        crop_filter(config, source_width=source_width, source_height=source_height),
        *video_arguments(encoder, target, render_config),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(destination),
    ]


def short_timeline(plan: ShortPlan, project_id: str) -> Timeline:
    """A one-clip timeline for the cut, so the caption engine needs no fork.

    ``build_captions`` maps transcript segments onto timeline clips by their
    source span; handing it this mini-timeline times the Short's own speech
    from zero, through exactly the collision and layout rules the long video
    uses. One caption engine, two frame shapes.
    """
    clip = TimelineClip(
        id=f"short-{plan.index:03d}",
        media_id=plan.media_id,
        track=TrackKind.VIDEO,
        clip_index=0,
        source_in=plan.start_seconds,
        source_out=plan.end_seconds,
        timeline_start=0.0,
        timeline_end=plan.duration,
    )
    return Timeline(
        project_id=project_id,
        tracks=(Track(kind=TrackKind.VIDEO, clips=(clip,)),),
    )


def overlay_merge_arguments(
    cut: Path, overlay: Path, destination: Path, *, overlay_format: str
) -> list[str]:
    """Burn the caption layer into a finished cut, keeping its audio.

    The long-form composite owns mixing and encoding policy; a Short's audio
    is simply the cut's own, so this is the two-input overlay with the alpha
    decoder the overlay format requires and a straight audio copy.
    """
    from backend.rendering.remotion import overlay_input_arguments

    return [
        "-i",
        str(cut),
        *overlay_input_arguments(overlay, overlay_format),
        "-filter_complex",
        "[0:v][1:v]overlay=format=auto:shortest=0[v]",
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "19",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]


def filename_for(plan: ShortPlan, project_name: str) -> str:
    from backend.core.fs import sanitize_filename

    stem = sanitize_filename(project_name) or "short"
    kind = f"-{plan.moment_type}" if plan.moment_type else ""
    return f"{stem}-short-{plan.index + 1:02d}{kind}.mp4"


def describe(config: AppConfig) -> dict[str, Any]:
    shorts = config.shorts
    return {
        "count": shorts.count,
        "frame": f"{shorts.width}x{shorts.height}",
        "band_seconds": [shorts.min_seconds, shorts.max_seconds],
        "captions": shorts.captions,
    }


__all__ = [
    "ShortPlan",
    "crop_filter",
    "cut_arguments",
    "describe",
    "filename_for",
    "overlay_merge_arguments",
    "plan_shorts",
    "short_timeline",
]
