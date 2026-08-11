"""The RENDER stage (SPEC §65, §67, §72–§75).

The stage that produces a file someone can watch. Everything before it is
description; this is the first thing a viewer could actually judge.

The order is §67's, and each step's output is checked before the next reads it:

    cut clips → concat → render overlay → mix audio → composite → probe

The last step is not a formality. An encoder can exit zero having produced a
file with the wrong frame rate, no audio stream, or a duration that drifted a
second per hour — and the acceptance for this phase is about the *file*, not
about the exit code. So the finished MP4 is probed and what it really contains
is what gets recorded.

Failure here is loud but not destructive: the previous render, if any, is left
alone until the new one is complete, because a user who asked for a re-render
and got neither video is worse off than before they asked.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.errors import ErrorCode, GamingEditorError, RenderError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.renders import RenderRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.pipeline.workers.base import WorkerContext
from backend.rendering import audio_mix
from backend.rendering.composite import composite
from backend.rendering.composition import build_composition, resolution_for
from backend.rendering.encoder import EncodeTarget, select_encoder
from backend.rendering.ffmpeg_renderer import clear_segments, render_programme
from backend.rendering.remotion import render_overlay
from backend.timeline.models import Timeline

logger = get_logger("pipeline.workers.render", LogChannel.RENDERING)


class RenderWorker:
    """RENDER -- the finished MP4 (§65, §67)."""

    stage = JobStage.RENDER

    def run(self, context: WorkerContext) -> dict[str, Any]:
        repository = TimelineRepository(context.database)
        timeline = repository.load(context.project_id)
        clips = timeline.video_clips()
        if not clips:
            # Nothing to render is a dead end, not a crash: the EDL stage
            # already explained why, and producing an empty file would hide it.
            context.report(1.0, "No timeline to render")
            return {"skipped": True, "reason": "the timeline has no enabled clips"}

        target, width, height = self._target(context)
        runner = context.ffmpeg
        encoder = select_encoder(context.config.render, runner)

        renders = RenderRepository(context.database)
        render_id = renders.start(
            context.project_id,
            resolution=target.height,
            fps=target.fps,
            encoder=encoder.name,
        )
        started = time.perf_counter()

        try:
            result = self._render(
                context, timeline, target, encoder, repository, render_id
            )
        except GamingEditorError as error:
            renders.fail(
                render_id, error_code=error.code.value, error_message=str(error)
            )
            raise

        renders.complete(
            render_id,
            output_path=str(result["output_path"]),
            duration_seconds=result["duration_seconds"],
            size_bytes=result["size_bytes"],
            video_codec=result["video_codec"],
            audio_codec=result["audio_codec"],
            render_seconds=time.perf_counter() - started,
        )
        return {
            **result,
            "render_id": render_id,
            "encoder": encoder.name,
            "hardware_encoder": encoder.hardware,
            "render_seconds": round(time.perf_counter() - started, 2),
            "width": width,
            "height": height,
            "fps": target.fps,
        }

    # -- steps ----------------------------------------------------------

    def _render(
        self,
        context: WorkerContext,
        timeline: Timeline,
        target: EncodeTarget,
        encoder,
        repository: TimelineRepository,
        render_id: str,
    ) -> dict[str, Any]:
        paths = context.paths
        # Already project-scoped. Intermediates live beside the finished files
        # so §47's resume finds them, and are cleared once the MP4 exists.
        work_dir = paths.renders / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        sources = self._sources(context)

        context.report(0.05, "Cutting the clips")
        programme = render_programme(
            timeline.video_clips(),
            sources,
            destination=work_dir / "programme.mp4",
            work_dir=work_dir,
            runner=context.ffmpeg,
            config=context.config,
            encoder=encoder,
            target=target,
            on_progress=lambda fraction, message: context.report(
                0.05 + fraction * 0.45, message
            ),
            should_cancel=context.should_cancel,
        )

        context.report(0.52, "Rendering the overlay")
        overlay = self._overlay(
            context, timeline, repository, target, programme.duration_seconds, work_dir
        )

        context.report(0.6, "Mixing the audio")
        mix = self._mix(
            context, timeline, sources, programme.duration_seconds, work_dir
        )

        context.report(0.65, "Encoding the final video")
        destination = paths.renders / f"{render_id}.mp4"
        final = composite(
            programme.path,
            overlay=overlay.path if overlay is not None else None,
            mix=mix,
            destination=destination,
            runner=context.ffmpeg,
            config=context.config,
            encoder=encoder,
            target=target,
            duration_seconds=programme.duration_seconds,
            on_progress=lambda fraction, message: context.report(
                0.65 + fraction * 0.33, message
            ),
            should_cancel=context.should_cancel,
        )

        removed = clear_segments(work_dir)
        context.report(1.0, f"Rendered {final.duration_seconds / 60:.1f} minutes")
        return {
            "output_path": str(final.path),
            "duration_seconds": round(final.duration_seconds, 3),
            "size_bytes": final.size_bytes,
            "video_codec": final.video_codec,
            "audio_codec": final.audio_codec,
            "has_overlay": final.has_overlay,
            "clips": programme.clips,
            "reused_segments": programme.reused_segments,
            "segments_removed": removed,
            "notes": [*programme.notes, *final.notes],
        }

    def _target(self, context: WorkerContext) -> tuple[EncodeTarget, int, int]:
        """The output format (§75).

        The YouTube preset is the default target; the video section decides the
        aspect ratio, because a preset that carried its own would disagree with
        the rest of the configuration the first time someone changed one.
        """
        video = context.config.video
        preset = context.config.youtube_preset
        width, height = resolution_for(preset.resolution, video.aspect_ratio)
        return EncodeTarget.from_preset(preset, width=width), width, height

    def _sources(self, context: WorkerContext) -> dict[str, Path]:
        """Recording paths by media id, checked for existence.

        The source is referenced in place (§42), so a file can be unplugged
        between analysis and render. Better to say which one is missing than to
        have FFmpeg fail on an input nobody named.
        """
        media = MediaRepository(context.database).list_for_project(context.project_id)
        sources: dict[str, Path] = {}
        for item in media:
            path = Path(item.source_path)
            if not path.is_file():
                raise RenderError(
                    f"The recording {item.filename} is no longer at {path}.",
                    code=ErrorCode.MEDIA_NOT_FOUND,
                    details={"media_id": item.id},
                    recoverable=False,
                )
            sources[item.id] = path
        return sources

    def _overlay(
        self,
        context: WorkerContext,
        timeline: Timeline,
        repository: TimelineRepository,
        target: EncodeTarget,
        duration_seconds: float,
        work_dir: Path,
    ):
        """Render the caption and graphics layer, or skip it (§66, D-008)."""
        captions = repository.list_captions(context.project_id)
        composition = build_composition(
            timeline,
            captions=captions,
            effects=(),
            caption_config=context.config.captions,
            width=target.width,
            height=target.height,
            fps=target.fps,
        )
        # The overlay must be exactly as long as the picture it sits on, and
        # the picture's measured length is the only number that is certainly
        # right (a frame per cut can differ from the sum of the spans).
        composition = _resized(composition, duration_seconds, target.fps)

        result = render_overlay(
            composition,
            output_path=work_dir / "overlay.webm",
            config=context.config.remotion,
            repo_root=_repo_root(),
            on_progress=lambda fraction, message: context.report(
                0.52 + fraction * 0.07, message
            ),
            should_cancel=context.should_cancel,
        )
        if result.skipped:
            logger.info("Rendering without an overlay", extra={"reason": result.reason})
            return None
        return result

    def _mix(
        self,
        context: WorkerContext,
        timeline: Timeline,
        sources: dict[str, Path],
        duration_seconds: float,
        work_dir: Path,
    ):
        """Build the audio mix (§72–§74).

        Speech spans come from the captions, which are the transcript already
        mapped onto the finished timeline -- the same mapping §71 requires, so
        the ducking and the captions cannot disagree about when someone spoke.
        """
        config = context.config.audio
        repository = TimelineRepository(context.database)
        captions = repository.list_captions(context.project_id)
        spoken = [(caption.timeline_start, caption.timeline_end) for caption in captions]

        music_envelope = None
        game_envelope = None
        if config.ducking.enabled and spoken:
            music_spans = audio_mix.merge_spans(audio_mix.speech_spans(spoken, config))
            music_envelope = audio_mix.write_envelope(
                audio_mix.build_envelope(
                    music_spans, duration_seconds=duration_seconds, config=config
                ),
                work_dir / "duck_music.wav",
            )
            game_spans = audio_mix.merge_spans(
                audio_mix.game_under_speech_spans(spoken, config)
            )
            game_envelope = audio_mix.write_envelope(
                audio_mix.build_envelope(
                    game_spans, duration_seconds=duration_seconds, config=config
                ),
                work_dir / "duck_game.wav",
            )

        game_audio = self._programme_audio(context, timeline, sources, work_dir)
        if game_audio is None:
            return None

        # §73: the user's own directory inside the project, and nowhere else.
        music = audio_mix.find_music(context.paths.assets / "music")
        return audio_mix.plan_mix(
            game=game_audio,
            microphone=None,
            music=music,
            music_envelope=music_envelope,
            game_envelope=game_envelope,
            config=config,
            duration_seconds=duration_seconds,
        )

    def _programme_audio(
        self,
        context: WorkerContext,
        timeline: Timeline,
        sources: dict[str, Path],
        work_dir: Path,
    ) -> Path | None:
        """Cut and concatenate the gameplay audio to match the edit.

        Built with one filter graph rather than per-clip files: audio is cheap
        enough that the reasons video is segmented -- resume, progress,
        per-clip failures -- do not apply, and a single pass keeps the sample
        alignment exact across every cut.
        """
        clips = timeline.video_clips()
        if not clips:
            return None

        inputs: list[str] = []
        chains: list[str] = []
        labels: list[str] = []
        for position, clip in enumerate(clips):
            source = sources[clip.media_id]
            inputs += ["-i", str(source)]
            chains.append(
                f"[{position}:a:0]atrim=start={clip.source_in:.6f}:end={clip.source_out:.6f},"
                f"asetpts=N/SR/TB,{audio_mix.MIX_FORMAT}[a{position}]"
            )
            labels.append(f"[a{position}]")

        destination = work_dir / "programme_audio.wav"
        graph = ";".join(
            [*chains, f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[aout]"]
        )
        context.ffmpeg.run(
            [
                *context.ffmpeg.base_arguments(),
                *inputs,
                "-filter_complex", graph,
                "-map", "[aout]",
                "-c:a", "pcm_s16le",
                str(destination),
            ],
            timeout_seconds=context.config.ffmpeg.timeout_seconds,
            error_code=ErrorCode.AUDIO_MIX_FAILED,
            error_type=RenderError,
            error_message="Could not assemble the programme audio.",
            details={"clips": len(clips)},
        )
        return destination


def _resized(composition, duration_seconds: float, fps: int):
    """Return the composition stretched or trimmed to the measured duration."""
    from dataclasses import replace

    frames = max(1, round(duration_seconds * fps))
    return replace(composition, duration_in_frames=frames)


def _repo_root() -> Path:
    from backend.config.paths import find_repository_root

    return find_repository_root()


__all__ = ["RenderWorker"]
