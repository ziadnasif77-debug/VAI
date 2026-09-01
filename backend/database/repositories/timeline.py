"""Timeline persistence (SPEC §40–§45, §78).

The timeline is stored as rows rather than as a JSON blob because §45 makes it
a set of entities, and because the interaction layer edits individual clips —
"remove the bit at 4:12" is an update to one row, not a rewrite of the edit.

One rule here is not obvious from the schema. **A rebuild preserves what the
user decided.** §78 gives the user the last word, so re-running the EDL stage
after a re-analysis must not silently re-enable a clip they removed. Clip ids
are derived from the source span rather than generated, so the same moment
produces the same id across runs and the disabled flag survives the round trip.
The moments repository preserves ``user_state`` for the same reason and in the
same way; both would be pointless if the pipeline could overwrite a decision by
re-running.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from backend.core.ids import derived_id
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import (
    EffectCategory,
    EffectEngine,
    EffectType,
    MomentType,
    TrackKind,
    TransitionType,
)
from backend.database.connection import Database, dumps, loads
from backend.effects.models import EffectInstance, PlacedEffect
from backend.timeline import operations
from backend.timeline.captions import Caption
from backend.timeline.models import Timeline, TimelineClip, Track

logger = get_logger("database.timeline", LogChannel.APPLICATION)

_CLIP_COLUMNS = (
    "id, project_id, media_id, moment_id, track, clip_index, source_in, source_out, "
    "timeline_start, timeline_end, speed, transition_in, transition_out, enabled, metadata"
)

_EFFECT_COLUMNS = (
    "id, project_id, clip_id, effect_type, start_seconds, duration_seconds, "
    "parameters, enabled, composition_id, group_role, anchor_seconds, offset_seconds"
)

#: Indices are parked above this while a reorder is in flight, so the unique
#: index cannot fire on an intermediate state. Far above any real clip count.
_INDEX_PARKING_OFFSET = 1_000_000

_CAPTION_COLUMNS = (
    "id, project_id, clip_id, caption_index, timeline_start, timeline_end, text, "
    "language, words"
)


class TimelineRepository:
    """CRUD for ``timeline_clips``, ``timeline_effects`` and ``captions``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- write ----------------------------------------------------------

    def replace(
        self,
        project_id: str,
        timeline: Timeline,
        *,
        captions: Sequence[Caption] = (),
        effects: Sequence[EffectInstance] = (),
        preserve_user_state: bool = True,
    ) -> int:
        """Replace a project's timeline.

        Args:
            preserve_user_state: keep the stored enable/disable decisions
                instead of the incoming ones. True for a **rebuild** -- the
                pipeline re-running must not re-enable a clip the user removed
                (§78). False for a **save** -- an edit the user just made is the
                decision, and preserving the old state would revert it. Getting
                this backwards makes one of those two impossible, so it is a
                parameter rather than a policy.

        Returns:
            The number of clips written.
        """
        preserved = self._enabled_states(project_id) if preserve_user_state else {}
        rows = [
            {
                **_clip_row(project_id, clip),
                # The stored decision wins over the freshly built default.
                "enabled": int(preserved.get(clip.id, clip.enabled)),
            }
            for clip in timeline.clips
        ]

        with self._db.transaction():
            # Captions and effects reference clips, and their rows cascade, so
            # the clip delete has to come first or the inserts race the cascade.
            self._db.execute("DELETE FROM timeline_clips WHERE project_id = ?", (project_id,))
            self._db.execute("DELETE FROM timeline_effects WHERE project_id = ?", (project_id,))
            self._db.execute("DELETE FROM captions WHERE project_id = ?", (project_id,))
            if rows:
                self._db.executemany(
                    f"INSERT INTO timeline_clips ({_CLIP_COLUMNS}) VALUES ("
                    ":id, :project_id, :media_id, :moment_id, :track, :clip_index, "
                    ":source_in, :source_out, :timeline_start, :timeline_end, :speed, "
                    ":transition_in, :transition_out, :enabled, :metadata)",
                    rows,
                )
            self._write_captions(project_id, captions)
            self._write_effects(project_id, timeline, effects)
        return len(rows)

    def save_edit(self, project_id: str, timeline: Timeline) -> int:
        """Persist an edit to an existing timeline, carrying its captions along.

        Distinct from :meth:`replace`, which rebuilds. This *syncs*: a row that
        survives is updated in place, so the captions and effects hanging off it
        survive too -- deleting and re-inserting would cascade those away, and
        re-deriving them needs the transcript this layer does not have.

        "Update the positions" is not enough, and the first version of this
        method did only that. A split adds a clip and a trim changes a source
        span, so a split wrote a row whose stored source span no longer matched
        its timeline span -- which the model then refused to load. Inserts and
        removals are handled here, and the source points are written with the
        rest.

        Captions are stored in timeline coordinates, so a clip that moves takes
        its captions with it: each one shifts by exactly its clip's delta. That
        keeps §71's guarantee true after an edit as well as after a build --
        timing still derives from the transcript, translated by the same offset
        as the footage it belongs to.

        Effects need no shift: they are stored relative to their clip.

        Returns:
            The number of clips updated.
        """
        before = {
            row["id"]: float(row["timeline_start"])
            for row in self._db.fetch_all(
                "SELECT id, timeline_start FROM timeline_clips WHERE project_id = ?",
                (project_id,),
            )
        }
        surviving = {clip.id for clip in timeline.clips}
        written = 0
        with self._db.transaction():
            # A clip the edit removed takes its captions and effects with it,
            # which is right: they described footage that is no longer there.
            for clip_id in set(before) - surviving:
                self._db.execute(
                    "DELETE FROM timeline_clips WHERE id = ? AND project_id = ?",
                    (clip_id, project_id),
                )

            # ``UNIQUE (project_id, track, clip_index)`` is checked per
            # statement, so reordering row by row collides the moment two clips
            # want to swap places. Parking every index out of range first makes
            # the intermediate state legal; the transaction means nothing ever
            # observes it.
            self._db.execute(
                "UPDATE timeline_clips SET clip_index = clip_index + ? WHERE project_id = ?",
                (_INDEX_PARKING_OFFSET, project_id),
            )
            for clip in timeline.clips:
                if clip.id in before:
                    self._db.execute(
                        "UPDATE timeline_clips SET clip_index = ?, source_in = ?, "
                        "source_out = ?, timeline_start = ?, timeline_end = ?, "
                        "enabled = ? WHERE id = ? AND project_id = ?",
                        (
                            clip.clip_index,
                            clip.source_in,
                            clip.source_out,
                            clip.timeline_start,
                            clip.timeline_end,
                            int(clip.enabled),
                            clip.id,
                            project_id,
                        ),
                    )
                else:
                    # A split's second half: a new row, with no captions of its
                    # own. The half it came from keeps them.
                    self._db.execute(
                        f"INSERT INTO timeline_clips ({_CLIP_COLUMNS}) VALUES ("
                        ":id, :project_id, :media_id, :moment_id, :track, :clip_index, "
                        ":source_in, :source_out, :timeline_start, :timeline_end, "
                        ":speed, :transition_in, :transition_out, :enabled, :metadata)",
                        _clip_row(project_id, clip),
                    )
                written += 1
                delta = clip.timeline_start - before.get(clip.id, clip.timeline_start)
                if abs(delta) > 1e-9:
                    self._db.execute(
                        "UPDATE captions SET timeline_start = timeline_start + ?, "
                        "timeline_end = timeline_end + ? WHERE clip_id = ? AND project_id = ?",
                        (delta, delta, clip.id, project_id),
                    )
        return written

    def set_enabled(self, project_id: str, clip_id: str, *, enabled: bool) -> bool:
        """Enable or disable one clip and re-flow the timeline (§78).

        The re-flow is the point. A raw flag update leaves the following clips
        where they were, so the finished video carries a hole exactly the length
        of the clip the user removed -- which the validator correctly calls a
        gap, and the renderer would draw as black frames.

        Returns whether anything changed.
        """
        timeline = self.load(project_id)
        if timeline.clip(clip_id) is None:
            return False
        edited = operations.set_enabled(timeline, clip_id, enabled=enabled)
        if edited is timeline:
            return False
        self.save_edit(project_id, edited)
        return True

    def apply_enabled_states(self, project_id: str, states: Mapping[str, bool]) -> int:
        """Apply many enable decisions at once, re-flowing once at the end.

        Used by undo (§78), which restores a whole snapshot: re-flowing per clip
        would be both slower and wrong, since intermediate states can be
        inconsistent with the snapshot being restored.
        """
        timeline = self.load(project_id)
        changed = 0
        for clip_id, enabled in states.items():
            clip = timeline.clip(clip_id)
            if clip is None or clip.enabled == enabled:
                continue
            timeline = operations.set_enabled(timeline, clip_id, enabled=enabled)
            changed += 1
        if changed:
            self.save_edit(project_id, timeline)
        return changed

    def delete_for_project(self, project_id: str) -> int:
        with self._db.transaction():
            cursor = self._db.execute(
                "DELETE FROM timeline_clips WHERE project_id = ?", (project_id,)
            )
            self._db.execute("DELETE FROM timeline_effects WHERE project_id = ?", (project_id,))
            self._db.execute("DELETE FROM captions WHERE project_id = ?", (project_id,))
        return cursor.rowcount

    # -- read -----------------------------------------------------------

    def load(self, project_id: str) -> Timeline:
        """Rebuild the timeline object from its rows.

        The inverse of :meth:`replace`, and the reason §127's re-edit is cheap:
        a user changing one clip loads this, edits it and re-renders, without
        any stage upstream running again.
        """
        rows = self._db.fetch_all(
            f"SELECT {_CLIP_COLUMNS} FROM timeline_clips WHERE project_id = ? "
            "ORDER BY track, clip_index",
            (project_id,),
        )
        by_track: dict[TrackKind, list[TimelineClip]] = {}
        for row in rows:
            clip = _clip(row)
            by_track.setdefault(clip.track, []).append(clip)

        timeline = Timeline(project_id=project_id)
        for kind, clips in by_track.items():
            timeline = timeline.with_track(Track(kind=kind, clips=tuple(clips)))
        return timeline

    def list_clips(
        self, project_id: str, *, track: TrackKind = TrackKind.VIDEO, enabled_only: bool = False
    ) -> list[TimelineClip]:
        sql = (
            f"SELECT {_CLIP_COLUMNS} FROM timeline_clips "
            "WHERE project_id = ? AND track = ?"
        )
        parameters: list[object] = [project_id, track.value]
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY clip_index"
        return [_clip(row) for row in self._db.fetch_all(sql, parameters)]

    def clip_count(self, project_id: str, *, enabled_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS total FROM timeline_clips WHERE project_id = ?"
        if enabled_only:
            sql += " AND enabled = 1"
        row = self._db.fetch_one(sql, (project_id,))
        return int(row["total"]) if row is not None else 0

    def duration_seconds(self, project_id: str) -> float:
        """The finished video's length: enabled video clips only."""
        row = self._db.fetch_one(
            "SELECT COALESCE(SUM(timeline_end - timeline_start), 0.0) AS total "
            "FROM timeline_clips WHERE project_id = ? AND track = 'video' AND enabled = 1",
            (project_id,),
        )
        return float(row["total"]) if row is not None else 0.0

    def list_captions(self, project_id: str) -> list[Caption]:
        rows = self._db.fetch_all(
            f"SELECT {_CAPTION_COLUMNS} FROM captions WHERE project_id = ? "
            "ORDER BY caption_index",
            (project_id,),
        )
        return [_caption(row) for row in rows]

    def caption_count(self, project_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM captions WHERE project_id = ?", (project_id,)
        )
        return int(row["total"]) if row is not None else 0

    def effect_count(self, project_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM timeline_effects WHERE project_id = ?",
            (project_id,),
        )
        return int(row["total"]) if row is not None else 0

    def list_placed(self, project_id: str) -> list[PlacedEffect]:
        """Effects in programme time, each with the id needed to remove it.

        :meth:`list_effects` returns them as the planner stored them, which
        means clip-relative times and no row identity -- correct for the
        renderers, which resolve both from the clip they are drawing, and
        useless to anything reading the finished video.

        The join is the whole point: ``timeline_start + start_seconds`` is
        where a viewer actually meets the effect.
        """
        rows = self._db.fetch_all(
            "SELECT e.id AS id, e.clip_id AS clip_id, e.effect_type AS effect_type, "
            "e.start_seconds AS start_seconds, e.duration_seconds AS duration_seconds, "
            "e.parameters AS parameters, e.composition_id AS composition_id, "
            "e.group_role AS group_role, c.timeline_start AS clip_start "
            "FROM timeline_effects e "
            "LEFT JOIN timeline_clips c ON c.id = e.clip_id "
            "WHERE e.project_id = ? AND e.enabled = 1",
            (project_id,),
        )
        placed: list[PlacedEffect] = []
        for row in rows:
            base = float(row["clip_start"] or 0.0) if row["clip_id"] else 0.0
            try:
                parameters = loads(row["parameters"] or "{}") or {}
            except Exception:
                parameters = {}
            try:
                placed.append(
                    PlacedEffect(
                        id=str(row["id"]),
                        effect=EffectType(row["effect_type"]),
                        timeline_start=max(0.0, base + float(row["start_seconds"])),
                        duration_seconds=float(row["duration_seconds"]),
                        clip_id=row["clip_id"],
                        composition_id=row["composition_id"],
                        group_role=row["group_role"],
                        strength=float(parameters.get("strength", 1.0) or 1.0),
                    )
                )
            except Exception:
                logger.warning(
                    "A stored effect could not be placed on the timeline",
                    extra={"effect_id": row["id"], "type": row["effect_type"]},
                )
        placed.sort(key=lambda item: item.timeline_start)
        return placed

    def list_effects(self, project_id: str) -> list[EffectInstance]:
        """The stored effect plan, ready for either renderer.

        The writer existed from Phase 8 and nothing ever read the rows back:
        seventeen planned effects across three real projects reached the
        database and not one reached the finished video. This is the reader.

        Times come back exactly as stored -- clip-relative when ``clip_id`` is
        set -- which is the convention :class:`EffectInstance` documents and
        both renderers expect. Rows that no longer parse (an effect type or
        engine removed from the library) are skipped with a warning rather than
        failing the render: a stale decoration is not worth losing the video.
        """
        rows = self._db.fetch_all(
            f"SELECT {_EFFECT_COLUMNS} FROM timeline_effects "
            "WHERE project_id = ? AND enabled = 1 "
            "ORDER BY start_seconds",
            (project_id,),
        )
        effects: list[EffectInstance] = []
        for row in rows:
            params = loads(row["parameters"]) or {}
            try:
                strength = params.pop("instance_strength", None)
                if strength is None:
                    # Rows written before the reserved key existed carried the
                    # instance strength under "strength" -- clobbering any
                    # like-named library magnitude, which is why the key moved.
                    # Popped, not read: leaving it in params would hand the
                    # instance value to a renderer as if it were the magnitude,
                    # and make the same effect hash differently before and
                    # after a re-plan rewrites the row.
                    strength = params.pop("strength", 1.0)
                effects.append(
                    EffectInstance(
                        effect=EffectType(row["effect_type"]),
                        engine=EffectEngine(params.pop("engine")),
                        category=EffectCategory(params.pop("category")),
                        start_seconds=max(0.0, float(row["start_seconds"])),
                        duration_seconds=float(row["duration_seconds"]),
                        clip_id=row["clip_id"],
                        params=params,
                        strength=min(1.0, max(0.0, float(strength))),
                        reason=str(params.pop("reason", "")),
                    )
                )
            except (KeyError, ValueError, TypeError) as error:
                logger.warning(
                    "Skipped a stored effect row that no longer parses",
                    extra={"effect_id": row["id"], "error": str(error)[:120]},
                )
        return effects

    # -- internals ------------------------------------------------------

    def _enabled_states(self, project_id: str) -> dict[str, bool]:
        """The user's current enable decisions, by clip id (§78)."""
        rows = self._db.fetch_all(
            "SELECT id, enabled FROM timeline_clips WHERE project_id = ?", (project_id,)
        )
        return {row["id"]: bool(row["enabled"]) for row in rows}

    def _write_captions(self, project_id: str, captions: Iterable[Caption]) -> None:
        rows = []
        for caption in captions:
            row = caption.as_row()
            rows.append(
                {
                    "id": row["id"],
                    "project_id": project_id,
                    "clip_id": row["clip_id"],
                    "caption_index": row["caption_index"],
                    "timeline_start": row["timeline_start"],
                    "timeline_end": row["timeline_end"],
                    "text": row["text"],
                    "language": row["language"],
                    "words": dumps(row["words"]),
                }
            )
        if rows:
            self._db.executemany(
                f"INSERT INTO captions ({_CAPTION_COLUMNS}) VALUES ("
                ":id, :project_id, :clip_id, :caption_index, :timeline_start, "
                ":timeline_end, :text, :language, :words)",
                rows,
            )

    def _write_effects(
        self, project_id: str, timeline: Timeline, effects: Iterable[EffectInstance]
    ) -> None:
        """Store effects relative to the clip they decorate.

        The planner works in timeline coordinates because that is where an
        effect is placed, but the ``timeline_effects`` schema stores times
        relative to the clip when ``clip_id`` is set -- and it is right to: an
        effect that travels with its clip survives every reorder untouched,
        while an absolute one would need rewriting after each edit and would be
        silently wrong until it was.
        """
        starts = {clip.id: clip.timeline_start for clip in timeline.clips}
        rows = [
            {
                "id": derived_id(
                    "timeline_effect",
                    project_id,
                    effect.clip_id or "",
                    effect.effect.value,
                    round(effect.start_seconds, 3),
                ),
                "project_id": project_id,
                "clip_id": effect.clip_id,
                "effect_type": effect.effect.value,
                "start_seconds": effect.start_seconds - starts.get(effect.clip_id or "", 0.0),
                "duration_seconds": effect.duration_seconds,
                "parameters": dumps(
                    {
                        **effect.params,
                        "engine": effect.engine.value,
                        "category": effect.category.value,
                        # Reserved key: "strength" is also a library magnitude
                        # (blur, glow), and writing the instance strength over
                        # it stored 0.6 where the scaled 0.24 belonged.
                        "instance_strength": effect.strength,
                        "reason": effect.reason,
                    }
                ),
                "enabled": 1,
                # V2-P4: an effect that belongs to a sentence says which one,
                # what part it plays, and which beat it was placed around.
                # Absent on a decoration, which is what every effect was
                # before compositions existed.
                "composition_id": effect.params.get("composition_id"),
                "group_role": effect.params.get("group_role"),
                "anchor_seconds": effect.params.get("anchor_seconds"),
                "offset_seconds": effect.params.get("offset_seconds"),
            }
            for effect in effects
        ]
        if rows:
            self._db.executemany(
                f"INSERT INTO timeline_effects ({_EFFECT_COLUMNS}) VALUES ("
                ":id, :project_id, :clip_id, :effect_type, :start_seconds, "
                ":duration_seconds, :parameters, :enabled, :composition_id, "
                ":group_role, :anchor_seconds, :offset_seconds)",
                rows,
            )


def _clip_row(project_id: str, clip: TimelineClip) -> dict[str, Any]:
    """One clip as its database row. Shared, so the two writers cannot drift."""
    return {
        "id": clip.id,
        "project_id": project_id,
        "media_id": clip.media_id,
        "moment_id": clip.moment_id,
        "track": clip.track.value,
        "clip_index": clip.clip_index,
        "source_in": clip.source_in,
        "source_out": clip.source_out,
        "timeline_start": clip.timeline_start,
        "timeline_end": clip.timeline_end,
        "speed": clip.speed,
        "transition_in": clip.transition_in.value,
        "transition_out": clip.transition_out.value,
        "enabled": int(clip.enabled),
        "metadata": dumps(
            {
                **clip.metadata,
                "moment_type": clip.moment_type.value if clip.moment_type else None,
                "score": clip.score,
                "role": clip.role,
            }
        ),
    }


def _clip(row: sqlite3.Row) -> TimelineClip:
    metadata = loads(row["metadata"]) or {}
    moment_type = metadata.pop("moment_type", None)
    return TimelineClip(
        id=row["id"],
        media_id=row["media_id"],
        moment_id=row["moment_id"],
        track=TrackKind(row["track"]),
        clip_index=row["clip_index"],
        source_in=row["source_in"],
        source_out=row["source_out"],
        timeline_start=row["timeline_start"],
        timeline_end=row["timeline_end"],
        speed=row["speed"],
        transition_in=TransitionType(row["transition_in"]),
        transition_out=TransitionType(row["transition_out"]),
        enabled=bool(row["enabled"]),
        moment_type=MomentType(moment_type) if moment_type else None,
        score=float(metadata.pop("score", 0.0) or 0.0),
        role=str(metadata.pop("role", "body")),
        metadata=metadata,
    )


def _caption(row: sqlite3.Row) -> Caption:
    words = loads(row["words"]) or []
    return Caption(
        id=row["id"],
        index=row["caption_index"],
        timeline_start=row["timeline_start"],
        timeline_end=row["timeline_end"],
        text=row["text"],
        language=row["language"],
        clip_id=row["clip_id"],
        words=tuple(
            (item["word"], float(item["start"]), float(item["end"])) for item in words
        ),
    )


__all__ = ["TimelineRepository"]
