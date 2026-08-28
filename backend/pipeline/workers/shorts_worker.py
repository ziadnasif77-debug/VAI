"""The SHORTS stage — the strongest moments as vertical cuts (§35, §51).

Manual like every delivery: nothing is cut until the person asks. What runs
when they do is the long-form stack at a different frame shape — the moments
already scored, the transcript already written, the caption engine and the
Remotion overlay already built. This worker's own contribution is sequencing:

    plan -> NVENC cut per moment -> captions on a one-clip timeline
         -> overlay at 1080x1920 -> merge, keeping the cut's own audio

Captions degrade the way everything §95 touches degrades: a Short whose
overlay cannot be produced ships as the plain cut with a note, because a
finished vertical clip without captions is a product and a failure message is
not.
"""

from __future__ import annotations

import pathlib
from typing import Any

from backend.core.errors import ErrorCode, RenderError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.projects import ProjectRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.pipeline.workers.base import WorkerContext
from backend.rendering import shorts as vertical
from backend.rendering.composition import build_composition
from backend.rendering.encoder import select_encoder
from backend.rendering.remotion import render_overlay
from backend.timeline import captions as caption_builder

logger = get_logger("pipeline.workers.shorts", LogChannel.RENDERING)


class ShortsWorker:
    """SHORTS — N vertical cuts from one analysis."""

    stage = JobStage.SHORTS

    def run(self, context: WorkerContext) -> dict[str, Any]:
        config = context.config.shorts
        if not config.enabled:
            context.report(1.0, "Shorts are switched off")
            return {"skipped": True, "reason": "shorts disabled in configuration", "shorts": []}

        moments = self._moments(context)
        plans = vertical.plan_shorts(moments, config)
        if not plans:
            context.report(1.0, "No moment is strong enough for a Short")
            return {
                "skipped": True,
                "reason": "no usable moments",
                "moments_considered": len(moments),
                "shorts": [],
            }

        project = ProjectRepository(context.database).require(context.project_id)
        media = MediaRepository(context.database)
        encoder = select_encoder(context.config.render, context.ffmpeg)
        destination_dir = context.paths.renders / "shorts"
        destination_dir.mkdir(parents=True, exist_ok=True)

        produced: list[dict[str, Any]] = []
        for position, plan in enumerate(plans):
            if context.should_cancel():
                raise RenderError(
                    "Shorts generation was cancelled.",
                    code=ErrorCode.JOB_CANCELLED,
                    details={"completed": len(produced)},
                )
            context.report(
                position / len(plans),
                f"Cutting Short {position + 1} of {len(plans)}",
            )
            record = media.require(plan.media_id)
            source = pathlib.Path(record.source_path)
            if not source.is_file():
                raise RenderError(
                    f"The source recording is no longer at {source}.",
                    code=ErrorCode.MEDIA_NOT_FOUND,
                    details={"media_id": plan.media_id},
                    recoverable=False,
                )
            source_meta = record.metadata
            out_path = destination_dir / vertical.filename_for(plan, project.name)

            cut = destination_dir / f".cut-{plan.index:03d}.mp4"
            argv = vertical.cut_arguments(
                plan,
                source=source,
                destination=cut,
                config=config,
                encoder=encoder,
                render_config=context.config.render,
                source_width=source_meta.width or 1920,
                source_height=source_meta.height or 1080,
                fps=round(source_meta.fps or 60),
            )
            context.ffmpeg.run(
                [*context.ffmpeg.base_arguments(), *argv],
                timeout_seconds=context.config.ffmpeg.timeout_seconds,
                error_code=ErrorCode.RENDER_FAILED,
                error_type=RenderError,
                error_message=f"Cutting Short {plan.index + 1} failed.",
            )

            note = self._burn_captions(context, plan, cut, out_path, config, project)
            if cut.exists() and out_path.exists() and cut != out_path:
                cut.unlink(missing_ok=True)

            produced.append(
                {
                    **plan.summary(),
                    "output_path": str(out_path),
                    "size_bytes": out_path.stat().st_size if out_path.exists() else 0,
                    **({"note": note} if note else {}),
                }
            )

        context.report(1.0, f"{len(produced)} Shorts in {destination_dir.name}/")
        return {
            "shorts": produced,
            "directory": str(destination_dir),
            "moments_considered": len(moments),
            "frame": f"{config.width}x{config.height}",
        }

    # -- internals ------------------------------------------------------

    def _moments(self, context: WorkerContext) -> list[Any]:
        repository = MomentRepository(context.database)
        media = MediaRepository(context.database).list_for_project(context.project_id)
        collected: list[Any] = []
        for item in media:
            collected.extend(repository.list_for_media(item.id))
        return collected

    def _burn_captions(
        self, context: WorkerContext, plan, cut, out_path, config, project
    ) -> str | None:
        """Captions through the long-form engine, or the plain cut with a note.

        The project's own captions choice governs Shorts too: a person who
        asked for no text inside the video meant the vertical cuts as well.
        """
        if not config.captions or not project.captions_enabled:
            cut.replace(out_path)
            return None

        segments = TranscriptRepository(context.database).list_for_media(plan.media_id)
        timeline = vertical.short_timeline(plan, context.project_id)
        captions = caption_builder.build_captions(
            timeline, {plan.media_id: segments}, context.config.captions
        )
        if not captions:
            cut.replace(out_path)
            return None

        composition = build_composition(
            timeline,
            captions=captions,
            caption_config=context.config.captions,
            width=config.width,
            height=config.height,
            fps=context.config.remotion.overlay_fps or 30,
        )
        overlay = render_overlay(
            composition,
            output_path=cut.with_suffix(".overlay.webm"),
            config=context.config.remotion,
            repo_root=_repo_root(),
        )
        if not overlay.exists:
            # §95: the cut is a finished Short; the missing layer is a note.
            cut.replace(out_path)
            return f"captions skipped: {overlay.reason}"

        merge = vertical.overlay_merge_arguments(
            cut, overlay.path, out_path, overlay_format=context.config.remotion.overlay_format
        )
        context.ffmpeg.run(
            [*context.ffmpeg.base_arguments(), *merge],
            timeout_seconds=context.config.ffmpeg.timeout_seconds,
            error_code=ErrorCode.RENDER_FAILED,
            error_type=RenderError,
            error_message=f"Merging captions into Short {plan.index + 1} failed.",
        )
        overlay.path.unlink(missing_ok=True)
        return None


def _repo_root():
    from backend.config.paths import find_repository_root

    return find_repository_root()


__all__ = ["ShortsWorker"]
