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

from collections.abc import Callable, Sequence
from typing import Any

from ai.llm import create_llm_provider
from backend.core.errors import ErrorCode, GamingEditorError, NarrativeError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, VideoMode
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.projects import ProjectRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.director import build_blueprint
from backend.director.models import Blueprint
from backend.interaction.models import EditingIntent, MessageRole
from backend.interaction.service import InteractionService
from backend.interaction.store import ConversationStore, IntentStore
from backend.moments.formation import Moment
from backend.narrative.story import NarrativePlan, build_plan
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.workers.story", LogChannel.PIPELINE)

#: How much of the project conversation the Director is shown. Enough for a
#: session's worth of instructions, short of pasting a chat log into a prompt.
_BRIEF_MESSAGES: int = 20
_BRIEF_CHARACTERS: int = 800


class StoryWorker:
    """STORY -- choose and order the clips that make the video (§35-§39)."""

    stage = JobStage.STORY

    def __init__(self, llm_provider: Any = None) -> None:
        """
        Args:
            llm_provider: the Director's model, injected the way every other
                model in this pipeline is, so a test proves the wiring without
                depending on what happens to be installed. Built lazily when
                absent, and unloaded as soon as it has answered.
        """
        self._llm = llm_provider

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

        # §4: the editing brief is what the person asked for, and this is the
        # first stage that can act on it (§10). Until now it reached only the
        # effects planner, so every preference about *which clips and in what
        # order* -- the ones people actually type -- changed nothing.
        intent = InteractionService(context.database, context.config).current_intent(
            context.project_id
        )
        target = float(intent.target_duration_seconds or project.target_duration_seconds)
        requested = intent.mode or project.mode
        mode = requested if isinstance(requested, VideoMode) else VideoMode(requested)

        # §14's word timestamps, for the final cut-point refinement: without
        # them a cut can land mid-word and nothing downstream can repair it.
        transcripts = TranscriptRepository(context.database)
        media_repository = MediaRepository(context.database)
        media_ids = sorted({moment.media_id for moment in moments})
        speech = {media_id: transcripts.list_for_media(media_id) for media_id in media_ids}
        durations = {
            media_id: media.metadata.duration_seconds
            for media_id in media_ids
            if (media := media_repository.require(media_id)).metadata.duration_seconds
        }

        context.report(0.4, f"Selecting clips for a {target / 60:.0f}-minute {mode.value} edit")
        plan = build_plan(
            moments,
            mode=mode,
            target_seconds=target,
            config=context.config.narrative,
            policy=context.config.duration_policy,
            chronological=intent.chronological,
            speech=speech,
            media_durations=durations,
            director=self._director(context, intent, target),
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
        _ensure_plan_chronology(plan)
        return {
            **_serialise(plan),
            "moments_considered": len(moments),
            # §80 (V2 P1): the session's natural form beside the plan.
            "session_shape": _session_shape(context, plan),
        }

    def _director(
        self, context: WorkerContext, intent: EditingIntent, target: float
    ) -> Callable[[Sequence[Moment]], Blueprint | None] | None:
        """The Director, or nothing, as a callable ``build_plan`` can hand a list.

        A callable rather than a finished blueprint, because the beats index
        into the optimiser's selection and only ``build_plan`` knows what that
        selection is. Handing over a blueprint built from a different list is
        how the first version of this put the climax role on the wrong clip.

        Returns ``None`` when the Director is off, and otherwise a callable
        that may itself return ``None`` -- no model, no server, an answer that
        names a moment nobody found. Every one of those paths ends in the
        deterministic order this stage has used since Phase 7 (§95).
        """
        if not context.config.narrative.director.enabled:
            return None

        brief = self._brief(context, intent)

        def propose(shown: Sequence[Moment]) -> Blueprint | None:
            provider = self._llm
            try:
                if provider is None:
                    provider = create_llm_provider(context.config)
            except GamingEditorError as error:
                # Not having a reasoning model available is a smaller edit,
                # not a failed one.
                logger.info(
                    "No Director for this edit; using the deterministic order",
                    extra={"project_id": context.project_id, "reason": str(error)},
                )
                return None

            try:
                outcome = build_blueprint(
                    shown,
                    provider=provider,
                    intent_text=brief,
                    target_seconds=target,
                    style=intent.style,
                )
            finally:
                # §54: this is the last model to run before the render, and
                # NVENC and Chromium both want the card next. Unload whether
                # the answer was usable or not.
                provider.unload()

            if isinstance(outcome, Blueprint):
                logger.info(
                    "The Director proposed a shape",
                    extra={
                        "project_id": context.project_id,
                        "theme": outcome.theme,
                        "beats": len(outcome.beats),
                    },
                )
                return outcome
            logger.info(
                "Keeping the deterministic order",
                extra={
                    "project_id": context.project_id,
                    "reason": outcome.reason,
                    **outcome.detail,
                },
            )
            return None

        return propose

    def _brief(self, context: WorkerContext, intent: EditingIntent) -> str:
        """What the person asked for, in their own words where there are any.

        The resolved :class:`EditingIntent` is a set of enum values -- enough
        for the optimiser, thin for a model being asked what the video is
        about. Two stores hold the sentences behind those values:

        * the intent log, which keeps the words that changed a setting (§4,
          "kept verbatim for auditability"), and
        * the conversation, which keeps every word typed at the project.

        Both, because the second contains the first and more. "keep the part
        where the base falls over" changes no setting, so the intent log never
        sees it -- and it is exactly the sentence this stage exists to act on.
        """
        said: list[str] = [
            message.text.strip()
            for message in ConversationStore(context.database).history(
                context.project_id, limit=_BRIEF_MESSAGES
            )
            if message.role is MessageRole.USER and message.text.strip()
        ]
        said += [
            update.raw_text.strip()
            for update in IntentStore(context.database).updates(context.project_id)
            if update.raw_text and update.raw_text.strip()
        ]
        # Oldest first, deduplicated: a later instruction refines an earlier
        # one, and the model should read them the way the resolver applied them.
        unique = list(dict.fromkeys(said))
        if not unique:
            return f"a {intent.style} edit of this session"
        brief = " ".join(unique)
        return brief if len(brief) <= _BRIEF_CHARACTERS else brief[-_BRIEF_CHARACTERS:]

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


def _session_shape(context, plan) -> list[dict]:
    """§80: the session's natural form beside the plan that shaped it (V2 P1).

    Read from the Semantic Timeline of the plan's primary recording. Any
    failure is an empty list -- the shape is a lens, never a gate.
    """
    try:
        from backend.database.repositories.media import MediaRepository
        from backend.semantic.timeline import load_timeline

        media_ids = {moment.media_id for moment in plan.moments}
        if len(media_ids) != 1:
            return []
        media_id = next(iter(media_ids))
        media = MediaRepository(context.database).get(media_id)
        duration = getattr(media.metadata, "duration_seconds", None) if media else None
        if not duration:
            return []
        timeline = load_timeline(
            context.database,
            media_id,
            duration_seconds=float(duration),
            config=context.config,
            cache_dir=context.paths.analysis / "semantic",
        )
        return timeline.summary()
    except Exception:
        logger.exception("Session shape unavailable; the plan ships without it")
        return []


def _ensure_plan_chronology(plan) -> None:
    """V2's constitution, enforced where the plan is born."""
    from backend.timeline.validation import ensure_chronological

    class _AsClip:
        __slots__ = ("media_id", "role", "source_start")

        def __init__(self, moment):
            self.media_id = moment.media_id
            self.source_start = moment.context_start
            self.role = moment.metadata.get("role", "body")

    ensure_chronological([_AsClip(moment) for moment in plan.moments])

