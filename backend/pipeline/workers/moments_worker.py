"""The MOMENTS stage (SPEC sections 28-34).

The pipeline's centre of gravity. Everything before it describes a recording;
everything after it edits one. This is where "these things happened" becomes
"these are the clips worth watching, ranked, with reasons".

The stage is a fixed sequence, and the order is load-bearing:

    form → expand context → dead time → repetition → variety → score

Context expansion comes before dead time because dead time is measured against
what a clip would actually *show* -- the pre-roll is already part of a moment,
and counting it as removable would mean the same seconds are both kept and cut.
Variety comes before scoring because §33's saturation penalty is an input to
the score, not a filter applied afterwards.

Nothing here decodes video. Every input is a database read, so re-running after
a weight change costs milliseconds -- which is exactly what §127 demands of a
re-edit.
"""

from __future__ import annotations

from typing import Any

from backend.analysis import frame_state
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.database.repositories.audio_events import AudioEventRepository
from backend.database.repositories.gaming import GameEventRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.scenes import SceneRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.database.repositories.vision import VisionRepository
from backend.moments.context import ExpansionSources, expand
from backend.moments.dead_time import dead_time_ratio, detect_dead_time
from backend.moments.formation import form_moments
from backend.moments.repetition import (
    detect_repetition,
    saturation_penalties,
    variety_report,
)
from backend.moments.scoring import ScoringContext, score_moments
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.workers.moments", LogChannel.PIPELINE)


class MomentsWorker:
    """MOMENTS -- ranked candidate clips, each with its reasoning (§28-§34)."""

    stage = JobStage.MOMENTS

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        config = context.config.moments
        duration = media.metadata.duration_seconds or 0.0

        events = GameEventRepository(context.database).list_for_media(media.id)
        if not events:
            context.report(1.0, "No events to form moments from")
            return {"skipped": True, "reason": "no game events", "moments": 0}

        audio = AudioEventRepository(context.database).list_for_media(media.id)
        transcript = TranscriptRepository(context.database).list_for_media(media.id)
        scenes = SceneRepository(context.database).list_for_media(media.id)
        vision = VisionRepository(context.database).list_for_media(media.id)

        context.report(0.2, "Forming moments")
        # Phase 0.6: the vision model has always said which frames were menus,
        # loading screens and cutscenes. Reading it here keeps them out of the
        # edit instead of letting QA report them after the render.
        screen_states = frame_state.spans(vision, duration_seconds=duration)
        moments = form_moments(
            events,
            config.formation,
            media_id=media.id,
            non_gameplay=frame_state.non_gameplay(screen_states),
        )
        if not moments:
            context.report(1.0, "No moments formed")
            return {"skipped": True, "reason": "no moments formed", "moments": 0}

        context.report(0.4, "Expanding context")
        moments = expand(
            moments,
            config.context,
            ExpansionSources(
                scenes=scenes,
                transcript=transcript,
                audio_events=audio,
                duration_seconds=duration,
            ),
        )

        context.report(0.6, "Measuring dead time and repetition")
        dead_segments = detect_dead_time(
            duration,
            config.dead_time,
            moments=moments,
            audio_events=audio,
            transcript=transcript,
            vision=vision,
        )
        dead_scores = {
            (moment.media_id, round(moment.start_seconds, 3)): dead_time_ratio(
                moment, dead_segments
            )
            for moment in moments
        }
        repetition = detect_repetition(moments, config.repetition)
        saturation = saturation_penalties(moments, config.variety)

        context.report(0.8, "Scoring")
        scored = score_moments(
            moments,
            config.scoring,
            ScoringContext(
                duration_seconds=duration,
                audio_events=audio,
                transcript=transcript,
                vision=vision,
                dead_time=dead_scores,
                repetition=repetition.scores,
                saturation=saturation,
            ),
        )

        # Below the floor a moment is not offered for selection at all. Kept out
        # of storage rather than stored and filtered, so the review screen and
        # the narrative stage see the same list.
        offered = [
            moment for moment in scored if moment.score >= config.scoring.minimum_score
        ]

        repository = MomentRepository(context.database)
        with context.database.transaction():
            stored = repository.replace_for_media(
                context.project_id,
                media.id,
                offered,
                needs_review_below=config.scoring.needs_review_confidence,
            )

        variety = variety_report(offered, config.variety)
        context.report(1.0, f"{stored} moments")
        return {
            "moments": stored,
            "formed": len(moments),
            "below_minimum": len(scored) - len(offered),
            "by_type": repository.counts_by_type(media.id),
            "needs_review": sum(
                1
                for moment in offered
                if moment.confidence < config.scoring.needs_review_confidence
            ),
            "top_score": round(offered[0].score, 4) if offered else 0.0,
            "total_context_seconds": round(
                repository.total_context_seconds(media.id), 2
            ),
            "dead_time_segments": len(dead_segments),
            "protected_dead_time": sum(1 for item in dead_segments if item.protected),
            "repetition_groups": len(repetition.groups),
            "variety": variety,
            **frame_state.report(screen_states, duration),
        }


__all__ = ["MomentsWorker"]
