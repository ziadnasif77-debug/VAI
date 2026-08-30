"""The final pass: programme + overlay + mix → MP4 (SPEC §65, §67, §72–§75).

§67's chain ends here: `Source → EDL → Timeline → Remotion Composition →
FFmpeg → Final MP4`. The video has been cut and joined, the overlay rendered on
a transparent canvas, and the audio tracks are waiting to be mixed. This puts
them together and encodes once.

Once, deliberately. Compositing to an intermediate and then encoding would add
a generation of loss to every frame for no benefit — the overlay, the mix and
the encoder all fit in one filter graph, and FFmpeg is perfectly happy to run
them together.

The overlay input carries a decoder name it looks like it should not need. It
does: VP9 in WebM keeps its alpha in a side channel that FFmpeg's *native* VP9
decoder discards silently, and the overlay would composite as an opaque
rectangle over the whole video. `overlay_input_arguments` is where that lives,
next to the code that wrote the file.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, RenderError
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.media.ffmpeg import FFmpegRunner, ProgressReport, progress_arguments
from backend.media.probe import ProbeResult, probe_media
from backend.rendering.audio_mix import MixPlan
from backend.rendering.encoder import (
    EncoderChoice,
    EncodeTarget,
    audio_arguments,
    container_arguments,
    video_arguments,
)
from backend.rendering.overlay_plan import OverlayPlan
from backend.rendering.remotion import overlay_input_arguments

logger = get_logger("rendering.composite", LogChannel.RENDERING)

CompositeProgress = Callable[[float, str], None]
CancelCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class FinalRender:
    """The finished file, described by what is actually in it."""

    path: Path
    duration_seconds: float
    width: int
    height: int
    fps: float
    size_bytes: int
    video_codec: str
    audio_codec: str
    has_overlay: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "duration_seconds": round(self.duration_seconds, 3),
            "resolution": f"{self.width}x{self.height}",
            "fps": self.fps,
            "size_bytes": self.size_bytes,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "has_overlay": self.has_overlay,
            "notes": list(self.notes),
        }


def composite(
    programme: Path,
    *,
    overlay: Path | None,
    overlay_plan: OverlayPlan | None = None,
    mix: MixPlan | None,
    destination: Path,
    runner: FFmpegRunner,
    config: AppConfig,
    encoder: EncoderChoice,
    target: EncodeTarget,
    duration_seconds: float,
    on_progress: CompositeProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> FinalRender:
    """Composite the overlay, mix the audio and encode the finished video.

    Args:
        programme: the concatenated gameplay video, without audio.
        overlay: the alpha layer, or ``None`` when there was nothing to draw.
        overlay_plan: where the overlay's stretches belong, when Remotion was
            asked for only the frames that carry something. ``None`` means the
            file runs the length of the video.
        mix: the audio graph, or ``None`` for a silent render.
        duration_seconds: the expected length, used for progress and to bound
            the output. FFmpeg is told to stop there so a looping music input
            cannot extend the video past its own picture.

    Returns:
        A :class:`FinalRender` built by **probing the output**, not by
        restating the request. An encoder that quietly produced 29.97 fps
        instead of 30 should be visible here rather than in the QA phase.
    """
    if not programme.is_file():
        raise RenderError(
            "The programme video is missing; nothing to composite.",
            code=ErrorCode.RENDER_FAILED,
            details={"expected": str(programme)},
            recoverable=False,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    partial.unlink(missing_ok=True)

    argv, notes = _build_command(
        programme,
        overlay=overlay,
        overlay_plan=overlay_plan,
        mix=mix,
        destination=partial,
        runner=runner,
        config=config,
        encoder=encoder,
        target=target,
        duration_seconds=duration_seconds,
    )

    with log_duration(
        logger, "Encoded the final video", output=str(destination), overlay=bool(overlay)
    ) as fields:
        runner.run_with_progress(
            argv,
            total_seconds=duration_seconds,
            on_progress=((lambda report: _relay(report, on_progress)) if on_progress else None),
            should_cancel=should_cancel,
            timeout_seconds=config.ffmpeg.timeout_seconds,
            error_code=ErrorCode.ENCODING_FAILED,
            error_type=RenderError,
            error_message="The final encode failed.",
            details={"destination": str(destination)},
        )
        fields["size_bytes"] = partial.stat().st_size if partial.is_file() else 0

    if not partial.is_file() or partial.stat().st_size == 0:
        raise RenderError(
            "The encoder reported success but produced no file.",
            code=ErrorCode.ENCODING_FAILED,
            details={"expected": str(partial)},
        )
    partial.replace(destination)

    probe = probe_media(destination, runner)
    result = _describe(destination, probe, overlay=overlay is not None, notes=notes)
    logger.info("Final render complete", extra=result.summary())
    return result


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _build_command(
    programme: Path,
    *,
    overlay: Path | None,
    overlay_plan: OverlayPlan | None,
    mix: MixPlan | None,
    destination: Path,
    runner: FFmpegRunner,
    config: AppConfig,
    encoder: EncoderChoice,
    target: EncodeTarget,
    duration_seconds: float,
) -> tuple[list[str], tuple[str, ...]]:
    """Assemble the whole invocation.

    Built as a list rather than a string, and returned rather than run, so the
    command a render *would* use is inspectable without producing a file --
    which is the only practical way to test an encode's flags.
    """
    notes: list[str] = []
    argv = [*runner.base_arguments(), *progress_arguments()]

    # Input 0 is always the programme picture. Every filter label below counts
    # from there, so this must stay first.
    argv += ["-i", str(programme)]
    filters: list[str] = []
    video_label = "0:v:0"

    if overlay is not None:
        argv += overlay_input_arguments(overlay, config.remotion.overlay_format)
        if overlay_plan is None:
            filters.append(
                # shortest=0: the overlay may be a frame shorter than the
                # picture after rounding, and stopping the video there would
                # truncate it.
                "[0:v][1:v]overlay=format=auto:shortest=0[composited]"
            )
            video_label = "composited"
        else:
            placed, video_label = _placed_segments(overlay_plan)
            filters.extend(placed)
            notes.append(
                f"the overlay was rendered as {len(overlay_plan.segments)} stretch(es) "
                f"covering {overlay_plan.render_frames} of {overlay_plan.total_frames} frames"
            )
    else:
        notes.append("no overlay layer: the video carries no captions or graphics")

    audio_label: str | None = None
    if mix is not None:
        offset = 2 if overlay is not None else 1
        argv += mix.input_arguments()
        filters.append(_reindexed(mix.filter_complex, offset))
        audio_label = mix.output_label
        notes.extend(mix.notes)
    else:
        notes.append("no audio inputs: the render is silent")

    if filters:
        graph = ";".join(filters)
        if len(graph) > 4000:
            # Windows caps an argv near 32K and the mix graph grows with the
            # clip count; a script file has no ceiling. Small graphs stay
            # inline so the command remains inspectable in one piece.
            graph_path = destination.with_suffix(".filters")
            graph_path.write_text(graph, encoding="utf-8")
            argv += ["-filter_complex_script", str(graph_path)]
        else:
            argv += ["-filter_complex", graph]
        argv += ["-map", f"[{video_label}]" if video_label != "0:v:0" else "0:v:0"]
        if audio_label is not None:
            argv += ["-map", f"[{audio_label}]"]
    else:
        argv += ["-map", "0:v:0"]

    argv += video_arguments(encoder, target, config.render)
    if audio_label is not None:
        argv += audio_arguments(target)
    else:
        argv += ["-an"]

    argv += container_arguments(config.render)
    # Bounded explicitly: a looping music input has no natural end, and without
    # this the audio would keep going long after the last frame.
    argv += ["-t", f"{duration_seconds:.3f}", str(destination)]
    return argv, tuple(notes)


def _placed_segments(plan: OverlayPlan) -> tuple[list[str], str]:
    """Cut the rendered overlay back into its stretches and lay each one down.

    The overlay file holds the drawn stretches end to end with the transparent
    gaps removed, so putting it back is: split the stream once per stretch,
    trim each to its own frames, shift its timestamps to where it belongs, and
    overlay them in order.

    ``eof_action=pass`` and ``repeatlast=0`` are what make the gaps gaps. The
    default overlay behaviour freezes the last overlay frame for the rest of
    the video and stops the whole graph when the overlay ends -- either one
    would paint a caption across the footage that follows it.
    """
    fps = plan.fps
    if fps <= 0:
        raise ValueError("an overlay plan needs its own fps to be placed in time")

    segments = plan.segments
    filters: list[str] = []
    labels = [f"seg{index}" for index in range(len(segments))]
    if len(segments) > 1:
        filters.append(
            "[1:v]split=" + str(len(segments)) + "".join(f"[{label}]" for label in labels)
        )
    else:
        labels = ["1:v"]

    label = "0:v"
    for index, (segment, source) in enumerate(zip(segments, labels, strict=True)):
        shifted = f"place{index}"
        filters.append(
            f"[{source}]trim=start_frame={segment.render_start}:"
            f"end_frame={segment.render_end},"
            f"setpts=PTS-STARTPTS+{segment.source_start / fps:.6f}/TB[{shifted}]"
        )
        output = f"over{index}"
        filters.append(
            f"[{label}][{shifted}]overlay=format=auto:eof_action=pass:repeatlast=0[{output}]"
        )
        label = output
    return filters, label


def _reindexed(filter_complex: str, offset: int) -> str:
    """Shift a mix graph's input indices past the video inputs.

    The mix is planned without knowing whether an overlay will be present, so
    its indices start at zero. Rewriting them here keeps that module free of a
    concern that belongs to whoever assembles the command.
    """
    if offset == 0:
        return filter_complex
    result = filter_complex
    # Highest first, so rewriting [0:a] does not then match a shifted [1:a].
    for index in range(_highest_input(filter_complex), -1, -1):
        result = result.replace(f"[{index}:a]", f"[\x00{index + offset}:a]")
    return result.replace("\x00", "")


def _highest_input(filter_complex: str) -> int:
    """The largest ``[N:a]`` index a graph refers to."""
    highest = -1
    for index in range(200):
        if f"[{index}:a]" in filter_complex:
            highest = index
    return highest


def _relay(report: ProgressReport, on_progress: CompositeProgress | None) -> None:
    """Turn an FFmpeg progress block into a fraction and a message."""
    if on_progress is None:
        return
    fraction = report.fraction if report.fraction is not None else 0.0
    on_progress(
        min(max(fraction, 0.0), 1.0),
        f"Encoding {report.out_time_seconds / 60:.1f} min",
    )


def _describe(
    path: Path, probe: ProbeResult, *, overlay: bool, notes: Sequence[str]
) -> FinalRender:
    metadata = probe.metadata
    return FinalRender(
        path=path,
        duration_seconds=probe.duration_seconds,
        width=metadata.width or 0,
        height=metadata.height or 0,
        fps=metadata.fps or 0.0,
        size_bytes=path.stat().st_size,
        video_codec=metadata.video_codec or "",
        audio_codec=metadata.audio_codec or "",
        has_overlay=overlay,
        notes=tuple(notes),
    )


__all__ = ["FinalRender", "composite"]
