"""The CRITIC2 stage — watch the finished video, then fix it once (V2-P7).

The Critic this pipeline already had reads a numbered list of clips before
anything is rendered. That placement is right for what it does and it means
nothing has ever looked at the object a viewer meets: the defects that live in
the *assembly* are invisible by construction to every stage that reads the
source.

This one runs twice, by design, and the pass counter is the safety.

    pass 1  watch V1, correct it, re-render
    pass 2  compare, and roll back if the correction made it worse

Three locks, each of which exists because its absence is a way for an
automatic re-edit to go wrong:

* **one cycle.** A revision counter on the timeline; V2 never produces a V3.
  Open loops drift rather than improve.
* **no reordering.** The correction vocabulary has no verb for it, and the
  chronology check runs again after the edit is applied.
* **no degradation.** If the corrected video scores lower than the one it
  replaced, the previous edit is restored and the corrections are recorded as
  refused. A change that cannot be shown to help is not an improvement.
"""

from __future__ import annotations

from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.workers.critic2", LogChannel.QA)

#: How many frames of the render are looked at. A uniform sample, because the
#: defects this stage exists for -- repetition, fatigue -- are about what
#: recurs, and a sample biased toward the interesting parts cannot see them.
MAX_FRAMES: Final[int] = 60

#: The quality score must not fall by more than this for the corrected edit to
#: stand. Exactly zero would make any rounding a rollback.
TOLERANCE: Final[float] = 0.5


class Critic2Worker:
    """CRITIC2 -- the first stage that watches the video it made."""

    stage = JobStage.CRITIC2

    def __init__(self, vision_provider: Any = None) -> None:
        self._vision = vision_provider

    def run(self, context: WorkerContext) -> dict[str, Any]:
        repository = TimelineRepository(context.database)
        timeline = repository.load(context.project_id)
        clips = timeline.video_clips()
        if not clips:
            context.report(1.0, "Nothing to watch")
            return {"skipped": True, "reason": "no clips"}

        spent = self._spent(context)
        if spent is not None:
            return self._second_pass(context, spent)

        render = self._render(context)
        if render is None:
            context.report(1.0, "No render to watch")
            return {"skipped": True, "reason": "no render"}

        context.report(0.2, "Watching the finished video")
        found, looks = self._watch(context, timeline, render, clips)

        from backend.critic2.watch import corrections
        from backend.semantic.levels import floor_for

        levels = self._levels(context, timeline)
        made, refused = corrections(
            found,
            clips=clips,
            effects=self._effects(context),
            floor_for=lambda clip_id: floor_for(levels.get(clip_id), context.config),
            style=self._style(context),
        )
        result = {
            "watched_frames": sum(len(names) for names in looks.values()),
            "findings": [item.as_dict() for item in found],
            "corrections": [item.as_dict() for item in made],
            "refused": refused,
            "revision": 0,
            "v1_quality_score": self._quality(context),
        }
        if not made:
            context.report(1.0, f"{len(found)} finding(s), nothing to correct")
            return result

        context.report(0.8, f"Applying {len(made)} correction(s)")
        applied = self._apply(context, repository, timeline, made)
        result["applied"] = applied
        if applied:
            self._requeue(context)
            result["revision"] = 1
        context.report(1.0, f"{applied} correction(s) applied; re-rendering")
        return result

    # -- pass two ---------------------------------------------------------

    def _spent(self, context: WorkerContext) -> float | None:
        """The score before correction, if the one permitted cycle was used.

        Reading this from the database rather than from ``Timeline.metadata``
        is the whole of the one-cycle lock: the repository loads a timeline
        from its clip rows and saves clips back, so anything written to the
        timeline's own metadata is discarded, and a counter kept there read
        zero on every run.
        """
        row = context.database.fetch_one(
            "SELECT quality_before FROM critic2_snapshots "
            "WHERE project_id = ? ORDER BY revision DESC LIMIT 1",
            (context.project_id,),
        )
        return float(row["quality_before"]) if row is not None else None

    def _second_pass(self, context: WorkerContext, before: float) -> dict[str, Any]:
        """Keep the corrected edit only if it is actually better.

        A change that cannot be shown to help is not an improvement, and an
        automatic editor that cannot tell the difference will drift.
        """
        after = self._quality(context)
        kept = after + TOLERANCE >= before
        already = self._already_restored(context)
        restored = None if kept else (False if already else self._restore(context))
        if not kept:
            logger.warning(
                "The corrected edit scored lower than the one it replaced",
                extra={
                    "before": before,
                    "after": after,
                    "restored": restored,
                    "already_restored": already,
                },
            )
            if restored:
                # The restored edit is not the one that was just rendered, so
                # the render and its checks have to run once more. That render
                # brings this stage back, which is why a restore happens once:
                # the second time through, ``already`` is true and the pair
                # stops here instead of trading work for ever.
                self._requeue(context)
        context.report(
            1.0,
            f"quality {before:.0f} -> {after:.0f}" + ("" if kept else "; rolled back"),
        )
        return {
            "revision": 1,
            "v1_quality_score": before,
            "v2_quality_score": after,
            "kept": kept,
            "restored": restored,
            "note": (
                "the corrected edit stands"
                if kept
                else (
                    "the corrections made it worse and were reverted"
                    if restored
                    else (
                        "the previous edit was already restored; this is the "
                        "render of it"
                        if already
                        else "the corrections made it worse and could not be reverted"
                    )
                )
            ),
        }

    # -- looking ----------------------------------------------------------

    def _watch(self, context: WorkerContext, timeline, render, clips):
        """Frames of the RENDER, described, and everything measurable."""
        from backend.critic2 import frames as sampling
        from backend.critic2.watch import findings

        looks = sampling.describe(
            context,
            render_path=render,
            clips=clips,
            provider=self._vision,
            max_frames=MAX_FRAMES,
        )
        reader = self._programme(context, timeline)
        found = findings(
            clips=clips,
            looks=looks,
            reader=reader,
            effects=self._effects(context),
            planned_silences=self._planned_silences(context),
            duration_seconds=timeline.duration,
            style=self._style(context),
        )
        return found, looks

    def _programme(self, context: WorkerContext, timeline):
        try:
            from backend.database.repositories.media import MediaRepository
            from backend.semantic.programme import ProgrammeReader
            from backend.semantic.timeline import load_timeline

            media_repository = MediaRepository(context.database)
            readers = {}
            for media_id in timeline.media_ids():
                media = media_repository.get(media_id)
                duration = (
                    getattr(media.metadata, "duration_seconds", None) if media else None
                )
                if duration:
                    readers[media_id] = load_timeline(
                        context.database,
                        media_id,
                        duration_seconds=float(duration),
                        config=context.config,
                    )
            return ProgrammeReader.build(
                timeline.video_clips(),
                readers,
                duration_seconds=timeline.duration,
                config=context.config,
            )
        except Exception:
            logger.exception("No programme lanes; the critic watches without them")
            return None

    # -- the edit ---------------------------------------------------------

    def _apply(self, context: WorkerContext, repository, timeline, made) -> int:
        """Apply the corrections, then check the constitution again.

        The vocabulary has no verb for reordering, but a trim that moves a
        clip's span is still an edit, and V2's rule is checked on the result
        rather than assumed from the inputs.
        """
        from backend.timeline import operations
        from backend.timeline.validation import ensure_chronological

        if made and not self._snapshot(context, self._quality(context)):
            logger.error(
                "Nothing was corrected: the edit before it could not be stored",
                extra={"project_id": context.project_id, "corrections": len(made)},
            )
            return 0
        edited = timeline
        applied = 0
        removed = 0
        refused = 0
        for correction in made:
            try:
                if correction.action == "trim_end":
                    edited = operations.trim(
                        edited, correction.target, end_delta=-correction.amount
                    )
                elif correction.action == "trim_start":
                    edited = operations.trim(
                        edited, correction.target, start_delta=correction.amount
                    )
                elif correction.action == "drop":
                    edited = operations.delete(edited, correction.target)
                elif correction.action == "remove_effect":
                    gone = self._remove_effect(context, correction.target)
                    removed += gone
                    if not gone:
                        # A refusal is not an application. Counting it as one
                        # is how nine removals of nothing were reported as
                        # nine corrections applied.
                        refused += 1
                        continue
                    applied += 1
                    continue
                else:
                    continue
            except Exception as error:
                logger.info(
                    "A correction was refused by the timeline",
                    extra={"target": correction.target, "reason": str(error)[:160]},
                )
                continue
            applied += 1

        if refused:
            logger.info(
                "Corrections that named a target the database no longer has",
                extra={"refused": refused, "applied": applied},
            )
        if not applied:
            # The cycle is spent by a correction, not by an attempt. Leaving
            # the snapshot here would retire the stage for this project
            # without a single frame of the video having changed.
            self._forget(context)
            return 0
        if applied == removed:
            # Only effects changed, so the timeline itself is untouched. The
            # render still has to be made again, and the snapshot written
            # above is what records that the cycle was spent.
            return applied
        ensure_chronological(edited.video_clips())
        repository.save_edit(context.project_id, edited)
        return applied

    def _remove_effect(self, context: WorkerContext, effect_id: str) -> int:
        """Take one placed effect out of the edit.

        A composition member is never a target: the engine that placed it
        admits a sentence whole or not at all, and pulling one word out is
        the exact failure P4 exists to prevent. The guard is here as well as
        in the chooser, because this is where the row actually dies.
        """
        row = context.database.fetch_one(
            "SELECT composition_id FROM timeline_effects WHERE id = ?", (effect_id,)
        )
        if row is None:
            return 0
        if row["composition_id"]:
            logger.info(
                "A composed effect was not removed; a sentence stays whole",
                extra={"effect_id": effect_id, "composition": row["composition_id"]},
            )
            return 0
        context.database.execute(
            "DELETE FROM timeline_effects WHERE id = ?", (effect_id,)
        )
        return 1

    def _snapshot(self, context: WorkerContext, quality_before: float) -> bool:
        """Store the edit exactly as it stands, before anything is changed.

        Written rather than derived: the second pass runs after a re-render
        and cannot reconstruct what was there from what is there now.
        """
        from datetime import datetime, timezone

        from backend.core.ids import new_id
        from backend.database.connection import dumps

        clips = [
            dict(row)
            for row in context.database.fetch_all(
                "SELECT * FROM timeline_clips WHERE project_id = ?",
                (context.project_id,),
            )
        ]
        effects = [
            dict(row)
            for row in context.database.fetch_all(
                "SELECT * FROM timeline_effects WHERE project_id = ?",
                (context.project_id,),
            )
        ]
        try:
            context.database.execute(
                "INSERT OR REPLACE INTO critic2_snapshots "
                "(id, project_id, revision, quality_before, clips, effects, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("job").replace("job-", "snap-"),
                    context.project_id,
                    1,
                    float(quality_before),
                    dumps(clips),
                    dumps(effects),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception:
            # The caller refuses to correct anything without this row: an
            # unrevertable automatic edit is worse than an uncorrected one.
            logger.exception("The pre-correction edit could not be stored")
            return False
        logger.info(
            "The edit before correction was stored",
            extra={"clips": len(clips), "effects": len(effects)},
        )
        return True

    def _forget(self, context: WorkerContext) -> None:
        """Drop a snapshot that turned out to have nothing behind it."""
        context.database.execute(
            "DELETE FROM critic2_snapshots WHERE project_id = ? AND restored_at IS NULL",
            (context.project_id,),
        )

    def _already_restored(self, context: WorkerContext) -> bool:
        """Whether the one rollback this stage is allowed has been used."""
        row = context.database.fetch_one(
            "SELECT restored_at FROM critic2_snapshots "
            "WHERE project_id = ? ORDER BY revision DESC LIMIT 1",
            (context.project_id,),
        )
        return bool(row and row["restored_at"])

    def _restore(self, context: WorkerContext) -> bool:
        """Put the pre-correction edit back, clips and effects both.

        By UPDATE and INSERT, never REPLACE: captions and effects reference
        ``timeline_clips`` with ON DELETE CASCADE, and REPLACE deletes before
        it inserts, so restoring a clip that way would silently take the
        captions of every surviving clip with it.
        """
        from backend.database.connection import loads

        row = context.database.fetch_one(
            "SELECT id, clips, effects, restored_at FROM critic2_snapshots "
            "WHERE project_id = ? ORDER BY revision DESC LIMIT 1",
            (context.project_id,),
        )
        if row is not None and row["restored_at"]:
            # Once. The second pass re-queues the render after a rollback, and
            # that render brings this stage back: without this the pair would
            # trade a restore for a re-render for ever.
            logger.info(
                "The edit was already restored once; nothing further is done",
                extra={"project_id": context.project_id, "at": row["restored_at"]},
            )
            return False
        if row is None:
            logger.error(
                "The corrected edit is worse and there is nothing to restore",
                extra={"project_id": context.project_id},
            )
            return False
        clips = loads(row["clips"]) or []
        effects = loads(row["effects"]) or []
        try:
            with context.database.transaction():
                self._restore_rows(context, "timeline_clips", clips, park="clip_index")
                self._restore_rows(context, "timeline_effects", effects)
        except Exception:
            logger.exception("The edit could not be restored")
            return False
        from datetime import datetime, timezone

        context.database.execute(
            "UPDATE critic2_snapshots SET restored_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
        logger.info(
            "The pre-correction edit was restored",
            extra={"clips": len(clips), "effects": len(effects)},
        )
        return True

    def _restore_rows(
        self, context: WorkerContext, table: str, rows: list, *, park: str | None = None
    ) -> None:
        """Write stored rows back into ``table`` for this project.

        ``park`` names a column under a unique index. Every current row is
        moved far out of its way first, so restoring an index a surviving row
        currently holds cannot collide half way through.
        """
        if not rows:
            return
        database = context.database
        if park:
            database.execute(
                f"UPDATE {table} SET {park} = {park} + 1000000 WHERE project_id = ?",
                (context.project_id,),
            )
        present = {
            item["id"]
            for item in database.fetch_all(
                f"SELECT id FROM {table} WHERE project_id = ?", (context.project_id,)
            )
        }
        columns = [name for name in rows[0] if name != "id"]
        assignments = ", ".join(f"{name} = ?" for name in columns)
        placeholders = ", ".join("?" for _ in range(len(columns) + 1))
        for stored in rows:
            values = [stored[name] for name in columns]
            if stored["id"] in present:
                database.execute(
                    f"UPDATE {table} SET {assignments} WHERE id = ?",
                    (*values, stored["id"]),
                )
            else:
                database.execute(
                    f"INSERT INTO {table} (id, {', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    (stored["id"], *values),
                )
        kept = {stored["id"] for stored in rows}
        for extra in present - kept:
            database.execute(f"DELETE FROM {table} WHERE id = ?", (extra,))

    def _requeue(self, context: WorkerContext) -> None:
        """Send the render and its checks round once more.

        Not this stage: a job cannot re-queue itself while it is running. The
        first version of this comment claimed the runner would bring CRITIC2
        back "by construction" because it depends on QA -- it does not. A
        dependency gates whether a stage *may* run, not whether a completed
        job returns to the queue, so the second pass never happened outside a
        hand-written script and the no-degradation lock never fired on a real
        video. :func:`owes_a_second_look` is the condition the runner checks
        when QA finishes, and it is what closes the loop.
        """
        from backend.services.job_manager import JobManager

        manager = JobManager(context.database, context.config)
        for job in manager.list_jobs(context.project_id):
            if job.stage in (JobStage.RENDER, JobStage.QA):
                manager.requeue(job.id)

    # -- reading what is already stored -----------------------------------

    def _render(self, context: WorkerContext):
        from pathlib import Path

        job = next(
            (
                item
                for item in JobRepository(context.database).list_for_project(
                    context.project_id
                )
                if item.stage is JobStage.RENDER and (item.result or {}).get("output_path")
            ),
            None,
        )
        if job is None:
            return None
        path = Path((job.result or {})["output_path"])
        return path if path.is_file() else None

    def _quality(self, context: WorkerContext) -> float:
        job = next(
            (
                item
                for item in JobRepository(context.database).list_for_project(
                    context.project_id
                )
                if item.stage is JobStage.QA and item.result
            ),
            None,
        )
        return float((job.result or {}).get("quality_score", 0.0)) if job else 0.0

    def _style(self, context: WorkerContext):
        """The taste that made this video, not the one the brief names now.

        A person who switches preset after a render has not changed the file
        on disk, and judging it by the new style would report defects the edit
        was never trying to avoid.
        """
        from backend.style import bible as style_bible

        return style_bible.for_project(
            context.database, context.config, context.project_id
        )

    def _effects(self, context: WorkerContext):
        """Placed effects, in programme time, each with its row id.

        Not :meth:`list_effects`: that returns the planner's view, whose times
        are clip-relative and whose objects have no identity to name.
        """
        return TimelineRepository(context.database).list_placed(context.project_id)

    def _levels(self, context: WorkerContext, timeline) -> dict[str, str]:
        from backend.semantic.levels import clip_levels

        by_index = clip_levels(
            context.database, timeline, config=context.config
        )
        return {
            clip.id: by_index.get(clip.clip_index, "normal")
            for clip in timeline.video_clips()
        }

    def _planned_silences(self, context: WorkerContext):
        job = next(
            (
                item
                for item in JobRepository(context.database).list_for_project(
                    context.project_id
                )
                if item.stage is JobStage.RENDER and item.result
            ),
            None,
        )
        planned = (job.result or {}).get("planned_silences") if job else None
        return [tuple(item) for item in planned or []]


def owes_a_second_look(database: Any, project_id: str) -> bool:
    """Whether the critic corrected an edit and has not yet judged the result.

    True exactly between the two passes: a snapshot exists, so corrections
    were applied and re-rendered, and the stored result has no verdict in it
    yet. Pass two always writes ``kept``, so this can never be true twice for
    the same correction -- which is what stops the pair from trading a render
    for a re-render for ever.
    """
    try:
        snapshot = database.fetch_one(
            "SELECT 1 AS present FROM critic2_snapshots WHERE project_id = ?",
            (project_id,),
        )
        if snapshot is None:
            return False
        row = database.fetch_one(
            "SELECT status, result FROM analysis_jobs "
            "WHERE project_id = ? AND stage = 'critic2'",
            (project_id,),
        )
    except Exception:
        logger.exception("Whether a second look is owed could not be read")
        return False
    if row is None or row["status"] != "completed":
        return False
    from backend.database.connection import loads

    return "kept" not in (loads(row["result"] or "{}") or {})


__all__ = ["MAX_FRAMES", "Critic2Worker", "owes_a_second_look"]
