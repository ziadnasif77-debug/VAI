"""The EDL stage (SPEC §40–§45, §71).

Where the plan becomes an edit. The narrative stage chose moments and put them
in an order; this puts them on a timeline, times the captions against it, plans
the effects for it, validates the result and stores it.

The order matters and is not arbitrary:

    build → validate → captions → effects → persist

Captions come after validation because they are timed against clip positions,
and timing them against a timeline that turns out to be invalid means doing it
twice. Effects come after captions because the effects planner is given the
finished clip positions and a budget measured in effects per minute — it needs
the final duration, which only exists once the timeline is built.

Nothing here decodes video. Every input is a database read and the output is
rows, which is what makes §127's re-edit cheap: changing a clip re-runs this
stage in milliseconds and never re-analyses the source.
"""

from __future__ import annotations

from typing import Any

from ai.providers.base import TranscriptSegment
from backend.core.errors import ErrorCode, ValidationError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import EffectEngine, JobStage
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.effects.models import EffectPlan
from backend.effects.planner import EffectPlanner, PlannedMoment
from backend.interaction.service import InteractionService
from backend.pipeline.workers.base import WorkerContext
from backend.timeline import captions as caption_builder
from backend.timeline import validation
from backend.timeline.builder import build_timeline, clips_from_story_result
from backend.timeline.models import Timeline

logger = get_logger("pipeline.workers.edl", LogChannel.PIPELINE)


class EdlWorker:
    """EDL -- the non-destructive description of the finished video (§40-§42)."""

    stage = JobStage.EDL

    def run(self, context: WorkerContext) -> dict[str, Any]:
        story = self._story_result(context)
        planned = clips_from_story_result(story)
        if not planned:
            # No plan is a dead end, not a crash: the STORY stage already said
            # why, and inventing a timeline here would hide it (§95).
            context.report(1.0, "No narrative plan to build a timeline from")
            return {
                "skipped": True,
                "reason": story.get("reason", "the narrative stage selected no clips"),
                "clips": 0,
            }

        durations = self._durations(context)
        policy = context.config.output.duration_policy()

        context.report(0.2, f"Laying out {len(planned)} clips")
        built = build_timeline(
            planned,
            project_id=context.project_id,
            policy=policy,
            target_seconds=float(story.get("target_seconds", 0.0)),
            media_durations=durations,
            transitions=context.config.narrative.transitions,
            notes=story.get("notes", ()),
            metadata={
                "mode": story.get("mode"),
                "within_target": story.get("within_target"),
                "hook": story.get("hook"),
            },
        )
        timeline = built.timeline

        context.report(0.4, "Checking the timeline")
        report = validation.validate(timeline, media_durations=durations, policy=policy)
        if not report.is_valid:
            # Raised rather than stored: a timeline with a gap or an
            # out-of-range seek is not something a later stage can work around,
            # and rendering it would fail with a far less useful message (§81).
            raise ValidationError(
                "The timeline is not renderable: "
                + "; ".join(str(item) for item in report.errors),
                code=ErrorCode.INVALID_EDL,
                details={"findings": [str(item) for item in report.findings]},
            )

        context.report(0.6, "Timing captions against the transcript")
        built_captions = self._captions(context, timeline)
        timeline = caption_builder.caption_track(timeline, built_captions)

        context.report(0.8, "Planning effects")
        effects = self._effects(context, timeline)

        written = TimelineRepository(context.database).replace(
            context.project_id, timeline, captions=built_captions, effects=effects.instances
        )
        context.report(1.0, f"{written} clips on the timeline")

        if built.warnings:
            logger.warning(
                "The timeline was built with warnings",
                extra={"project_id": context.project_id, "warnings": list(built.warnings)},
            )
        return {
            **timeline.summary(),
            "captions": len(built_captions),
            "effects": effects.count,
            "effects_by_engine": {
                engine.value: len(effects.for_engine(engine)) for engine in EffectEngine
            },
            "validation": report.summary(),
            "clamped": list(built.clamped),
            "warnings": [*built.warnings, *(str(item) for item in report.warnings)],
        }

    # -- inputs ---------------------------------------------------------

    def _story_result(self, context: WorkerContext) -> dict[str, Any]:
        """What the STORY stage decided, read back from its job row (§81).

        Not recomputed. Re-running the optimiser here would usually agree, and
        the one time it did not -- a changed weight, a re-scored moment -- the
        EDL would describe a different video from the plan the user approved,
        with nothing to indicate it. The stage that made the decision wrote it
        down; this reads it.
        """
        job = JobRepository(context.database).find(context.project_id, JobStage.STORY, None)
        if job is None:
            raise ValidationError(
                "The EDL stage ran before the narrative stage produced a plan.",
                code=ErrorCode.JOB_DEPENDENCY_FAILED,
                details={"project_id": context.project_id},
            )
        return dict(job.result)

    def _durations(self, context: WorkerContext) -> dict[str, float]:
        """Source lengths, so a clip cannot be laid out past the end of a file."""
        media = MediaRepository(context.database).list_for_project(context.project_id)
        return {
            item.id: item.metadata.duration_seconds
            for item in media
            if item.metadata.duration_seconds
        }

    def _captions(self, context: WorkerContext, timeline: Timeline):
        """Transcript segments for every recording the edit draws on (§71)."""
        repository = TranscriptRepository(context.database)
        segments: dict[str, list[TranscriptSegment]] = {
            media_id: list(repository.list_for_media(media_id))
            for media_id in timeline.media_ids()
        }
        return caption_builder.build_captions(timeline, segments, context.config.captions)

    def _effects(self, context: WorkerContext, timeline: Timeline) -> EffectPlan:
        """Plan effects against the finished clip positions (§68-§70).

        The planner works in timeline coordinates because an effect is placed in
        the finished video, not in the source recording -- so it is given the
        clips as they now sit, not the moments they came from.

        The detected event types ride along. Every ``events:`` list in the
        effects library was dead code while ``PlannedMoment.events`` stayed
        empty -- event-triggered candidates never matched, ``text_pop`` could
        only ever name the moment's type, and the evidence guards would reject
        effects whose evidence genuinely existed one query away.
        """
        events_by_moment = {
            str(moment.metadata.get("id") or ""): [
                event.event_type for event in moment.events
            ]
            for moment in MomentRepository(context.database).list_for_project(
                context.project_id
            )
        }
        planned = [
            PlannedMoment(
                id=clip.moment_id or clip.id,
                moment_type=clip.moment_type,
                timeline_start=clip.timeline_start,
                timeline_end=clip.timeline_end,
                score=clip.score,
                events=events_by_moment.get(clip.moment_id or "", []),
                clip_id=clip.id,
            )
            for clip in timeline.video_clips()
            if clip.moment_type is not None
        ]
        if not planned:
            return EffectPlan(intensity=context.config.effects.intensity)

        service = InteractionService(context.database, context.config)
        intent = service.current_intent(context.project_id)
        return EffectPlanner(context.config).plan(
            planned, intent, video_duration_seconds=timeline.duration
        )


__all__ = ["EdlWorker"]
