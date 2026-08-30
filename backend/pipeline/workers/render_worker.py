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

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.core.errors import ErrorCode, GamingEditorError, RenderError
from backend.core.gpu import contention, release_everything_we_loaded
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import EffectEngine, EffectType, JobStage
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.renders import RenderRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.effects.models import EffectInstance
from backend.pipeline.workers.base import WorkerContext
from backend.rendering import audio_mix, jl, sfx
from backend.rendering.composite import composite
from backend.rendering.composition import build_composition, resolution_for
from backend.rendering.encoder import EncodeTarget, select_encoder
from backend.rendering.ffmpeg_renderer import clear_segments, render_programme
from backend.rendering.remotion import render_overlay
from backend.timeline import retime
from backend.timeline.models import Timeline, TimelineClip

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

        # The card, to itself. Every stage before this one is a specialist
        # that has finished -- Whisper, then a vision model, then a reasoning
        # model -- and none of their memory is of any use to NVENC or to
        # Chromium. Each provider already unloads in a `finally`, but only
        # when *that instance* loaded the model: one left by a previous run or
        # a killed process is invisible to it and stays on the card. Asking by
        # name costs milliseconds and works whoever loaded it.
        if context.config.gpu.release_before_render:
            freed = release_everything_we_loaded(context.config)
            if freed["released"] or freed["freed_mb"]:
                logger.info("Freed the card before rendering", extra=freed)

        # And then what is left, which is not ours to take. §54's "one heavy
        # model at a time" is honoured between this project's own stages and
        # assumes nothing else is on the machine -- but this one has an
        # OpenHands install reaching the same Ollama daemon from Docker, and a
        # `qwen2.5-coder:7b` it left resident once held 4.7 GB while a render
        # waited twenty minutes to find out. Said here, before the twenty
        # minutes, and said rather than acted on: another program's model may
        # be mid-request, and taking its memory is the discourtesy this
        # project asks not to receive.
        held = contention(context.config)
        if held["foreign_models"]:
            logger.warning("The card is not empty and the rest is not ours", extra=held)
            context.report(0.05, held["message"])

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
        effects = self._effects(context, repository, timeline)

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
            effects_by_clip=self._by_clip(
                effects.get(EffectEngine.FFMPEG, ()), timeline
            ),
            on_progress=lambda fraction, message: context.report(
                0.05 + fraction * 0.45, message
            ),
            should_cancel=context.should_cancel,
        )

        context.report(0.52, "Rendering the overlay")
        overlay = self._overlay(
            context,
            timeline,
            repository,
            target,
            programme.duration_seconds,
            work_dir,
            effects=effects.get(EffectEngine.REMOTION, ()),
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
            # Where the rendered stretches go back, when only the frames that
            # carry something were sent through Chromium.
            overlay_plan=overlay.plan if overlay is not None else None,
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
        delivered, delivery_note = self._deliver(context, final.path)
        context.report(1.0, f"Rendered {final.duration_seconds / 60:.1f} minutes")
        return {
            **({"delivered_to": delivered} if delivered else {}),
            "output_path": str(final.path),
            "duration_seconds": round(final.duration_seconds, 3),
            "size_bytes": final.size_bytes,
            "video_codec": final.video_codec,
            "audio_codec": final.audio_codec,
            "has_overlay": final.has_overlay,
            "clips": programme.clips,
            "reused_segments": programme.reused_segments,
            "segments_removed": removed,
            "notes": [
                *programme.notes,
                *final.notes,
                *([delivery_note] if delivery_note else []),
            ],
        }

    def _deliver(self, context: WorkerContext, rendered) -> tuple[str | None, str | None]:
        """Copy the finished file where the project asked to receive it.

        ``(destination, note)``. The copy is a delivery, not the render: a
        full disk or a vanished folder at the chosen location must not turn a
        finished video into a failed job (§95), so failure here is a note on
        success, never an exception.
        """
        import shutil

        from backend.core.fs import sanitize_filename
        from backend.database.repositories.projects import ProjectRepository

        project = ProjectRepository(context.database).get(context.project_id)
        if project is None or not project.output_directory:
            return None, None
        directory = Path(project.output_directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"{sanitize_filename(project.name) or project.id}.mp4"
            shutil.copy2(rendered, destination)
        except OSError as exc:
            logger.warning(
                "Could not deliver the finished video to the chosen folder",
                extra={"directory": str(directory), "error": str(exc)},
            )
            return None, f"delivery to {directory} failed: {exc}"
        logger.info("Delivered the finished video", extra={"destination": str(destination)})
        return str(destination), None

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

    def _effects(
        self,
        context: WorkerContext,
        repository: TimelineRepository,
        timeline: Timeline,
    ) -> dict[EffectEngine, list[EffectInstance]]:
        """The stored effect plan, filtered and split by renderer (§68).

        Until this existed the worker passed a hard-coded empty tuple to the
        overlay and nothing at all to the segment cutter: seventeen planned
        effects across three real projects, none in any finished video. Each
        engine's wire has its own switch, so a misbehaving realiser can be
        turned off without silencing the other.

        Filtering happens here, once, for both engines -- the rules are
        engine-independent and splitting first meant each renderer enforced
        its own subset: the FFmpeg half dropped a disabled clip's zoom while
        the overlay drew the same clip's text_pop at a placeholder position
        over unrelated footage.
        """
        if not context.config.effects.enabled:
            return {}
        realisation = context.config.effects.realisation
        wanted = {
            EffectEngine.FFMPEG: realisation.ffmpeg_filters,
            EffectEngine.REMOTION: realisation.remotion_overlay,
        }
        enabled_clips = {clip.id: clip for clip in timeline.video_clips()}
        stored = repository.list_effects(context.project_id)
        split: dict[EffectEngine, list[EffectInstance]] = {}
        dropped = 0
        for effect in stored:
            if not wanted.get(effect.engine, False):
                continue
            if not self._still_placeable(effect, enabled_clips):
                dropped += 1
                continue
            split.setdefault(effect.engine, []).append(effect)
        if stored:
            logger.info(
                "Loaded the stored effect plan",
                extra={
                    "stored": len(stored),
                    "ffmpeg": len(split.get(EffectEngine.FFMPEG, ())),
                    "remotion": len(split.get(EffectEngine.REMOTION, ())),
                    "dropped": dropped,
                },
            )
        return split

    @staticmethod
    def _still_placeable(
        effect: EffectInstance, enabled_clips: dict[str, Any]
    ) -> bool:
        """Whether a stored row still describes something drawable.

        Three ways a row outlives its plan: its clip was disabled or removed
        (§78 gives the user the last word, and effect rows are independent of
        clip state); an edit trimmed the clip shorter than the effect's
        clip-relative position (§127's save_edit keeps effect rows on
        purpose); or the row predates the planner's content guards and would
        draw a default -- a centred box around nothing, an "x1" tally, an
        empty label. The stored plan is replayed on plain re-renders without
        re-planning, so the reader has to hold the same line the planner does.
        """
        if effect.clip_id:
            clip = enabled_clips.get(effect.clip_id)
            if clip is None:
                return False
            if effect.start_seconds >= clip.duration:
                return False
        params = effect.params
        if effect.effect is EffectType.TEXT_POP and not params.get("text"):
            return False
        if params.get("require_detected_region") and not params.get("region"):
            return False
        count = params.get("count")
        return not params.get("require_event_count") or (
            isinstance(count, (int, float)) and count >= 2
        )

    @staticmethod
    def _by_clip(
        effects: Sequence[EffectInstance], timeline: Timeline
    ) -> dict[str, list[EffectInstance]]:
        """Group the FFmpeg half by the clip whose segment bakes it.

        A timeline-anchored effect (``clip_id`` None) is a documented
        convention this wire cannot serve -- a segment only knows its own
        clip-relative clock -- so it is dropped loudly rather than silently:
        the silent version of this drop is the exact stored-but-unrealised
        defect the wire exists to end. No writer produces such rows today.
        """
        del timeline  # placeability was already enforced in _effects
        grouped: dict[str, list[EffectInstance]] = {}
        for effect in effects:
            if not effect.clip_id:
                logger.warning(
                    "A timeline-anchored effect cannot be baked into a segment",
                    extra={"effect": effect.effect.value, "at": effect.start_seconds},
                )
                continue
            grouped.setdefault(effect.clip_id, []).append(effect)
        return grouped

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
        effects: Sequence[EffectInstance] = (),
    ):
        """Render the caption and graphics layer, or skip it (§66, D-008)."""
        captions = repository.list_captions(context.project_id)
        remotion_config = context.config.remotion
        overlay_fps = remotion_config.overlay_fps or target.fps
        composition = build_composition(
            timeline,
            captions=captions,
            effects=effects,
            caption_config=context.config.captions,
            width=target.width,
            height=target.height,
            fps=overlay_fps,
        )
        # The overlay must be exactly as long as the picture it sits on, and
        # the picture's measured length is the only number that is certainly
        # right (a frame per cut can differ from the sum of the spans).
        composition = _resized(composition, duration_seconds, overlay_fps)

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
        music = audio_mix.find_music(
            context.paths.assets / "music",
            mean_intensity=self._mean_intensity(context),
        )
        return audio_mix.plan_mix(
            game=game_audio,
            microphone=None,
            music=music,
            music_envelope=music_envelope,
            game_envelope=game_envelope,
            config=config,
            duration_seconds=duration_seconds,
            stingers=self._stingers(context, timeline, duration_seconds),
        )

    def _stingers(
        self, context: WorkerContext, timeline: Timeline, duration_seconds: float
    ) -> list[audio_mix.Stinger]:
        """Sound effects the planner placed, resolved to local files (§68, §73).

        ``sound_effect`` has been in the library with triggers and budgets
        since Phase 1, and nothing read the rows: an effect the planner
        carefully rationed and then discarded. This is the reader.

        Timing follows the same rule as every other effect row: times are
        stored relative to the clip that carries them, so the clip's position
        on the finished timeline is what puts the sound where it belongs.

        A row naming an asset the project does not have is skipped rather than
        substituted -- §73's "local files only" is about consent, and quietly
        playing a different sound is the wrong way to be helpful.

        The switches govern this wire too: ``sound_effect`` is an
        FFmpeg-engine effect, and "effects off" that still plays planned
        stingers is a contract nobody can verify by listening.
        """
        if not (
            context.config.effects.enabled
            and context.config.effects.realisation.ffmpeg_filters
        ):
            return []
        rows = context.database.fetch_all(
            "SELECT clip_id, start_seconds, parameters FROM timeline_effects "
            "WHERE project_id = ? AND effect_type = 'sound_effect' AND enabled = 1",
            (context.project_id,),
        )
        if not rows:
            return []

        by_id = {clip.id: clip for clip in timeline.video_clips()}
        assets = context.paths.assets / "sfx"
        stingers: list[audio_mix.Stinger] = []
        for row in rows:
            params = json.loads(row["parameters"]) if row["parameters"] else {}
            name = params.get("asset")
            if not name:
                continue
            asset = sfx.resolve_stinger_asset(
                str(name), assets, context.data_root, context.ffmpeg
            )
            if asset is None:
                logger.warning(
                    "A planned sound effect has no local asset and no recipe",
                    extra={"asset": str(name), "project_id": context.project_id},
                )
                continue
            clip = by_id.get(row["clip_id"] or "")
            if clip is None:
                at = float(row["start_seconds"])
            else:
                # Through the clip's warp mapping, not a straight addition: a
                # stinger anchored after a freeze point belongs after the
                # hold, where the frame it decorates is actually seen.
                at = clip.timeline_start + retime.output_offset(
                    clip, float(row["start_seconds"])
                )
            if at >= duration_seconds:
                continue
            stingers.append(
                audio_mix.Stinger(
                    path=asset,
                    # A riser belongs *before* its peak; lead_seconds is the
                    # doctrine's silence-then-payoff shape (§14).
                    at_seconds=max(0.0, at - float(params.get("lead_seconds", 0.0))),
                    gain_db=float(params.get("gain_db", -6.0)),
                )
            )
        stingers.extend(self._transition_whooshes(context, timeline, duration_seconds))
        return stingers

    def _mean_intensity(self, context) -> float | None:
        """The story's own pacing measurement, for the music shelf (§15)."""
        try:
            pacing = context.stage_result(JobStage.STORY).get("pacing")
            value = (pacing or {}).get("mean_intensity")
            return float(value) if value is not None else None
        except Exception:
            return None

    def _transition_whooshes(self, context, timeline, duration_seconds) -> list:
        """§14's transition sound, placed where the picture already dips.

        The time-jump grammar marks its joins with fade metadata; a whoosh at
        each marked join is the audio half of the same sentence, from the
        same evidence, with nothing new decided here.
        """
        placed = []
        try:
            whoosh = sfx.resolve_stinger_asset(
                "whoosh.wav", context.paths.assets, context.data_root, context.ffmpeg
            )
            if whoosh is None:
                return []
            for clip in timeline.video_clips():
                fade = float(clip.metadata.get("fade_in_seconds") or 0.0)
                medium = clip.metadata.get("time_jump") == "medium"
                if fade <= 0.0 and not medium:
                    continue
                at = max(0.0, clip.timeline_start - 0.35)
                if at >= duration_seconds:
                    continue
                placed.append(
                    audio_mix.Stinger(path=whoosh, at_seconds=at, gain_db=-10.0)
                )
        except Exception:
            logger.exception("Transition whooshes unavailable; the dips stay silent")
        return placed

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

        offset = self._jl_audio(context, timeline, clips, sources, work_dir)
        if offset is not None:
            return offset

        inputs: list[str] = []
        chains: list[str] = []
        labels: list[str] = []
        # One -i per unique source, not per clip: a jump-cut edit holds
        # hundreds of pieces of the same recording, and repeating the input
        # once per piece both blew the Windows 32K argv ceiling (WinError
        # 206) and asked FFmpeg to open the same file hundreds of times.
        index_by_media: dict[str, int] = {}
        for position, clip in enumerate(clips):
            source = sources[clip.media_id]
            if clip.media_id not in index_by_media:
                index_by_media[clip.media_id] = len(index_by_media)
                inputs += ["-i", str(source)]
            source_index = index_by_media[clip.media_id]
            warp = retime.clip_retime(clip)
            if warp is None:
                chains.append(
                    _audio_span_filter(
                        source_index,
                        clip.source_in,
                        clip.source_out,
                        out_label=f"a{position}",
                    )
                )
            else:
                # A re-laid clip's audio must occupy its re-laid seconds:
                # silence under a freeze, a pitch-preserving stretch under a
                # ramp -- the same shape the picture's segment carries, or
                # every later clip's sound lands early by the added time.
                fade = min(_JOIN_FADE_SECONDS, clip.duration / 4)
                chains.append(
                    audio_mix.warped_clip_audio(
                        source_index,
                        clip,
                        warp,
                        out_label=f"a{position}",
                        fade_in=fade,
                        fade_out=fade,
                    )
                )
            labels.append(f"[a{position}]")

        destination = work_dir / "programme_audio.wav"
        graph = ";".join(
            [*chains, f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[aout]"]
        )
        # The graph grows with the clip count and Windows caps an argv near
        # 32K; a script file has no ceiling.
        graph_path = work_dir / "programme_audio.filters"
        graph_path.write_text(graph, encoding="utf-8")
        context.ffmpeg.run(
            [
                *context.ffmpeg.base_arguments(),
                *inputs,
                "-filter_complex_script", str(graph_path),
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

    def _jl_audio(
        self,
        context: WorkerContext,
        timeline: Timeline,
        clips: Sequence[TimelineClip],
        sources: dict[str, Path],
        work_dir: Path,
    ) -> Path | None:
        """The gameplay track with J/L offsets, or ``None`` for the plain path.

        ``None`` is the common answer and costs nothing: with the feature off
        (its shipped default) or with no boundary earning an offset, this
        neither reads the transcript nor runs FFmpeg, and the concat path
        behaves byte-for-byte as it always has.
        """
        jl_config = context.config.render.jl_cuts
        if not jl_config.enabled or len(clips) < 2:
            return None

        repository = TranscriptRepository(context.database)
        transcript_by_media = {
            media_id: [
                (segment.start, segment.end)
                for segment in repository.list_for_media(media_id)
            ]
            for media_id in timeline.media_ids()
        }
        durations = {
            item.id: float(item.metadata.duration_seconds)
            for item in MediaRepository(context.database).list_for_project(
                context.project_id
            )
            if item.metadata.duration_seconds
        }
        boundaries = jl.plan_boundaries(
            timeline, transcript_by_media, jl_config, source_durations=durations
        )
        if all(plan.is_hard for plan in boundaries):
            return None

        destination = work_dir / "programme_audio.wav"
        argv = jl.assembly_arguments(
            clips,
            boundaries,
            sources=sources,
            destination=destination,
            config=jl_config,
        )
        logger.info(
            "Assembling the gameplay audio with J/L offsets at the cuts",
            extra={
                "boundaries": len(boundaries),
                "j_cuts": sum(1 for plan in boundaries if plan.kind == "j"),
                "l_cuts": sum(1 for plan in boundaries if plan.kind == "l"),
            },
        )
        context.ffmpeg.run(
            [*context.ffmpeg.base_arguments(), *argv],
            timeout_seconds=context.config.ffmpeg.timeout_seconds,
            error_code=ErrorCode.AUDIO_MIX_FAILED,
            error_type=RenderError,
            error_message="Could not assemble the programme audio with J/L cuts.",
            details={"clips": len(clips)},
        )
        return destination


#: Fade length at each cut boundary. Thirty milliseconds is below anything a
#: listener registers as a fade and above anything that still clicks.
_JOIN_FADE_SECONDS = 0.03


def _audio_span_filter(
    input_index: int, source_in: float, source_out: float, *, out_label: str
) -> str:
    """One clip's audio chain: trim, restamp, format -- and defuse the joins.

    ``atrim`` cuts the waveform at whatever sample value the boundary lands
    on, and a jump from that value to the next clip's is audible as a click or
    pop at every join. A micro fade at each end takes the boundary through
    zero. Spans too short to hold two fades get proportionally shorter ones
    rather than none: the shorter the clip, the more joins per second it
    contributes.
    """
    duration = max(source_out - source_in, 0.0)
    fade = min(_JOIN_FADE_SECONDS, duration / 4) if duration > 0 else 0.0
    fades = ""
    if fade > 0:
        fades = (
            f",afade=t=in:st=0:d={fade:.3f}"
            f",afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}"
        )
    return (
        f"[{input_index}:a:0]atrim=start={source_in:.6f}:end={source_out:.6f},"
        f"asetpts=N/SR/TB,{audio_mix.MIX_FORMAT}{fades}[{out_label}]"
    )


def _resized(composition, duration_seconds: float, fps: int):
    """Return the composition stretched or trimmed to the measured duration."""
    from dataclasses import replace

    frames = max(1, round(duration_seconds * fps))
    return replace(composition, duration_in_frames=frames)


def _repo_root() -> Path:
    from backend.config.paths import find_repository_root

    return find_repository_root()


__all__ = ["RenderWorker"]
