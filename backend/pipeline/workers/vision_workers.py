"""The visual stages: SCENES and VISION.

SPEC sections 17 (scene detection), 15 and 16 (the cascade), 56 (preview
frames), 54 (one model in VRAM at a time), 95 (graceful degradation).

The VISION stage is where a naive implementation turns a two-hour recording
into an afternoon of GPU time. It does not send frames to the model; it asks
:mod:`backend.analysis.candidates` which frames are worth sending, and that
answer is bounded by ``analysis.vision.max_frames_per_source_hour`` no matter
how eventful the recording is.

Everything the cascade reads has already been computed by an earlier stage:
audio events, the transcript, scene boundaries. Nominating costs a database
read, not a decode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from ai.providers.base import VisionProvider
from ai.vision import create_vision_provider
from backend.analysis.candidates import (
    CandidatePlan,
    Trigger,
    build_candidates,
    frame_difference_scores,
    triggers_from_audio,
    triggers_from_frame_difference,
    triggers_from_scenes,
    triggers_from_transcript,
)
from backend.analysis.scenes import SceneResult, detect_scenes
from backend.core.errors import GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.core.models.media import Media
from backend.core.prompts import load_prompt
from backend.core.versions import PROMPT_VERSIONS
from backend.database.repositories.audio_events import AudioEventRepository
from backend.database.repositories.frames import FrameRepository
from backend.database.repositories.scenes import SceneRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.database.repositories.vision import VisionRepository, observations_from
from backend.media.frames import extract_at_times
from backend.media.probe import probe_media
from backend.pipeline.reuse import record_success, try_reuse
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.workers.vision", LogChannel.PIPELINE)

#: The prompt this stage uses. Registered in §92's registry, versioned on disk.
VISION_PROMPT_ID: Final[str] = "vision.frame_description"

#: Sampling level recorded for keyframes the model saw, so the §16 hierarchy is
#: visible in the database rather than implied.
CANDIDATE_LEVEL: Final[str] = "candidate"


class SceneWorker:
    """SCENES -- shot boundaries and one preview frame each (§17, §56).

    Runs on the proxy. Boundaries do not become more accurate at source
    resolution, and the proxy is a quarter of the pixels.
    """

    stage = JobStage.SCENES

    def run(self, context: WorkerContext) -> dict[str, Any]:
        reused = try_reuse(context, self.stage)
        if reused is not None:
            context.report(1.0, "Reused matching analysis from another project")
            return reused
        media = context.require_media()
        source, from_proxy = _visual_source(context, media)
        probe = probe_media(source, context.ffmpeg, require_video=True)

        result = detect_scenes(
            source,
            context.config.analysis.scenes,
            duration_seconds=probe.duration_seconds,
            on_progress=context.report,
            should_cancel=context.should_cancel,
        )

        keyframes = self._keyframes(context, media, source, result)
        repository = SceneRepository(context.database)
        with context.database.transaction():
            stored = repository.replace_for_media(
                context.project_id, media.id, result.scenes, keyframes=keyframes
            )

        record_success(context, self.stage)
        context.report(1.0, f"{stored} scenes")
        return {
            "scenes": stored,
            "from_proxy": from_proxy,
            "keyframes": len(keyframes),
            "detector": result.detector,
            "threshold": result.threshold,
            **result.summary(),
        }

    def _keyframes(
        self, context: WorkerContext, media: Media, source: Path, result: SceneResult
    ) -> dict[int, str]:
        """Extract one preview per scene (§56).

        Best effort. A missing thumbnail costs the review screen a picture; a
        failed stage would cost the analysis, and the boundaries are the part
        that matters.
        """
        thumbnails = context.config.thumbnails
        if not thumbnails.enabled or not result.scenes:
            return {}

        directory = context.paths.previews / media.id / "scenes"
        try:
            frames = extract_at_times(
                source,
                directory,
                result.keyframe_times(),
                context.ffmpeg,
                level="base",
                jpeg_quality=thumbnails.jpeg_quality,
                width=thumbnails.width,
                prefix="scene",
                should_cancel=context.should_cancel,
            )
        except GamingEditorError as exc:
            logger.warning(
                "Scene keyframe extraction failed; continuing without previews",
                extra={"media_id": media.id, "error_code": exc.code},
            )
            return {}

        by_time = {round(frame.timestamp, 3): str(frame.path) for frame in frames}
        return {
            scene.index: by_time[round(scene.midpoint, 3)]
            for scene in result.scenes
            if round(scene.midpoint, 3) in by_time
        }


class VisionWorker:
    """VISION -- describe the frames the cascade nominated (§15, §16).

    The stage is deliberately shaped as *plan, then execute*. The plan is built
    and logged before a single model call, so what the model will be asked to
    look at -- and what it will not -- is inspectable rather than emergent.
    """

    stage = JobStage.VISION

    def __init__(self, provider: VisionProvider | None = None) -> None:
        """
        Args:
            provider: injected for tests. Production builds it from
                ``config/models.yaml`` at the start of the stage (§13).
        """
        self._provider = provider

    def run(self, context: WorkerContext) -> dict[str, Any]:
        reused = try_reuse(context, self.stage)
        if reused is not None:
            context.report(1.0, "Reused matching analysis from another project")
            return reused
        media = context.require_media()
        analysis = context.config.analysis
        if not analysis.vision.enabled:
            context.report(1.0, "Vision disabled")
            return {"skipped": True, "reason": "vision disabled in configuration"}

        source, _ = _visual_source(context, media)
        probe = probe_media(source, context.ffmpeg, require_video=True)
        plan = self._plan(context, media, probe.duration_seconds)

        if not plan.analysed_regions:
            context.report(1.0, "No candidate regions")
            return {"skipped": True, "reason": "no candidate regions", **plan.summary()}

        provider = self._provider or create_vision_provider(
            context.config, game=_project_game(context)
        )
        if not provider.is_available():
            # §95: vision failing degrades to OCR, audio, scenes and the game
            # profile. It does not fail the analysis.
            context.report(1.0, "Vision model unavailable")
            logger.warning(
                "Vision provider unavailable; continuing without frame descriptions",
                extra={"media_id": media.id, "model": provider.info().name},
            )
            return {
                "skipped": True,
                "reason": "vision provider unavailable",
                "model": provider.info().name,
                **plan.summary(),
            }

        prompt_version = PROMPT_VERSIONS.get(VISION_PROMPT_ID)
        # Loaded before the model: a malformed prompt should fail in
        # milliseconds, not after six gigabytes have been paged into VRAM.
        load_prompt(VISION_PROMPT_ID)

        observations = self._describe(context, media, source, plan, provider)

        repository = VisionRepository(context.database)
        with context.database.transaction():
            stored = repository.replace_for_media(context.project_id, media.id, observations)

        record_success(context, self.stage)
        context.report(1.0, f"{stored} frames described")
        info = provider.info()
        return {
            "observations": stored,
            "model": info.name,
            "model_version": info.version,
            "prompt_id": VISION_PROMPT_ID,
            "prompt_version": prompt_version,
            "labels": repository.label_counts(media.id),
            **plan.summary(),
        }

    # -- the cascade ----------------------------------------------------

    def _plan(self, context: WorkerContext, media: Media, duration: float) -> CandidatePlan:
        """Ask every cheap detector where to look (§16)."""
        analysis = context.config.analysis
        triggers: list[Trigger] = []

        audio = AudioEventRepository(context.database).list_for_media(media.id)
        triggers += triggers_from_audio(audio, analysis)

        scenes = SceneRepository(context.database).list_for_media(media.id)
        if scenes:
            triggers += triggers_from_scenes(
                SceneResult(
                    scenes=tuple(scenes),
                    duration_seconds=duration,
                    detector=analysis.scenes.detector,
                    threshold=analysis.scenes.threshold,
                ),
                analysis,
            )

        transcript = TranscriptRepository(context.database).list_for_media(media.id)
        triggers += triggers_from_transcript(transcript, analysis)

        sampled = [
            (row.timestamp, Path(row.image_path))
            for row in FrameRepository(context.database).list_for_media(media.id, level="base")
        ]
        if sampled:
            triggers += triggers_from_frame_difference(frame_difference_scores(sampled), analysis)

        plan = build_candidates(triggers, analysis, duration_seconds=duration)
        logger.info(
            "Vision cascade planned",
            extra={
                "media_id": media.id,
                "triggers": len(triggers),
                "audio_events": len(audio),
                "scenes": len(scenes),
                "transcript_segments": len(transcript),
                "sampled_frames": len(sampled),
                **plan.summary(),
            },
        )
        return plan

    def _describe(
        self,
        context: WorkerContext,
        media: Media,
        source: Path,
        plan: CandidatePlan,
        provider: VisionProvider,
    ):
        """Extract the planned keyframes and hand them to the model in batches."""
        directory = context.paths.frames / media.id / CANDIDATE_LEVEL
        batch_size = max(context.config.analysis.vision.max_frames_per_request, 1)
        minimum = context.config.analysis.vision.min_confidence
        regions = plan.analysed_regions
        info = provider.info()
        prompt_version = PROMPT_VERSIONS.get(VISION_PROMPT_ID)

        stored = []
        frame_repository = FrameRepository(context.database)
        try:
            provider.load()
            for index, region in enumerate(regions):
                if context.should_cancel():
                    from backend.media.ffmpeg import CancelledError

                    raise CancelledError(details={"stage": "vision", "region": index})

                context.report(
                    index / max(len(regions), 1),
                    f"Describing region {index + 1}/{len(regions)}",
                )
                frames = extract_at_times(
                    source,
                    directory,
                    region.keyframes,
                    context.ffmpeg,
                    level=CANDIDATE_LEVEL,
                    jpeg_quality=context.config.analysis.frame_sampling.jpeg_quality,
                    should_cancel=context.should_cancel,
                )
                if not frames:
                    continue
                with context.database.transaction():
                    frame_repository.add_many(context.project_id, media.id, frames)

                for start in range(0, len(frames), batch_size):
                    batch = frames[start : start + batch_size]
                    observations = provider.describe(
                        tuple(frame.path for frame in batch),
                        tuple(frame.timestamp for frame in batch),
                        prompt_id=VISION_PROMPT_ID,
                    )
                    stored.extend(
                        observations_from(
                            # §94: below the configured confidence the model is
                            # telling us it does not know, and an uncertain
                            # description carried forward is one that will be
                            # believed by whatever reads it next.
                            [item for item in observations if item.confidence >= minimum],
                            info=info,
                            prompt_id=VISION_PROMPT_ID,
                            prompt_version=prompt_version,
                            region_start=region.start_seconds,
                            region_end=region.end_seconds,
                            sources=sorted(region.sources),
                        )
                    )
        finally:
            provider.unload()
        return stored


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _visual_source(context: WorkerContext, media: Media) -> tuple[Path, bool]:
    """Return the file the visual stages read, and whether it is the proxy."""
    proxy = context.paths.proxy / f"{Path(media.filename).stem}_proxy.mp4"
    if proxy.is_file() and proxy.stat().st_size > 0:
        return proxy, True
    return context.source_path(), False


def _project_game(context: WorkerContext) -> str:
    """The project's game profile id, for the prompt (§22, §23)."""
    row = context.database.fetch_one(
        "SELECT game FROM projects WHERE id = ?", (context.project_id,)
    )
    return str(row["game"]) if row is not None and row["game"] else "auto"


__all__ = ["CANDIDATE_LEVEL", "VISION_PROMPT_ID", "SceneWorker", "VisionWorker"]
