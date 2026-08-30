"""The SEMANTIC stage — build the session's meaning once, for everyone after it.

Before this stage existed the lanes were built inside the EDL worker, which
meant they arrived *after* the optimiser had already chosen which moments the
video was made of. The pacing engine then cut those moments at exactly the
right speed. Cutting the wrong moment at the right speed is still the wrong
moment, and nothing upstream could have known.

So it moves here, between GAME_EVENTS and MOMENTS: everything the lanes are
built from is stored by then, and everything that decides anything comes
after. One build, one row, six readers.

Nothing is detected here. The lanes are a fusion of evidence that already
exists, which is why the stage is cheap enough to run on every project and
deterministic enough to be worth storing (§48's principle without §48's
machinery: the digest is over the values, so an unchanged recording rebuilds
to the same lanes).
"""

from __future__ import annotations

from typing import Any

from backend.analysis import motion
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.database.repositories.media import MediaRepository
from backend.pipeline.workers.base import WorkerContext
from backend.semantic.reader import LANES
from backend.semantic.timeline import load_timeline

logger = get_logger("pipeline.workers.semantic", LogChannel.PIPELINE)


class SemanticWorker:
    """SEMANTIC -- the session's lanes, stored for every stage that follows."""

    stage = JobStage.SEMANTIC

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = MediaRepository(context.database).list_for_project(context.project_id)
        if not media:
            # No recording is not a failure here; the stages that need lanes
            # will find none and fall back to their pre-V2 behaviour (§95).
            context.report(1.0, "No recordings to read")
            return {"skipped": True, "reason": "no media", "timelines": []}

        built: list[dict[str, Any]] = []
        for index, item in enumerate(media):
            duration = item.metadata.duration_seconds
            if not duration:
                logger.info(
                    "A recording with no measured duration has no timeline",
                    extra={"media_id": item.id},
                )
                continue
            context.report(
                (index + 1) / (len(media) + 1),
                f"Reading the shape of {item.filename}",
            )
            # Motion is the heaviest term in the fusion and its column had
            # never been written -- seventeen thousand frames, no scores. It
            # is measured here rather than at extraction so a recording
            # analysed before the measurement existed repairs itself.
            scored = motion.score_media(context.database, item.id)
            timeline = load_timeline(
                context.database,
                item.id,
                duration_seconds=float(duration),
                config=context.config,
            )
            shape = timeline.shape()
            built.append(
                {
                    "media_id": item.id,
                    "hz": timeline.hz,
                    "duration_seconds": round(timeline.duration_s, 3),
                    "lanes": sorted(timeline.lanes),
                    "segments": len(shape),
                    "frames_scored": scored,
                    # §80: the session's own form, in the job result, so the
                    # shape a video was cut to is inspectable afterwards.
                    "shape": timeline.summary(),
                }
            )

        context.report(1.0, f"{len(built)} recording(s) read")
        return {
            "timelines": built,
            "lanes": list(LANES),
            "media_analysed": len(built),
        }


__all__ = ["SemanticWorker"]
