"""The STORY stage (SPEC sections 35-39).

The first stage that reasons across every recording at once. Moments belong to
a file; a video does not — a session recorded in three parts is one video, and
the duration target applies to all of it together.

The stage produces a **plan**, not a timeline: an ordered selection with a hook
and a pacing report. Phase 8 turns it into an EDL. Keeping them apart is what
makes §127's re-edit cheap — changing the target duration re-runs this stage
against stored moments in milliseconds, and never re-analyses the source.

The plan is stored on the job row rather than in a table of its own, because
that is what job results are for (§81) and the next stage reads it the same way
every other stage reads its input.
"""

from __future__ import annotations

from typing import Any

from backend.core.errors import ErrorCode, NarrativeError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, VideoMode
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.projects import ProjectRepository
from backend.moments.formation import Moment
from backend.narrative.story import NarrativePlan, build_plan
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.workers.story", LogChannel.PIPELINE)


class StoryWorker:
    """STORY -- choose and order the clips that make the video (§35-§39)."""

    stage = JobStage.STORY

    def run(self, context: WorkerContext) -> dict[str, Any]:
        project = ProjectRepository(context.database).require(context.project_id)
        moments = self._moments(context)

        if not moments:
            # No moments is not a crash, but it is a dead end: there is nothing
            # to make a video out of, and the next stage must not be told there
            # is. §95 degrades; it does not invent.
            context.report(1.0, "No moments to build an edit from")
            return {
                "skipped": True,
                "reason": "no moments available",
                # A list, not a count. The EDL stage reads this key as the
                # clips it must lay out, and a key whose type depends on which
                # branch produced it is not a contract (§81).
                "clips": [],
                "within_target": False,
            }

        target = float(project.target_duration_seconds)
        mode = project.mode if isinstance(project.mode, VideoMode) else VideoMode(project.mode)

        context.report(0.4, f"Selecting clips for a {target / 60:.0f}-minute {mode.value} edit")
        plan = build_plan(
            moments,
            mode=mode,
            target_seconds=target,
            config=context.config.narrative,
            policy=context.config.duration_policy,
        )

        if plan.is_empty:
            raise NarrativeError(
                "The optimiser could not assemble any edit from the available moments.",
                code=ErrorCode.NARRATIVE_FAILED,
                details={"moments": len(moments), "target_seconds": target},
            )

        if not plan.within_target:
            # Reported, not hidden. §39's tolerance is the product's own
            # definition of close enough, and missing it is something the user
            # should be told rather than something the EDL silently clamps.
            logger.warning(
                "The edit does not land inside the duration tolerance",
                extra={
                    "project_id": context.project_id,
                    "target_seconds": target,
                    "actual_seconds": round(plan.total_seconds, 2),
                    "notes": list(plan.notes),
                },
            )

        context.report(1.0, f"{len(plan.moments)} clips selected")
        return {**_serialise(plan), "moments_considered": len(moments)}

    def _moments(self, context: WorkerContext) -> list[Moment]:
        """Every scored moment in the project, across all its recordings."""
        repository = MomentRepository(context.database)
        media = MediaRepository(context.database).list_for_project(context.project_id)
        collected: list[Moment] = []
        for item in media:
            collected.extend(repository.list_for_media(item.id))
        return collected


def _serialise(plan: NarrativePlan) -> dict[str, Any]:
    """The plan as the job result the EDL stage will read (§81).

    Clips carry the identifiers and the source span, which is everything a
    non-destructive timeline needs (§42): the EDL references the original
    recording, it never copies frames.
    """
    return {
        **plan.summary(),
        "clips": [
            {
                "index": index,
                "media_id": moment.media_id,
                "moment_id": moment.metadata.get("id"),
                "moment_type": moment.moment_type.value,
                "source_start": round(moment.context_start, 3),
                "source_end": round(moment.context_end, 3),
                "seconds": round(moment.context_duration, 3),
                "score": round(moment.score, 4),
                "role": moment.metadata.get("role", "body"),
                "beat": plan.beats[index] if index < len(plan.beats) else None,
            }
            for index, moment in enumerate(plan.moments)
        ],
    }


__all__ = ["StoryWorker"]
