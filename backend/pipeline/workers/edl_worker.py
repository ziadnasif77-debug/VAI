"""The EDL stage (SPEC §40–§45, §71).

Where the plan becomes an edit. The narrative stage chose moments and put them
in an order; this puts them on a timeline, times the captions against it, plans
the effects for it, validates the result and stores it.

The order matters and is not arbitrary:

    build → validate → effects → re-lay → captions → persist

Effects come after validation because the planner is given the built clip
positions and a budget measured in effects per minute — it needs a real
timeline. The re-lay comes next because a time-warping effect (freeze_frame,
speed_ramp) *changes clip durations*: a frozen clip occupies its source span
plus the hold, and every clip after it shifts. Captions therefore come last —
they are timed against clip positions, and timing them before the re-lay left
every caption after a frozen clip late by the length of the hold. (They also
come after validation for the original reason: timing them against a timeline
that turns out to be invalid means doing it twice.)

Nothing here decodes video. Every input is a database read and the output is
rows, which is what makes §127's re-edit cheap: changing a clip re-runs this
stage in milliseconds and never re-analyses the source.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
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
from backend.effects.models import EffectInstance, EffectPlan
from backend.effects.planner import EffectPlanner, PlannedMoment
from backend.interaction.service import InteractionService
from backend.pipeline.workers.base import WorkerContext
from backend.style import bible as style_bible
from backend.timeline import captions as caption_builder
from backend.timeline import retime, validation
from backend.timeline.builder import build_timeline, clips_from_story_result
from backend.timeline.models import Timeline

logger = get_logger("pipeline.workers.edl", LogChannel.PIPELINE)


def _refusal_tally(planned: Sequence[Any], states_by_media: Mapping[str, Sequence[Any]]) -> str:
    """How many planned clips each kind of exclusion touched, as one sentence.

    A clip touched by two kinds is counted under both: the sentence answers
    "what was on screen where the story chose to cut", not "which rule won".
    A clip no stored state touches was refused by the builder's own floor --
    the surviving gameplay piece was shorter than a shot -- and is counted
    under that name.
    """
    counts: dict[str, int] = {}
    for clip in planned:
        touched = {
            str(getattr(state.state, "value", state.state))
            for state in states_by_media.get(clip.media_id, ())
            if state.covers(clip.source_start, clip.source_end)
        }
        if not touched:
            touched = {"trimmed below the minimum surviving length"}
        for name in touched:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return "no exclusion recorded"
    return ", ".join(
        f"{name}: {count}" for name, count in sorted(counts.items(), key=lambda item: -item[1])
    )


class EdlWorker:
    """EDL -- the non-destructive description of the finished video (§40-§42)."""

    stage = JobStage.EDL

    def __init__(self, ocr_provider: Any = None) -> None:
        """
        Args:
            ocr_provider: the engine that reads the planned base frames
                (P0.2.2). Injected the way the OCR stage's is, so a test
                proves the wire without depending on what is installed.
                Built from the configuration when absent.
        """
        self._ocr_provider = ocr_provider

    def _read_planned_frames(self, context: WorkerContext, planned) -> dict[str, Any]:
        """OCR the base frames the edit will use and nobody has looked at (P0.2.2).

        Reads are appended to ``ocr_results`` -- they are ordinary reads with
        timestamps, and every consumer of the table reads them like any
        other -- and the frames are marked ``analyzed`` so a second run does
        not read them again. The OCR stage resets that mark when it replaces
        the reads.

        Never fatal (§95): a machine with no OCR engine, or a read that
        fails, builds the edit from the evidence it already had, and the
        result says so. A configuration error and a cancellation are not
        that and pass through.
        """
        from ai.ocr import create_ocr_provider
        from backend.core.errors import ConfigurationError
        from backend.database.repositories.frames import FrameRepository
        from backend.database.repositories.gaming import OcrRepository
        from backend.database.repositories.vision import VisionRepository
        from backend.gaming import planned_reads
        from backend.gaming.ocr import CROP_DIRNAME, read_frames
        from backend.media.ffmpeg import CancelledError

        settings = context.config.narrative.planned_frame_reads
        if not settings.enabled:
            return {"skipped": True, "reason": "disabled in configuration"}
        ocr = context.config.analysis.ocr
        if not ocr.enabled:
            return {"skipped": True, "reason": "ocr disabled in configuration"}
        try:
            provider = self._ocr_provider or create_ocr_provider(context.config)
        except Exception:
            logger.exception("No OCR provider for the planned frames; built without them")
            return {"skipped": True, "reason": "no OCR provider"}
        if provider is None or not provider.is_available():
            logger.info("No usable OCR engine; the planned frames go unread")
            return {"skipped": True, "reason": "no usable OCR engine"}

        summary: dict[str, Any] = {"frames": 0, "with_text": 0, "detections": 0, "media": 0}
        spans_by_media: dict[str, list[tuple[float, float]]] = {}
        for clip in planned:
            spans_by_media.setdefault(clip.media_id, []).append(
                (float(clip.source_start), float(clip.source_end))
            )
        for media_id, spans in spans_by_media.items():
            try:
                frames = FrameRepository(context.database).list_for_media(
                    media_id, level="base", analyzed=False
                )
                looked = [
                    d.timestamp for d in OcrRepository(context.database).list_for_media(media_id)
                ] + [
                    float(getattr(o, "timestamp", 0.0))
                    for o in VisionRepository(context.database).list_for_media(media_id)
                ]
                chosen = [
                    frame
                    for frame in planned_reads.select(
                        frames,
                        spans,
                        looked,
                        margin_seconds=settings.margin_seconds,
                        min_gap_seconds=settings.min_gap_seconds,
                    )
                    if Path(frame.image_path).is_file()
                ]
                if not chosen:
                    continue
                profile = self._profile(context, media_id)
                work_dir = context.paths.frames / media_id / CROP_DIRNAME
                context.report(0.05, f"Reading {len(chosen)} planned frames")
                try:
                    provider.load()
                    results = read_frames(
                        [(float(frame.timestamp), Path(frame.image_path)) for frame in chosen],
                        provider,
                        ocr,
                        profile,
                        work_dir=work_dir,
                        should_cancel=context.should_cancel,
                        on_progress=lambda fraction, message: context.report(
                            0.05 + 0.1 * fraction, message
                        ),
                    )
                finally:
                    provider.unload()
                    shutil.rmtree(work_dir, ignore_errors=True)
                detections = [item for frame in results for item in frame.detections]
                with context.database.transaction():
                    OcrRepository(context.database).add_for_media(
                        context.project_id,
                        media_id,
                        detections,
                        game_profile=profile.id,
                        engine=provider.info().provider,
                    )
                    FrameRepository(context.database).mark_analyzed(
                        [frame.id for frame in chosen]
                    )
            except (ConfigurationError, CancelledError):
                raise
            except Exception:
                logger.exception(
                    "Planned frame reads failed; the edit is built without them",
                    extra={"media_id": media_id},
                )
                continue
            summary["frames"] += len(chosen)
            summary["with_text"] += len(results)
            summary["detections"] += len(detections)
            summary["media"] += 1
            logger.info(
                "Read the planned base frames",
                extra={
                    "media_id": media_id,
                    "frames": len(chosen),
                    "with_text": len(results),
                    "detections": len(detections),
                },
            )
        return summary

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

        # V2-P8: which taste is cutting. Resolved once here and recorded, so
        # the renderer, QA and the post-render critic judge the video by the
        # style that made it rather than by whatever the brief says later.
        style = style_bible.resolve(
            context.config,
            self._asked_style(context),
            database=context.database,
        )

        guard = context.config.narrative.screen_guard
        if guard.enabled:
            before_guard = len(planned)
            planned = self._guarded(context, planned, durations, guard, style)
            if not planned:
                # [P0.2.2] A failure, not a skip. A skip left every later stage
                # "completed" with nothing to render, the interface showing a
                # green pipeline, and the daily ledger counting a recording
                # with no video as produced.
                raise ValidationError(
                    f"Every one of the {before_guard} planned clips opened inside dead "
                    "screen time -- menus, loading screens, black or frozen stretches "
                    "the source probe measured, or the "
                    f"{guard.recording_start_guard_seconds:.0f} s recording-start guard "
                    "-- and nothing survived the piece floor. The story chose clips "
                    "the screen guard could not keep; re-run the story stage or lower "
                    "narrative.screen_guard.min_piece_seconds.",
                    code=ErrorCode.INVALID_EDL,
                    details={"planned": before_guard, "refused_by": "screen_guard"},
                )

        # P0.2.2: before the exclusions are read, read the frames they will
        # be read from. The detectors sampled candidate frames; the edit is
        # about to use seconds nobody sampled.
        planned_reads_summary = self._read_planned_frames(context, planned)

        context.report(0.2, f"Laying out {len(planned)} clips")
        excluded_spans, exclusion_states = self._excluded(context, durations)
        built = build_timeline(
            planned,
            project_id=context.project_id,
            policy=policy,
            target_seconds=float(story.get("target_seconds", 0.0)),
            media_durations=durations,
            transitions=context.config.narrative.transitions,
            notes=story.get("notes", ()),
            excluded_spans=excluded_spans,
            metadata={
                "mode": story.get("mode"),
                "within_target": story.get("within_target"),
                "hook": story.get("hook"),
            },
        )
        timeline = built.timeline
        if not timeline.video_clips():
            # [P0.2.2] Every planned clip was refused, and the stage fails and
            # says by what. The first cut of this returned a "skipped" result
            # with the reason in a JSON field nobody displayed: every later
            # stage then completed on an empty timeline, the interface showed
            # a green pipeline with no video, and the daily ledger counted the
            # recording as produced. A validation error carrying the tally
            # and the builder's own notes is the loud version.
            tally = _refusal_tally(planned, exclusion_states)
            notes = [str(note) for note in built.notes[-3:]]
            raise ValidationError(
                f"Every one of the {len(planned)} planned clips was refused as not "
                f"gameplay: {tally}. "
                + (" ".join(notes) if notes else "The builder recorded no note.")
                + " The story chose clips the exclusion layer could not keep; re-run the "
                "story stage, or look at the recording where these clips were planned.",
                code=ErrorCode.INVALID_EDL,
                details={
                    "planned": len(planned),
                    "refused_by": "exclusions",
                    "tally": tally,
                    "notes": [str(note) for note in built.notes],
                    "planned_frame_reads": planned_reads_summary,
                },
            )

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

        context.report(0.55, "Planning effects")
        effects = self._effects(context, timeline)

        instances = effects.instances
        retimed = 0
        if instances and context.config.effects.realisation.ffmpeg_filters:
            # The re-lay happens only when the wire that realises time-warps
            # is live: with it off the rows stay stored-but-unrealised, and a
            # timeline promising seconds nothing will render would fail the
            # §76 duration gate it exists to keep honest.
            relaid = retime.relay_timeline(
                timeline, instances, max_duration_seconds=policy.max_seconds
            )
            retimed = relaid.retimed_clips
            if retimed:
                context.report(0.65, f"Re-laid {retimed} clip(s) for time effects")
                after = validation.validate(
                    relaid.timeline, media_durations=durations, policy=policy
                )
                if not after.is_valid:
                    raise ValidationError(
                        "The re-laid timeline is not renderable: "
                        + "; ".join(str(item) for item in after.errors),
                        code=ErrorCode.INVALID_EDL,
                        details={"findings": [str(item) for item in after.findings]},
                    )
                timeline = relaid.timeline
                instances = relaid.effects

        context.report(0.8, "Timing captions against the transcript")
        built_captions = self._captions(context, timeline)
        timeline = caption_builder.caption_track(timeline, built_captions)

        written = TimelineRepository(context.database).replace(
            context.project_id, timeline, captions=built_captions, effects=instances
        )
        # Recorded with the edit rather than after it: an edit whose style is
        # unknown cannot be compared with anything later, which is the whole
        # of what P9 will need from this phase.
        style_bible.stamp(context.database, context.project_id, style)
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
            "retimed_clips": retimed,
            "effects_by_engine": {
                engine.value: len(effects.for_engine(engine)) for engine in EffectEngine
            },
            "style": style.name,
            "style_version": style.version,
            "planned_frame_reads": planned_reads_summary,
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

    def _excluded(
        self, context: WorkerContext, durations: dict[str, float]
    ) -> tuple[list[tuple[float, float]], dict[str, list[Any]]]:
        """Source stretches that are not gameplay, across every recording used.

        Read through the one reader every stage shares
        (:mod:`backend.gaming.exclusions`); nothing is re-analysed. Never
        fatal for a store that will not answer (§95); a configuration error
        passes through (P0.2.1). Returns the spans and, per recording, the
        states behind them for the refusal tally.
        """
        from backend.gaming.exclusions import read_exclusions

        spans: list[tuple[float, float]] = []
        states_by_media: dict[str, list[Any]] = {}
        for media_id, length in durations.items():
            found = read_exclusions(
                context.database,
                media_id,
                duration_seconds=float(length),
                profiles_dir=context.profiles_dir,
            )
            spans.extend(found.spans)
            states_by_media[media_id] = list(found.states)
            if found.spans:
                logger.info(
                    "Refusing footage that is not gameplay",
                    extra={
                        "media_id": media_id,
                        "spans": len(found.spans),
                        "seconds": round(found.seconds, 2),
                    },
                )
        return spans, states_by_media
    def _profile(self, context: WorkerContext, media_id: str) -> Any:
        """The game's profile, or the generic one (P0.2.1: a missing
        profiles directory is raised, not swallowed)."""
        from backend.core.errors import ConfigurationError
        from backend.gaming.exclusions import profile_for
        from backend.gaming.profiles import GENERIC_PROFILE

        try:
            return profile_for(context.database, media_id, context.profiles_dir)
        except ConfigurationError:
            raise
        except Exception:
            logger.exception("Profile unavailable; the generic table stands")
            return GENERIC_PROFILE
    def _durations(self, context: WorkerContext) -> dict[str, float]:
        """Source lengths, so a clip cannot be laid out past the end of a file."""
        media = MediaRepository(context.database).list_for_project(context.project_id)
        return {
            item.id: item.metadata.duration_seconds
            for item in media
            if item.metadata.duration_seconds
        }

    def _asked_style(self, context: WorkerContext) -> str | None:
        """What this project's brief calls its style, if it can be read."""
        try:
            intent = InteractionService(
                context.database, context.config
            ).current_intent(context.project_id)
        except Exception:
            logger.info("No editing brief; the house style cuts this one")
            return None
        return intent.style

    def _guarded(self, context, planned, durations, guard, style=None):
        """§77's screen states and the stored scene seams, applied to bounds."""
        from ai.ocr import create_ocr_provider
        from backend.analysis import frame_state
        from backend.analysis.recorder_probe import recorder_spans
        from backend.analysis.source_dead import dead_source_spans
        from backend.database.repositories.media import MediaRepository
        from backend.database.repositories.scenes import SceneRepository
        from backend.database.repositories.vision import VisionRepository
        from backend.timeline.screen_guard import guard_clips

        media_repository = MediaRepository(context.database)
        try:
            ocr = create_ocr_provider(context.config)
        except Exception:
            ocr = None
        states = {}
        scenes = {}
        for media_id in {clip.media_id for clip in planned}:
            spans = list(
                frame_state.spans(
                    VisionRepository(context.database).list_for_media(media_id),
                    duration_seconds=durations.get(media_id),
                )
            )
            record = media_repository.get(media_id)
            if record is not None and record.source_path:
                spans.extend(
                    recorder_spans(
                        Path(record.source_path),
                        ffmpeg=context.ffmpeg,
                        ocr=ocr,
                        scratch_dir=context.paths.analysis / "recorder-probe",
                    )
                )
                # QA has been reporting the recording's own black and frozen
                # stretches after every render (three warnings on a real
                # session, all tracing to the source). Same detectors, same
                # thresholds, run once and cached -- so the guard cuts them
                # instead of QA lamenting them (S36).
                spans.extend(
                    dead_source_spans(
                        Path(record.source_path),
                        ffmpeg=context.ffmpeg,
                        config=context.config,
                        cache_dir=context.paths.analysis / "source-dead",
                        media_id=media_id,
                        # Only the seconds the plan is about to use: a probe
                        # over the whole recording died on a mid-file OBS
                        # corruption once, silently, and most of a session is
                        # not in the edit anyway.
                        windows=[
                            (clip.source_start, clip.source_end)
                            for clip in planned
                            if clip.media_id == media_id
                        ],
                        duration_seconds=durations.get(media_id),
                    )
                )
            states[media_id] = spans
            scenes[media_id] = SceneRepository(context.database).list_for_media(media_id)
        events = {
            media_id: [
                (float(row["start_seconds"]), float(row["end_seconds"]))
                for row in context.database.fetch_all(
                    # Strong events only: the generic unknown_event may
                    # not bless stillness (see the guard's neighbourhood
                    # veto) -- on real footage it dominates the stream and
                    # would bless everything.
                    "SELECT start_seconds, end_seconds FROM game_events "
                    "WHERE media_id = ? AND event_type != 'unknown_event'",
                    (media_id,),
                )
            ]
            for media_id in states
        }
        # V2 P1: the semantic timeline sets each clip's cut-length cap by
        # the level of ITS OWN stretch -- calm breathes, a climax cuts fast.
        # Dynamic off, or no evidence: the static tier caps stand untouched.
        from backend.editorial import pacing_engine
        from backend.semantic.timeline import load_timeline

        timelines = {}
        if context.config.editorial.pacing.dynamic:
            for media_id in states:
                length = durations.get(media_id)
                if not length:
                    continue
                try:
                    timelines[media_id] = load_timeline(
                        context.database,
                        media_id,
                        duration_seconds=float(length),
                        config=context.config,
                    )
                except Exception:
                    logger.exception(
                        "Semantic timeline unavailable; static caps stand",
                        extra={"media_id": media_id},
                    )

        # Strong event onsets, per recording: the beats a cut should land on
        # rather than a beat before.
        beats = {
            media_id: tuple(sorted(start for start, _end in spans))
            for media_id, spans in events.items()
        }
        def dynamic_cap(clip, previous_length=0.0):
            """The shot starting here, its length and the rules behind it."""
            from backend.timeline.screen_guard import _cap_for

            reader = timelines.get(clip.media_id)
            pacing_context = pacing_engine.context_at(
                clip.source_start,
                reader,
                role=clip.role,
                previous_length=previous_length,
                events=beats.get(clip.media_id, ()),
            )
            if pacing_context is None or not context.config.editorial.pacing.dynamic:
                # V1's tier caps, untouched, whenever the session cannot be read.
                return _cap_for(
                    clip,
                    max_seconds=guard.max_clip_seconds,
                    high_tier_max_seconds=45.0,
                    low_tier_max_seconds=100.0,
                )
            return pacing_engine.shot_length(pacing_context, context.config, style)

        # Where a hot stretch has neither a scene change nor an event --
        # 40 continuous seconds of action produced zero of both -- the
        # semantic lane's own local peaks are the beat the cut lands on.
        seam_hints = {
            media_id: _lane_peaks(timeline)
            for media_id, timeline in timelines.items()
        }
        # Where the session changes level, a shot ends: those edges are the
        # session's own shape, read once here and handed to the guard.
        # A finer shape than the narrative one: a two-second burst is not a
        # section a person would name, but it is a turn worth cutting on.
        level_stops = {
            media_id: [
                segment.start_seconds for segment in timeline.shape(min_segment=2.0)
            ]
            for media_id, timeline in timelines.items()
        }

        # Spans a cut may not land inside: spoken WORDS, not segments.
        #
        # The first version used the speech lane's runs, which are the
        # transcript's segments -- and on this footage a segment runs 186
        # seconds. Holding a shot to the end of one would mean a three-minute
        # shot, so the rule protected nothing and could not have. Words are
        # the granularity that makes "do not cut mid-speech" an achievable
        # sentence rather than a slogan.
        no_cut = {
            media_id: _spoken_words(context.database, media_id)
            for media_id in timelines
        }

        guarded = guard_clips(
            planned,
            states_by_media=states,
            scenes_by_media=scenes,
            events_by_media=events,
            seam_hints_by_media=seam_hints,
            level_stops_by_media=level_stops,
            no_cut_by_media=no_cut,
            jump_cut_gap=context.config.editorial.pacing.jump_cut_gap_seconds,
            jump_cut_below=context.config.editorial.pacing.bands.normal.max,
            cap_fn=dynamic_cap if timelines else None,
            min_observations=guard.min_observations,
            bridge_interior_seconds=guard.bridge_interior_seconds,
            recording_start_guard_seconds=guard.recording_start_guard_seconds,
            dead_state_pad_seconds=guard.dead_state_pad_seconds,
            max_clip_seconds=guard.max_clip_seconds,
            high_tier_max_seconds=guard.high_tier_max_seconds,
            low_tier_max_seconds=guard.low_tier_max_seconds,
            min_piece_seconds=guard.min_piece_seconds,
        )
        # The constitution is checked in `build_timeline`, on the selection
        # after overlapping context spans have been resolved -- here it
        # refused plans that were about to become legal one pass later.
        return guarded

    def _captions(self, context: WorkerContext, timeline: Timeline):
        """Transcript segments for every recording the edit draws on (§71).

        Unless the project said no: captions are a per-project choice made at
        the import screen, off by default. The transcript itself still exists
        -- the edit is built from what was said -- but nothing is written
        into the frame.
        """
        from backend.database.repositories.projects import ProjectRepository

        project = ProjectRepository(context.database).get(context.project_id)
        if project is not None and not project.captions_enabled:
            return []
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
        plan = EffectPlanner(context.config).plan(
            planned, intent, video_duration_seconds=timeline.duration
        )
        return self._composed(context, timeline, plan)

    def _composed(
        self, context: WorkerContext, timeline: Timeline, plan: EffectPlan
    ) -> EffectPlan:
        """Speak the composed sentences, and let the decorations fill in.

        Order matters and is the whole design. The flat planner scores every
        candidate independently and admits them best-first; a composition
        judged that way survives in pieces, and half a build-up rendered
        without its payoff is worse than no build-up -- noise wearing the
        shape of intent. So compositions are admitted first, atomically, and
        the decorations that would have landed on the same ground are dropped
        rather than layered over a gesture that is already speaking.

        Nothing here is required: with no library, no anchors or no reader the
        plan passes through exactly as the flat planner made it (§95).
        """
        from backend.emphasis import engine as emphasis
        from backend.emphasis.grammar import load_library

        library = load_library(context.config)
        if not library:
            return plan

        anchors = self._anchors(context, timeline)
        if not anchors:
            return plan

        minutes = max(timeline.duration / 60.0, 1.0 / 60.0)
        budget = max(1, int(context.config.effects.global_limits.max_effects_per_minute * minutes))
        spoken, refused = emphasis.compose(
            anchors,
            library,
            budget=budget,
            min_gap_seconds=context.config.compositions.min_gap_seconds,
            duration_seconds=timeline.duration,
        )
        if not spoken:
            return plan.model_copy(update={"rejected": [*plan.rejected, *refused]})

        composed = self._instances(spoken, timeline, config=context.config)
        occupied = [
            (item.start_seconds - 0.25, item.end_seconds + 0.25) for item in spoken
        ]
        kept = [
            instance
            for instance in plan.instances
            if not any(
                start <= instance.start_seconds <= end for start, end in occupied
            )
        ]
        dropped = len(plan.instances) - len(kept)
        merged = sorted(kept + composed, key=lambda item: item.start_seconds)
        return plan.model_copy(
            update={
                "instances": merged,
                "rejected": [
                    *plan.rejected,
                    *refused,
                    *(
                        [f"{dropped} decoration(s) dropped where a composition is speaking"]
                        if dropped
                        else []
                    ),
                ],
            }
        )

    def _anchors(self, context: WorkerContext, timeline: Timeline):
        """The beats worth building around, in timeline coordinates.

        Anchors are found in SOURCE time -- that is where events and phases
        live -- and translated onto the finished timeline, because an effect
        is placed in the video, not in the recording.
        """
        from backend.emphasis import engine as emphasis

        found = []
        for clip in timeline.video_clips():
            reader = self._reader(context, clip.media_id)
            events = [
                (
                    float(row["start_seconds"]),
                    float(row["end_seconds"]),
                    str(row["event_type"]),
                    float(row["importance"] or 0.5) * float(row["confidence"] or 0.5),
                )
                for row in context.database.fetch_all(
                    "SELECT start_seconds, end_seconds, event_type, importance, confidence "
                    "FROM game_events WHERE media_id = ? AND event_type != 'unknown_event' "
                    "AND start_seconds BETWEEN ? AND ?",
                    (clip.media_id, clip.source_in, clip.source_out),
                )
            ]
            phases = [
                (clip.moment_id or clip.id, phase["start_seconds"], phase["end_seconds"],
                 phase["confidence"], phase["name"])
                for phase in self._phases(context, clip)
            ]
            for anchor in emphasis.anchors_from(
                media_id=clip.media_id, events=events, phases=phases, reader=reader
            ):
                if not clip.source_in <= anchor.seconds <= clip.source_out:
                    continue
                from dataclasses import replace as _replace

                found.append(
                    _replace(
                        anchor,
                        seconds=clip.timeline_start + (anchor.seconds - clip.source_in),
                        clip_id=clip.id,
                        clip_start=clip.timeline_start,
                        clip_end=clip.timeline_end,
                    )
                )
        return found

    def _phases(self, context: WorkerContext, clip) -> list[dict]:
        """The stored phases of the moment this clip came from."""
        if not clip.moment_id:
            return []
        from json import loads

        row = context.database.fetch_one(
            "SELECT phases FROM moments WHERE id = ?", (clip.moment_id,)
        )
        if row is None or not row["phases"]:
            return []
        try:
            phases = loads(row["phases"])
        except ValueError:
            return []
        return [phase for phase in phases if isinstance(phase, dict)]

    def _reader(self, context: WorkerContext, media_id: str):
        try:
            from backend.database.repositories.media import MediaRepository
            from backend.semantic.timeline import load_timeline

            media = MediaRepository(context.database).get(media_id)
            duration = getattr(media.metadata, "duration_seconds", None) if media else None
            if not duration:
                return None
            return load_timeline(
                context.database,
                media_id,
                duration_seconds=float(duration),
                config=context.config,
            )
        except Exception:
            logger.exception("No reader for emphasis; compositions stand down")
            return None

    def _instances(self, spoken, timeline, *, config) -> list[EffectInstance]:
        """Composition members as the effect rows the renderer already draws.

        Each member is bound to whichever shot contains its own placement,
        not to the anchor's shot: a sentence is a gesture over the video, and
        with climax shots under two seconds it necessarily crosses cuts.
        """
        shots = sorted(
            timeline.video_clips(), key=lambda clip: clip.timeline_start
        )
        rows: list[EffectInstance] = []
        shapes = _effect_shapes(config)
        for planned in spoken:
            for member, seconds in planned.placements:
                definition = shapes.get(member.effect)
                if definition is None:
                    continue
                engine, category = definition
                rows.append(
                    EffectInstance(
                        effect=member.effect,
                        engine=engine,
                        category=category,
                        start_seconds=max(0.0, seconds),
                        duration_seconds=max(0.05, member.duration_seconds),
                        strength=member.strength,
                        clip_id=_shot_at(shots, seconds),
                        moment_id=planned.anchor.moment_id,
                        params={
                            "composition_id": planned.composition_id,
                            "group_role": member.role,
                            "anchor_seconds": round(planned.anchor.seconds, 3),
                            "offset_seconds": member.offset_seconds,
                        },
                        reason=planned.reason,
                    )
                )
        return sorted(rows, key=lambda row: row.start_seconds)


def _shot_at(shots, seconds: float) -> str | None:
    """The clip covering ``seconds`` on the finished timeline, or ``None``."""
    for clip in shots:
        if clip.timeline_start <= seconds < clip.timeline_end:
            return clip.id
    return shots[-1].id if shots else None


def _effect_shapes(config) -> dict:
    """Which renderer draws each effect, and what family it belongs to.

    Read from the shipped effects library rather than restated here, so a
    composition can never name an engine the effects configuration disagrees
    with -- and an effect the library does not define is simply not placeable.
    """
    from backend.core.models.enums import EffectCategory, EffectEngine, EffectType

    shapes: dict = {}
    for name, spec in config.effects.library.items():
        try:
            shapes[EffectType(name)] = (
                EffectEngine(spec.engine),
                EffectCategory(spec.category),
            )
        except ValueError:
            continue
    return shapes


__all__ = ["EdlWorker"]


def _spoken_words(database, media_id: str) -> list[tuple[float, float]]:
    """Every word's span, so a cut never lands inside one.

    §14 stored these from the first transcript and nothing had read them for
    this purpose. They are the only speech boundary fine enough to protect:
    the segments around them are minutes long on real gameplay.
    """
    from json import loads

    spans: list[tuple[float, float]] = []
    for row in database.fetch_all(
        "SELECT words FROM transcript_segments WHERE media_id = ? ORDER BY start_seconds",
        (media_id,),
    ):
        if not row["words"]:
            continue
        try:
            words = loads(row["words"])
        except ValueError:
            continue
        for word in words if isinstance(words, list) else ():
            start, end = word.get("start"), word.get("end")
            if start is None or end is None or end <= start:
                continue
            spans.append((float(start), float(end)))
    spans.sort()
    return spans


def _lane_peaks(timeline) -> list[float]:
    """Local maxima of the fused intensity lane, as candidate seam times."""
    lane = timeline.lanes.get("intensity", [])
    step = 1.0 / timeline.hz
    return [
        index * step
        for index in range(1, len(lane) - 1)
        if lane[index] > lane[index - 1] and lane[index] >= lane[index + 1]
    ]
