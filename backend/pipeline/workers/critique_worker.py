"""The CRITIQUE stage (Phase E).

The first stage that reads the pipeline's own output instead of the recording.
Everything before it works from evidence about the source; this one works from
the edit that evidence produced, which is the only object a viewer will ever
see.

It sits between EDL and RENDER for a practical reason. A criticism of the
finished MP4 costs a re-render to act on; the same criticism of the timeline
costs a database write, because §42's operations are non-destructive and the
render has not started. So the loop closes before the expensive part rather
than after it.

Nothing here decodes video, and nothing re-runs analysis. The vision
descriptions, the transcript and the named events all exist already; this
stage's whole contribution is arranging them per *clip of the edit* and asking
a model what a viewer would think.
"""

from __future__ import annotations

from typing import Any

from ai.llm import create_llm_provider
from backend.core.errors import GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.critic import evidence as edit_evidence
from backend.critic import revision as edit_revision
from backend.critic.models import Critique, CritiqueRejection
from backend.critic.service import review
from backend.database.repositories.gaming import GameEventRepository
from backend.database.repositories.projects import ProjectRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.database.repositories.vision import VisionRepository
from backend.interaction.service import InteractionService
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.workers.critique", LogChannel.PIPELINE)


class CritiqueWorker:
    """CRITIQUE -- review the assembled edit and act on what is wrong (Phase E)."""

    stage = JobStage.CRITIQUE

    def __init__(self, llm_provider: Any = None) -> None:
        """
        Args:
            llm_provider: the Critic's model, injected the way every other
                model in this pipeline is. Built lazily when absent, and
                unloaded as soon as it has answered (§54).
        """
        self._llm = llm_provider

    def run(self, context: WorkerContext) -> dict[str, Any]:
        config = context.config.critique
        repository = TimelineRepository(context.database)
        timeline = repository.load(context.project_id)

        if not config.enabled:
            context.report(1.0, "The Critic is switched off")
            return _skipped("the critic is switched off")
        if timeline is None or not timeline.video_clips():
            # No edit is a dead end the EDL stage already explained. Reviewing
            # nothing and reporting a verdict would hide it (§95).
            context.report(1.0, "No edit to review")
            return _skipped("there is no edit to review")

        intent = InteractionService(context.database, context.config).current_intent(
            context.project_id
        )
        project = ProjectRepository(context.database).require(context.project_id)
        target = float(intent.target_duration_seconds or project.target_duration_seconds)

        context.report(0.2, "Reading the edit")
        gathered = self._evidence(context, timeline, target)

        context.report(0.5, f"Reviewing {len(gathered.clips)} clips")
        outcome = self._review(context, gathered, intent)
        if not isinstance(outcome, Critique):
            context.report(1.0, "The edit was not reviewed")
            return _skipped(outcome.reason, detail=outcome.detail, evidence=gathered.summary())

        context.report(0.8, "Applying what the review asked for")
        # V2's pacing engine chose every one of these lengths from the
        # session's own heat; the Critic tightens shots, it does not erase
        # them. Same reader as QA's, so the two cannot disagree about which
        # short shot was deliberate.
        from backend.semantic.levels import clip_levels, floor_for

        levels = clip_levels(
            context.database,
            timeline,
            config=context.config,
        )
        revision = edit_revision.apply(
            timeline,
            outcome,
            gathered,
            policy=context.config.duration_policy,
            target_seconds=target,
            allow_drops=config.allow_drops,
            clip_floor=lambda index: floor_for(levels.get(index), context.config),
        )
        if revision.changed and config.apply:
            # P0.3, brief rule 4: the Critic may not widen an authorized span.
            # It only ever narrows through operations.trim, which refuses a
            # widening from anyone but a person; this is the check that the
            # timeline it wrote still lies inside every grant it carries.
            from backend.timeline import validation

            validation.require_valid(revision.timeline)
            repository.save_edit(context.project_id, revision.timeline)
        elif revision.changed:
            # Reviewed, reported, not acted on: §78's "the human has the last
            # word" as a setting rather than a slogan.
            revision = edit_revision.Revision(
                timeline=timeline,
                applied=(),
                refused=(
                    *revision.refused,
                    *(f"{note} (not applied)" for note in revision.applied),
                ),
            )

        context.report(1.0, _reported(revision))
        return {
            "verdict": outcome.verdict,
            "reviewed_clips": len(gathered.clips),
            "applied": list(revision.applied),
            "refused": list(revision.refused),
            "seconds_removed": round(revision.seconds_removed, 3),
            "notes": [
                {
                    "clip": note.clip,
                    "action": note.action.value,
                    "seconds": note.seconds,
                    "reason": note.reason,
                }
                for note in outcome.notes
            ],
            "evidence": gathered.summary(),
        }

    # -- internals ------------------------------------------------------

    def _evidence(self, context: WorkerContext, timeline, target: float):
        """Everything already stored, arranged per clip of the edit."""
        media_ids = sorted({clip.media_id for clip in timeline.video_clips()})
        vision = VisionRepository(context.database)
        transcripts = TranscriptRepository(context.database)
        events = GameEventRepository(context.database)
        repository = TimelineRepository(context.database)
        return edit_evidence.gather(
            timeline,
            target_seconds=target,
            observations={media: vision.list_for_media(media) for media in media_ids},
            speech={media: transcripts.list_for_media(media) for media in media_ids},
            events={media: events.list_for_media(media) for media in media_ids},
            captions=repository.list_captions(context.project_id),
            # Every effect, both engines: what the viewer sees is the sum,
            # and which renderer draws it is not the Critic's business.
            effects=repository.list_effects(context.project_id),
        )

    def _review(self, context: WorkerContext, gathered, intent):
        provider = self._llm
        try:
            if provider is None:
                provider = create_llm_provider(context.config)
        except GamingEditorError as error:
            logger.info(
                "No Critic for this edit; rendering it unreviewed",
                extra={"project_id": context.project_id, "reason": str(error)},
            )
            return CritiqueRejection(reason="no reasoning model is available")

        try:
            return review(gathered, provider=provider, intent_text=intent.style)
        finally:
            # §54: the render stages want the card next, and this is the last
            # model before them.
            provider.unload()


def _skipped(reason: str, **extra: Any) -> dict[str, Any]:
    """A stage result that says plainly that nothing was reviewed.

    The keys the render stage and the UI read are present either way, with the
    types they always have -- a result whose shape depends on which branch
    produced it is not a contract (§81).
    """
    return {
        "skipped": True,
        "reason": reason,
        "verdict": "",
        "reviewed_clips": 0,
        "applied": [],
        "refused": [],
        "seconds_removed": 0.0,
        "notes": [],
        **extra,
    }


def _reported(revision: edit_revision.Revision) -> str:
    if revision.changed:
        return f"{len(revision.applied)} change(s) from the review"
    if revision.refused:
        return "reviewed; nothing could be changed"
    return "reviewed; nothing to change"


__all__ = ["CritiqueWorker"]
