"""Cross-project reuse of per-media analysis (SPEC §48, §49, §127).

The cost this exists to delete was measured on a real afternoon: the same
67-minute recording imported into two projects ran the full transcript /
vision / OCR chain twice — over an hour of duplicate GPU work producing
byte-identical conclusions. §48 had already defined the identity of a cached
result (``video_hash + model_version + analysis_version``), the key builder in
:mod:`backend.core.cache_keys` implements it, and the ``analysis_cache`` table
was waiting in migration 0001. What was missing was the wiring: nothing ever
wrote the table, so nothing could read it.

The design is a pointer, not a payload. On success a stage records *where* a
result of this exact identity lives (project, media); a later stage with the
same identity copies the stored rows across and returns the donor's job
result. Copying rows rather than files keeps §42 intact — nothing in a project
tree is shared — and returning the donor's job result matters because job
results are the §81 contract between stages (OCR's HUD readings travel in
one).

What is deliberately *not* reused:

* **probe / proxy / audio / frames** — media preparation. Their artefacts are
  files inside a project tree, and sharing files across projects is how
  deleting one project breaks another.
* **game_events** — profile-dependent and derived in seconds from rows this
  module already copies (§127 calls that re-run cheap, and it is).
* **moments onward** — per-project by definition: intent, mode and target all
  change the answer.

Two safety rules, both enforced rather than assumed:

* A hit pointing at the **same media** is treated as a miss. §90's re-analysis
  requeues a stage precisely to run it again; serving a stage its own old rows
  would turn "re-run" into a no-op.
* Reuse **never fails a stage**. Any error here degrades to recomputing
  (§95) — a cache is an optimisation, and an optimisation that can break the
  pipeline is a bug with good intentions.

Staleness is structural, not policed: ``analysis_cache.project_id`` has
``ON DELETE CASCADE``, so deleting the donor project deletes the pointer, and
the checksum comparison catches a media row whose file was swapped.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Final

from backend.core.cache_keys import CacheNamespace, build_cache_key
from backend.core.ids import derived_id
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.core.versions import PROMPT_VERSIONS
from backend.gaming.profiles import load_profile
from backend.pipeline.workers.base import WorkerContext

logger = get_logger("pipeline.reuse", LogChannel.PIPELINE)

#: The prompt the vision stage renders. Mirrored here rather than imported
#: from the worker module, because workers import this module and a cycle
#: would be the price of the nicety. A test pins the two together.
VISION_PROMPT_ID: Final[str] = "vision.frame_description"

#: Stage → the tables its rows live in. Being listed here is what makes a
#: stage reusable.
_TABLES: Final[dict[JobStage, tuple[str, ...]]] = {
    JobStage.TRANSCRIPT: ("transcript_segments",),
    JobStage.AUDIO_EVENTS: ("audio_events",),
    JobStage.SCENES: ("scenes",),
    JobStage.VISION: ("vision_observations",),
    JobStage.OCR: ("ocr_results",),
}

#: Table → the id entity its rows carry. A copied row keeps its native shape:
#: a transcript segment that arrived by reuse is still ``seg-...``, derived
#: deterministically so re-copying is idempotent (§48).
_ENTITIES: Final[dict[str, str]] = {
    "transcript_segments": "transcript_segment",
    "audio_events": "audio_event",
    "scenes": "scene",
    "vision_observations": "vision_observation",
    "ocr_results": "ocr_result",
}

#: Result keys that state how many rows the stage produced. Used to refuse a
#: donor whose rows have vanished while its result still claims some.
_COUNT_KEYS: Final[tuple[str, ...]] = (
    "segments",
    "events",
    "scenes",
    "observations",
    "detections",
)


def try_reuse(context: WorkerContext, stage: JobStage) -> dict[str, Any] | None:
    """Return a finished result for identical inputs, or ``None``.

    ``None`` means "compute it yourself" for any reason at all — stage not
    reusable, no donor, donor unverifiable, or the reuse machinery itself
    failing. The caller never needs to know which.
    """
    if stage not in _TABLES:
        return None
    try:
        return _reuse(context, stage)
    except Exception as error:  # §95: reuse must never break a stage
        logger.warning(
            "Analysis reuse failed; recomputing",
            extra={"stage": stage.value, "error": str(error)[:200]},
            exc_info=True,
        )
        return None


def record_success(context: WorkerContext, stage: JobStage) -> None:
    """Publish this stage's result as reusable, keyed by its exact inputs.

    Called only on the full-success path. Skipped stages are never recorded:
    "no speech model on this machine" is a fact about the machine, not about
    the recording, and caching it would export one machine's gaps to another.
    """
    if stage not in _TABLES:
        return
    try:
        media = context.require_media()
        key, inputs = _cache_key(context, stage)
        now = datetime.now(timezone.utc).isoformat()
        with context.database.transaction():
            context.database.execute(
                "INSERT OR REPLACE INTO analysis_cache "
                "(cache_key, project_id, media_id, namespace, artefact_path, size_bytes, "
                " model_name, model_version, prompt_id, prompt_version, analysis_version, "
                " created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    context.project_id,
                    media.id,
                    stage.value,
                    "db://" + ",".join(_TABLES[stage]),
                    0,
                    inputs.model_name,
                    inputs.model_version,
                    inputs.prompt_id,
                    inputs.prompt_version,
                    inputs.analysis_version,
                    now,
                    now,
                ),
            )
    except Exception as error:  # never fail the stage over bookkeeping
        logger.warning(
            "Could not record the analysis for reuse",
            extra={"stage": stage.value, "error": str(error)[:200]},
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _KeyInputs:
    """What went into a key, kept for the cache row's provenance columns."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        model_version: str | None = None,
        prompt_id: str | None = None,
        prompt_version: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        from backend.core.versions import ANALYSIS_VERSION

        self.model_name = model_name
        self.model_version = model_version
        self.prompt_id = prompt_id
        self.prompt_version = prompt_version
        self.analysis_version = ANALYSIS_VERSION
        self.params = params or {}


def _dump(value: Any) -> Any:
    return value.model_dump() if hasattr(value, "model_dump") else value


def _analysis_params(analysis: Any, *names: str) -> dict[str, Any]:
    """The named analysis sub-configs, for sections that exist.

    Guarded with ``hasattr`` so a renamed config section degrades to "that
    knob no longer affects the key" rather than breaking every stage run.
    """
    return {name: _dump(getattr(analysis, name)) for name in names if hasattr(analysis, name)}


def _key_inputs(context: WorkerContext, stage: JobStage) -> _KeyInputs:
    """Everything that changes this stage's output, per §48's exactness.

    Too loose and a stale result reaches the timeline; too strict and every
    re-import pays for a full re-analysis. Where a stage consumes another
    stage's choices (vision and OCR read the §16 cascade's candidates), the
    upstream knobs are part of this stage's identity too.
    """
    config = context.config
    analysis = config.analysis

    if stage is JobStage.TRANSCRIPT:
        speech = config.models.speech
        return _KeyInputs(
            model_name=speech.model,
            model_version=speech.version,
            params={
                "provider": speech.provider,
                "model": speech.model,
                "language": speech.language,
                "vad": speech.vad_filter,
                "words": speech.word_timestamps,
                **_analysis_params(
                    analysis, "chunk_seconds", "chunk_overlap_seconds", "event_overlap_seconds"
                ),
            },
        )
    if stage is JobStage.AUDIO_EVENTS:
        return _KeyInputs(
            params=_analysis_params(analysis, "audio_events", "reactions", "chunk_seconds"),
        )
    if stage is JobStage.SCENES:
        return _KeyInputs(params=_analysis_params(analysis, "scenes"))
    if stage is JobStage.VISION:
        vision = config.models.vision
        return _KeyInputs(
            model_name=vision.model,
            model_version=vision.version,
            prompt_id=VISION_PROMPT_ID,
            prompt_version=PROMPT_VERSIONS.get(VISION_PROMPT_ID),
            params={
                "model": vision.model,
                **_analysis_params(
                    analysis, "vision", "frame_sampling", "candidates", "scenes", "audio_events"
                ),
            },
        )
    if stage is JobStage.OCR:
        ocr = config.models.ocr
        resolution = _profile(context)
        return _KeyInputs(
            model_name=ocr.model,
            model_version=ocr.version,
            params={
                "engine": ocr.provider,
                "model": ocr.model,
                "profile": resolution.id,
                "profile_version": getattr(resolution.profile, "version", None),
                **_analysis_params(
                    analysis, "ocr", "hud", "frame_sampling", "candidates", "scenes",
                    "audio_events",
                ),
            },
        )
    raise KeyError(stage)


def _profile(context: WorkerContext):
    row = context.database.fetch_one(
        "SELECT game FROM projects WHERE id = ?", (context.project_id,)
    )
    game = str(row["game"]) if row is not None and row["game"] else "auto"
    return load_profile(game, context.profiles_dir)


def _cache_key(context: WorkerContext, stage: JobStage) -> tuple[str, _KeyInputs]:
    media = context.require_media()
    inputs = _key_inputs(context, stage)
    key = build_cache_key(
        CacheNamespace(stage.value),
        video_hash=media.checksum,
        model_version=inputs.model_version,
        prompt_id=inputs.prompt_id,
        prompt_version=inputs.prompt_version,
        params=inputs.params,
    )
    return key, inputs


def _reuse(context: WorkerContext, stage: JobStage) -> dict[str, Any] | None:
    media = context.require_media()
    key, _ = _cache_key(context, stage)
    donor = context.database.fetch_one(
        "SELECT project_id, media_id FROM analysis_cache WHERE cache_key = ?", (key,)
    )
    if donor is None:
        return None
    if donor["media_id"] == media.id:
        # §90: a requeued stage was requeued to run again.
        return None

    donor_media = context.database.fetch_one(
        "SELECT checksum FROM media WHERE id = ?", (donor["media_id"],)
    )
    if donor_media is None or donor_media["checksum"] != media.checksum:
        return None

    job = context.database.fetch_one(
        "SELECT result FROM analysis_jobs "
        "WHERE project_id = ? AND media_id = ? AND stage = ? AND status = 'completed' "
        "ORDER BY completed_at DESC LIMIT 1",
        (donor["project_id"], donor["media_id"], stage.value),
    )
    if job is None or not job["result"]:
        return None
    result = json.loads(job["result"])
    if not isinstance(result, dict) or result.get("skipped"):
        return None

    donor_rows = {
        table: context.database.fetch_all(
            f"SELECT * FROM {table} WHERE media_id = ?", (donor["media_id"],)
        )
        for table in _TABLES[stage]
    }
    copied_total = sum(len(rows) for rows in donor_rows.values())
    if copied_total == 0 and _declared_count(result) not in (0, None):
        # The pointer survived but the rows did not (a partial delete, a
        # migration mishap). A donor whose result claims rows it cannot
        # produce is not a donor.
        return None

    with context.database.transaction():
        for table, rows in donor_rows.items():
            context.database.execute(
                f"DELETE FROM {table} WHERE media_id = ?", (media.id,)
            )
            for row in rows:
                # sqlite3.Row: .keys() is the API; SIM118's dict advice does
                # not apply, so materialise the names first.
                names = list(row.keys())
                values = {name: row[name] for name in names}
                values["id"] = derived_id(_ENTITIES[table], media.id, str(row["id"]))
                values["project_id"] = context.project_id
                values["media_id"] = media.id
                if table == "scenes":
                    # The keyframe path points into the donor's tree. A blank
                    # thumbnail beats reading another project's files.
                    values["keyframe_path"] = ""
                columns = ", ".join(values)
                placeholders = ", ".join(f":{name}" for name in values)
                context.database.execute(
                    f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
                    values,
                )
        context.database.execute(
            "UPDATE analysis_cache SET last_used_at = ? WHERE cache_key = ?",
            (datetime.now(timezone.utc).isoformat(), key),
        )

    logger.info(
        "Reused an identical earlier analysis",
        extra={
            "stage": stage.value,
            "donor_project": donor["project_id"],
            "rows_copied": copied_total,
        },
    )
    return {
        **result,
        "reused_from_project": donor["project_id"],
        "rows_copied": copied_total,
    }


def _declared_count(result: dict[str, Any]) -> int | None:
    for key in _COUNT_KEYS:
        value = result.get(key)
        if isinstance(value, int):
            return value
    return None


__all__ = ["record_success", "try_reuse"]
