"""Cutting and concatenating the programme (SPEC §65, §67, §7, §47).

The EDL says which seconds of which recordings the video is made of. This turns
that into one continuous file.

Clip by clip, not in one enormous filter graph. A single `filter_complex`
trimming seventy clips is a legitimate way to do this and a bad way to *ship*
it: no progress that means anything, no way to resume after a crash three
quarters of the way through a twenty-minute encode, and a failure that reports
one unreadable graph rather than the clip that broke. Segments give §47 its
resume, §82 a cancellation point every few seconds, and a stack trace that
names a clip.

The cost is a second encode. Segments are therefore written at visually
lossless quality (`intermediate_arguments`), because every artefact introduced
here survives into the finished video, and disk is the cheapest thing in this
pipeline.

Nothing is ever held in memory. FFmpeg reads and writes files; §7's rule is
kept by not having a code path that could break it.
"""

from __future__ import annotations

import math
import shutil
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, MediaError, RenderError
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.core.models.enums import EffectType, TransitionType
from backend.effects.models import EffectInstance
from backend.media.ffmpeg import CancelledError, FFmpegRunner, format_seconds, progress_arguments
from backend.media.probe import probe_media
from backend.rendering.encoder import EncoderChoice, EncodeTarget, intermediate_arguments
from backend.timeline.models import TimelineClip

logger = get_logger("rendering.renderer", LogChannel.RENDERING)

RenderProgress = Callable[[float, str], None]
CancelCheck = Callable[[], bool]

#: Where the per-clip intermediates live, under the project's render directory.
SEGMENT_DIRNAME: Final[str] = "segments"

#: The concat demuxer's list file.
SEGMENT_LIST_FILENAME: Final[str] = "segments.txt"

#: How far a segment's measured duration may sit from its requested one before
#: it is treated as unfinished. A frame at 30 fps is 33 ms; this allows two.
SEGMENT_TOLERANCE_SECONDS: Final[float] = 0.08


@dataclass(frozen=True, slots=True)
class RenderedProgramme:
    """The concatenated video, and what went into it."""

    path: Path
    clips: int
    duration_seconds: float
    reused_segments: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "clips": self.clips,
            "duration_seconds": round(self.duration_seconds, 3),
            "reused_segments": self.reused_segments,
            "notes": list(self.notes),
        }


def render_programme(
    clips: Sequence[TimelineClip],
    sources: dict[str, Path],
    *,
    destination: Path,
    work_dir: Path,
    runner: FFmpegRunner,
    config: AppConfig,
    encoder: EncoderChoice,
    target: EncodeTarget,
    effects_by_clip: Mapping[str, Sequence[EffectInstance]] | None = None,
    on_progress: RenderProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> RenderedProgramme:
    """Cut every enabled clip and concatenate them into one file (§65).

    Args:
        clips: the timeline's video clips, in order. Disabled clips are skipped
            here rather than filtered by the caller, so "what the viewer sees"
            has one definition.
        sources: recording paths by media id.
        destination: the programme video.
        work_dir: where segments are written. Kept between runs so an
            interrupted render resumes (§47).
        effects_by_clip: the FFmpeg-engine half of the effect plan, keyed by
            clip id with clip-relative times. Realisable effects are baked into
            the clip's segment; the rest stay stored-but-unrealised, exactly as
            all of them were before this parameter existed.

    Returns:
        The concatenated programme. Its duration is *measured*, not assumed:
        the sum of the requested spans and what FFmpeg actually produced can
        differ by a frame per cut, and every later stage compares against a
        real number.
    """
    enabled = [clip for clip in clips if clip.enabled]
    if not enabled:
        raise RenderError(
            "The timeline has no enabled clips to render.",
            code=ErrorCode.RENDER_FAILED,
            details={"clips": len(clips)},
            recoverable=False,
        )

    segments_dir = Path(work_dir) / SEGMENT_DIRNAME
    segments_dir.mkdir(parents=True, exist_ok=True)
    total_seconds = sum(clip.duration for clip in enabled)

    segments: list[Path] = []
    reused = 0
    baked_effects = 0
    elapsed = 0.0
    for position, clip in enumerate(enabled):
        if should_cancel is not None and should_cancel():
            raise CancelledError("Render cancelled between clips.")

        source = sources.get(clip.media_id)
        if source is None or not source.is_file():
            raise RenderError(
                f"The recording for clip {clip.clip_index} is not available.",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"media_id": clip.media_id, "clip_id": clip.id},
                recoverable=False,
            )

        # The transition and the effects are part of the picture, so they are
        # part of the name: a reused segment must carry the same fades and the
        # same baked effects the plan now asks for, and a duration check alone
        # cannot see a 0.3s dip -- or a zoom -- inside a same-length file.
        effects = _realised(clip, (effects_by_clip or {}).get(clip.id, ()))
        baked_effects += len(effects)
        segment = segments_dir / (
            f"{position:05d}_{clip.id}{_fade_token(clip)}{_effects_token(effects)}.mp4"
        )
        if _is_complete(segment, clip.duration, runner):
            reused += 1
        else:
            _cut(
                clip,
                source,
                segment,
                runner=runner,
                config=config,
                encoder=encoder,
                target=target,
                effects=effects,
                should_cancel=should_cancel,
            )
        segments.append(segment)

        elapsed += clip.duration
        if on_progress is not None:
            on_progress(
                min(0.9, elapsed / max(total_seconds, 1e-6) * 0.9),
                f"Cut {position + 1}/{len(enabled)} clips",
            )

    if on_progress is not None:
        on_progress(0.92, "Joining the clips")
    _concatenate(segments, destination, segments_dir, runner, config)

    measured = probe_media(destination, runner).duration_seconds or 0.0
    notes: list[str] = []
    if reused:
        notes.append(f"{reused} segment(s) reused from an earlier run")
    if baked_effects:
        notes.append(f"{baked_effects} effect(s) baked into the picture")
    result = RenderedProgramme(
        path=destination,
        clips=len(enabled),
        duration_seconds=measured,
        reused_segments=reused,
        notes=tuple(notes),
    )
    logger.info("Rendered the programme video", extra=result.summary())
    return result


def clear_segments(work_dir: Path) -> int:
    """Delete the intermediates. Returns how many files went.

    Called once the finished file exists. They are kept until then on purpose:
    §47's resume is worth more during a render than the disk is after one.
    """
    segments_dir = Path(work_dir) / SEGMENT_DIRNAME
    if not segments_dir.is_dir():
        return 0
    removed = 0
    for path in segments_dir.iterdir():
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _cut(
    clip: TimelineClip,
    source: Path,
    destination: Path,
    *,
    runner: FFmpegRunner,
    config: AppConfig,
    encoder: EncoderChoice,
    target: EncodeTarget,
    effects: Sequence[EffectInstance] = (),
    should_cancel: CancelCheck | None,
) -> None:
    """Extract one clip, re-encoded so the cut lands on the requested frame.

    ``-ss`` before ``-i`` seeks by keyframe and is fast; the re-encode is what
    makes the cut frame-accurate. Copying the stream instead would move every
    in-point to the nearest keyframe -- up to a couple of seconds away, which
    is the difference between a clip that opens on the kill and one that opens
    after it.
    """
    partial = destination.with_suffix(".part.mp4")
    partial.unlink(missing_ok=True)

    argv = [
        *runner.base_arguments(),
        *progress_arguments(),
        "-ss", format_seconds(clip.source_in),
        "-i", str(source),
        "-t", format_seconds(clip.duration),
        # One video stream, no audio: the audio path builds the programme
        # separately, because the mix needs the tracks apart (§72).
        "-map", "0:v:0",
        "-an",
        *_scale_arguments(
            clip, target, fades=_fade_filter(clip) + _effect_filters(clip, effects, target)
        ),
        *intermediate_arguments(encoder, config.render),
        "-r", str(target.fps),
        # Every segment must start at zero for the concat demuxer, and a
        # source seek leaves the original timestamps in place.
        "-reset_timestamps", "1",
        "-video_track_timescale", "90000",
        str(partial),
    ]

    runner.run_with_progress(
        argv,
        total_seconds=clip.duration,
        should_cancel=should_cancel,
        timeout_seconds=config.ffmpeg.timeout_seconds,
        error_code=ErrorCode.ENCODING_FAILED,
        error_type=RenderError,
        error_message=f"Could not cut clip {clip.clip_index} from {source.name}.",
        details={"clip_id": clip.id, "source_in": clip.source_in},
    )
    partial.replace(destination)


def _scale_arguments(
    clip: TimelineClip, target: EncodeTarget, *, fades: str = ""
) -> list[str]:
    """Scale and pad every clip to the output frame.

    Recordings in a project can differ in resolution, and the concat demuxer
    refuses streams that do not match. Padding rather than stretching keeps a
    4:3 capture from being distorted into 16:9; the bars are honest.
    """
    return [
        "-vf",
        (
            f"scale={target.width}:{target.height}:force_original_aspect_ratio=decrease,"
            f"pad={target.width}:{target.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1" + fades
        ),
    ]


#: Transitions the video renders as a fade through black. Until this existed
#: no transition reached the picture at all -- FADE and DIP_TO_BLACK were
#: honoured by the audio mix and silently dropped by every frame.
_FADED = frozenset({TransitionType.FADE, TransitionType.DIP_TO_BLACK})

#: When the timeline names the shape but not the length.
_DEFAULT_FADE_SECONDS = 0.4


def _fade_filter(clip: TimelineClip) -> str:
    """The clip's edge fades, as ffmpeg filter steps. Empty for hard cuts."""
    steps = ""
    if clip.transition_in in _FADED:
        seconds = _fade_length(clip, "fade_in_seconds")
        steps += f",fade=t=in:st=0:d={seconds:.3f}"
    if clip.transition_out in _FADED:
        seconds = _fade_length(clip, "fade_out_seconds")
        start = max(0.0, clip.duration - seconds)
        steps += f",fade=t=out:st={start:.3f}:d={seconds:.3f}"
    return steps


def _fade_length(clip: TimelineClip, key: str) -> float:
    """The named fade length, bounded so a short clip is not all fade."""
    raw = clip.metadata.get(key) or clip.metadata.get("fade_seconds")
    try:
        seconds = float(raw) if raw is not None else _DEFAULT_FADE_SECONDS
    except (TypeError, ValueError):
        seconds = _DEFAULT_FADE_SECONDS
    return min(seconds, clip.duration / 4) if clip.duration > 0 else 0.0


def _fade_token(clip: TimelineClip) -> str:
    """A filename fragment naming the fades baked into a segment."""
    parts = ""
    if clip.transition_in in _FADED:
        parts += f"-i{round(_fade_length(clip, 'fade_in_seconds') * 1000)}"
    if clip.transition_out in _FADED:
        parts += f"-o{round(_fade_length(clip, 'fade_out_seconds') * 1000)}"
    return parts


# ---------------------------------------------------------------------------
# planned effects, realised as per-clip filters (§68)
# ---------------------------------------------------------------------------
#
# The planner stored these rows from Phase 8 on, positioned to the centisecond,
# and no renderer ever read them: seventeen effects across three real projects,
# none visible in any finished video. This is the FFmpeg half of the wire.
#
# Only duration-neutral effects are realised here. Nothing below changes the
# frame count or a timestamp, which is what keeps every §76 QA gate untouched:
# a letterbox covers 26% of the frame (blackdetect needs the whole picture),
# a zoom re-samples pixels, a flash lifts brightness, a shake pans a padded
# frame. Time-warping effects (slow_motion, speed_ramp, freeze_frame) need the
# EDL to re-lay clip durations first and stay unrealised for now; blur/glow's
# configured modes (background isolation, highlight lift) are not honest
# one-filter jobs and a full-frame approximation would misrepresent the plan.

#: Bumped when a filter formula changes, so segments baked with the old
#: formula stop matching their name and §47's reuse re-cuts them.
_EFFECT_FILTER_VERSION: Final[int] = 2

#: Shorter than this on the clip, an effect is a flicker, not an effect.
_MIN_EFFECT_SECONDS: Final[float] = 0.05

#: Decay constant for the shake envelope: the jolt dies within about a second,
#: which is what "impact" reads as -- a sustained wobble reads as a broken pan.
_SHAKE_DECAY_SECONDS: Final[float] = 0.3

#: Letterbox bars taller than this fraction of the frame are not cinema, they
#: are blackout: 2.39 on a 16:9 target costs ~13% per bar, but the same ratio
#: on a 9:16 Shorts target would cover 76% of the picture.
_MAX_BAR_FRACTION: Final[float] = 0.2


def _realised(clip: TimelineClip, effects: Sequence[EffectInstance]) -> list[EffectInstance]:
    """The effects this renderer will actually bake into ``clip``'s segment.

    Order is by start time so the token -- and the filter chain -- are stable
    for the same plan regardless of how the rows came back.
    """
    kept = [
        effect
        for effect in effects
        if effect.effect in _REALISED_EFFECTS and _effect_span(clip, effect) is not None
    ]
    return sorted(kept, key=lambda effect: (effect.start_seconds, effect.effect.value))


def _effect_span(clip: TimelineClip, effect: EffectInstance) -> tuple[float, float] | None:
    """The effect's window in clip-relative seconds, clamped to the clip."""
    start = max(0.0, min(effect.start_seconds, clip.duration))
    end = min(start + effect.duration_seconds, clip.duration)
    if end - start < _MIN_EFFECT_SECONDS:
        return None
    return start, end


def _effects_token(effects: Sequence[EffectInstance]) -> str:
    """A filename fragment naming the effects baked into a segment.

    Everything that shapes the filter chain is hashed -- type, window, the
    numeric and string parameters, the formula version -- so §47's reuse can
    never serve a segment whose picture no longer matches the plan.
    """
    if not effects:
        return ""
    parts = [
        (
            effect.effect.value,
            round(effect.start_seconds, 2),
            round(effect.duration_seconds, 2),
            round(effect.strength, 3),
            sorted(
                (key, value)
                for key, value in effect.params.items()
                if isinstance(value, (int, float, str, bool))
            ),
        )
        for effect in effects
    ]
    digest = zlib.crc32(repr((_EFFECT_FILTER_VERSION, parts)).encode("utf-8"))
    return f"-fx{digest:08x}"


def _effect_filters(
    clip: TimelineClip, effects: Sequence[EffectInstance], target: EncodeTarget
) -> str:
    """The clip's baked effects, as ffmpeg filter steps. Empty for none.

    Every step keys its window off the frame timestamp ``t`` (or zoompan's
    ``it``), which after the input seek starts at zero -- the same clip-relative
    clock the stored rows use. Nothing here alters timestamps or frame count.

    Camera moves come before frame furniture, whatever their start times: a
    letterbox drawn first and zoomed after visibly thickens by the zoom factor
    and snaps back. Geometry filters (zoom, punch, shake) reshape the world;
    bars and flashes are drawn on the finished frame.
    """
    geometry = [effect for effect in effects if _BUILDERS[effect.effect][1]]
    furniture = [effect for effect in effects if not _BUILDERS[effect.effect][1]]

    steps = ""
    if any(effect.effect in (EffectType.ZOOM, EffectType.PUNCH_IN) for effect in geometry):
        # zoompan regenerates timestamps at its own rate. Conforming the frame
        # rate *before* it keeps a lower-rate source from being time-compressed;
        # for a source already at the target rate this is a no-op.
        steps += f",fps={target.fps}"
    for effect in (*geometry, *furniture):
        span = _effect_span(clip, effect)
        if span is None:
            continue
        start, end = span
        steps += _BUILDERS[effect.effect][0](effect, start, end, target)
    return steps


def _zoom_step(
    effect: EffectInstance, start: float, end: float, target: EncodeTarget
) -> str:
    """A push toward the centre: eased for ``zoom``, instant for ``punch_in``.

    ``zoompan`` with ``d=1`` passes each frame through once; ``s`` and ``fps``
    pin its output geometry and clock to the segment's own, so the only thing
    that changes is the sampling. A zoom eases in over 60% of its window and
    back out over the rest -- returning to 1.0 exactly at the end, because a
    zoom that snaps back is worse than no zoom. A punch-in is a hard cut to a
    tighter framing and a hard cut back; that is the effect, not a defect.
    """
    scale = _numeric(effect.params.get("scale"), 1.12)
    scale = max(1.0, min(scale, _numeric(effect.params.get("max_scale"), 1.35)))
    if effect.effect is EffectType.PUNCH_IN:
        expression = f"if(between(it,{start:.3f},{end:.3f}),{scale:.4f},1)"
    else:
        ease_in = max((end - start) * 0.6, 1e-3)
        ease_out = max((end - start) * 0.4, 1e-3)
        # Commas stay bare: the surrounding single quotes already shield them
        # from the filtergraph splitter, and no escape processing happens
        # inside quotes -- a backslash would reach the expression parser.
        expression = (
            f"1+({scale:.4f}-1)*max(0,min(1,min("
            f"(it-{start:.3f})/{ease_in:.3f},({end:.3f}-it)/{ease_out:.3f})))"
        )
    return (
        f",zoompan=z='{expression}'"
        ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={target.width}x{target.height}:fps={target.fps}"
    )


def _bars_step(
    effect: EffectInstance, start: float, end: float, target: EncodeTarget
) -> str:
    """Letterbox bars for a cinematic beat, sized from the configured ratio.

    Skipped entirely when the bars would eat the picture: 2.39 letterboxes a
    16:9 frame with ~13% bars, but the same arithmetic on a 9:16 Shorts target
    yields 76% black -- a blackout, not a cinematic beat.
    """
    ratio = max(_numeric(effect.params.get("ratio"), 2.39), 1e-6)
    bar = round((target.height - target.width / ratio) / 2)
    if bar <= 0 or bar > target.height * _MAX_BAR_FRACTION:
        return ""
    enable = f"between(t,{start:.3f},{end:.3f})"
    return (
        f",drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill:enable='{enable}'"
        f",drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill:enable='{enable}'"
    )


def _flash_step(
    effect: EffectInstance, start: float, end: float, target: EncodeTarget
) -> str:
    """A brief brightness lift, triangular over the window."""
    del target  # uniform builder signature; brightness needs no geometry
    peak = max(0.0, min(_numeric(effect.params.get("peak_opacity"), 0.33), 1.0))
    duration = max(end - start, 1e-3)
    return (
        f",eq=brightness='if(between(t,{start:.3f},{end:.3f}),"
        f"{peak:.3f}*(1-abs(2*(t-{start:.3f})/{duration:.3f}-1)),0)':eval=frame"
    )


def _shake_step(
    effect: EffectInstance, start: float, end: float, target: EncodeTarget
) -> str:
    """A decaying jolt: pad the frame, then let the crop window wander.

    ``crop``'s x/y are evaluated per frame with ``t`` available; outside the
    window the expression collapses to the pad offset and the picture is
    pixel-identical to an unshaken one.
    """
    amplitude = max(1, round(_numeric(effect.params.get("amplitude_px"), 5.0)))
    frequency = max(0.1, _numeric(effect.params.get("frequency_hz"), 14.0))
    omega = 2.0 * math.pi * frequency
    window = f"between(t,{start:.3f},{end:.3f})"
    wobble = f"exp(-(t-{start:.3f})/{_SHAKE_DECAY_SECONDS})"
    jolt_x = f"{amplitude}*sin((t-{start:.3f})*{omega:.3f})*{wobble}"
    jolt_y = f"{amplitude}*cos((t-{start:.3f})*{omega * 0.9:.3f})*{wobble}"
    return (
        f",pad=iw+{2 * amplitude}:ih+{2 * amplitude}:{amplitude}:{amplitude}"
        f",crop={target.width}:{target.height}"
        f":x='{amplitude}+if({window},{jolt_x},0)'"
        f":y='{amplitude}+if({window},{jolt_y},0)'"
    )


def _numeric(value: object, fallback: float) -> float:
    """A parameter as a float, or the fallback -- params arrive from JSON."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return fallback


_StepBuilder = Callable[[EffectInstance, float, float, EncodeTarget], str]

#: Realiser and whether it reshapes geometry, by effect type. The realisable
#: set below is *derived* from this table, so a new effect cannot be
#: half-registered: absent means stored-but-unrealised (and never counted,
#: named or tokenised), present means built. The geometry flag orders the
#: chain -- camera moves reshape the world before bars and flashes are drawn
#: on the finished frame.
_BUILDERS: Final[dict[EffectType, tuple[_StepBuilder, bool]]] = {
    EffectType.ZOOM: (_zoom_step, True),
    EffectType.PUNCH_IN: (_zoom_step, True),
    EffectType.CAMERA_SHAKE: (_shake_step, True),
    EffectType.CINEMATIC_BARS: (_bars_step, False),
    EffectType.FLASH: (_flash_step, False),
}

#: The effects this renderer can bake without touching duration.
_REALISED_EFFECTS: Final[frozenset[EffectType]] = frozenset(_BUILDERS)


def _is_complete(segment: Path, expected_seconds: float, runner: FFmpegRunner) -> bool:
    """Whether an existing segment can be reused (§47).

    Measured rather than trusted. A segment left behind by a killed process is
    a readable file of the wrong length, and reusing it would splice a
    truncated clip into the finished video without a word.
    """
    if not segment.is_file() or segment.stat().st_size == 0:
        return False
    try:
        measured = probe_media(segment, runner).duration_seconds or 0.0
    except (MediaError, RenderError, OSError):
        # An unreadable segment is simply not reusable. Anything else is a bug
        # and should not be swallowed here.
        return False
    return abs(measured - expected_seconds) <= SEGMENT_TOLERANCE_SECONDS


def _concatenate(
    segments: Sequence[Path],
    destination: Path,
    work_dir: Path,
    runner: FFmpegRunner,
    config: AppConfig,
) -> None:
    """Join the segments without re-encoding them."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    partial.unlink(missing_ok=True)

    if len(segments) == 1:
        shutil.copyfile(segments[0], partial)
        partial.replace(destination)
        return

    # Relative names with forward slashes: the concat demuxer resolves them
    # against the list file's directory and treats a backslash as an escape,
    # so a Windows path written literally would not survive.
    listing = work_dir / SEGMENT_LIST_FILENAME
    listing.write_text(
        "".join(f"file '{path.name}'\n" for path in segments), encoding="utf-8"
    )

    with log_duration(logger, "Joined the render segments", segments=len(segments)):
        runner.run(
            [
                *runner.base_arguments(),
                "-f", "concat",
                "-safe", "0",
                "-i", str(listing),
                "-c", "copy",
                str(partial),
            ],
            timeout_seconds=config.ffmpeg.timeout_seconds,
            error_code=ErrorCode.RENDER_FAILED,
            error_type=RenderError,
            error_message="Could not join the rendered clips.",
            details={"segments": len(segments)},
        )
    partial.replace(destination)


__all__ = [
    "SEGMENT_DIRNAME",
    "SEGMENT_LIST_FILENAME",
    "RenderedProgramme",
    "clear_segments",
    "render_programme",
]
