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

    def _excluded_spans(self, context, media, vision, duration: float):
        """Non-gameplay stretches, from every store that has an opinion.

        Never fatal: a store that will not answer leaves the older, vision-only
        guard standing rather than stopping the stage (§95).
        """
        from backend.analysis import frame_state as _fs
        from backend.database.repositories.gaming import OcrRepository
        from backend.gaming import content
        from backend.gaming.profiles import GENERIC_PROFILE, load_profile

        try:
            row = context.database.fetch_one(
                "SELECT game_profile FROM ocr_results WHERE media_id = ? "
                "AND game_profile IS NOT NULL LIMIT 1",
                (media.id,),
            )
            name = str(row["game_profile"]) if row is not None else ""
            profile = (
                load_profile(name, context.profiles_dir).profile
                if name
                else GENERIC_PROFILE
            )
            detections = OcrRepository(context.database).list_for_media(media.id)
            states = content.read(
                detections=detections,
                frame_spans=_fs.non_gameplay(
                    _fs.spans(vision, duration_seconds=duration)
                ),
                profile=profile,
                duration_seconds=duration,
            )
        except Exception:
            logger.exception("Content states unavailable; the vision guard stands")
            return ()
        return content.excluded_spans(
            states,
            observed_at=[d.timestamp for d in detections]
            + [float(getattr(o, "timestamp", 0.0)) for o in vision],
        )

    def _reader(self, context: WorkerContext, media, duration: float):
        """The session's lanes, or ``None``.

        Never raises: a stage that cannot read the shape still forms moments,
        it just forms them the way it did before V2.
        """
        if not duration:
            return None
        try:
            from backend.semantic.timeline import load_timeline

            return load_timeline(
                context.database,
                media.id,
                duration_seconds=float(duration),
                config=context.config,
            )
        except Exception:
            logger.exception(
                "No semantic timeline for this recording; moments form without it",
                extra={"media_id": media.id},
            )
            return None

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

        # V2-P2: the session's own lanes, built by the stage before this one.
        # Absent (an older project, a recording with no duration) every step
        # below falls back to what it did before V2 -- §95, not an exception.
        reader = self._reader(context, media, duration)

        context.report(0.2, "Forming moments")
        # Phase 0.6: the vision model has always said which frames were menus,
        # loading screens and cutscenes. Reading it here keeps them out of the
        # edit instead of letting QA report them after the render.
        screen_states = frame_state.spans(vision, duration_seconds=duration)
        # V2-P0.2: the vision half above, and the text half beside it. The
        # labels see menus with nothing written on them; the OCR sees the ones
        # whose whole identity is written text -- MISSIONFAILED, EXIT TO MENU.
        # Measured on one 88-minute session, the two refuse ten and sixteen
        # clips of a shipped render and share only four, so neither alone was
        # ever going to be enough.
        moments = form_moments(
            events,
            config.formation,
            media_id=media.id,
            non_gameplay=frame_state.non_gameplay(screen_states),
            excluded_spans=self._excluded_spans(context, media, vision, duration),
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
                reader=reader,
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

        # The shape of each surviving moment, stored beside it: where its
        # build begins, where it pays off, where the come-down ends. Read by
        # pacing, emphasis, audio and the Critic rather than re-derived four
        # times from four slightly different assumptions.
        offered = [_with_phases(moment, reader) for moment in offered]

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


def _with_phases(moment, reader):
    """Attach the moment's phases to its metadata (§80)."""
    from dataclasses import replace

    from backend.moments.phases import classify_phases

    phases = classify_phases(
        reader,
        start_seconds=moment.context_start or moment.start_seconds,
        end_seconds=moment.context_end or moment.end_seconds,
    )
    return replace(
        moment,
        metadata={
            **moment.metadata,
            "phases": [phase.as_dict() for phase in phases],
        },
    )
