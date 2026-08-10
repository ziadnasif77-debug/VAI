"""Vision observation persistence (SPEC sections 15, 26, 45, 49).

One row per candidate keyframe the model described. Each carries its model and
prompt version, because §49 requires a wrong result to be traceable to the
exact model and wording that produced it — and because §48 cannot invalidate
what it cannot identify.

Each row also carries the region it came from and the detectors that nominated
it. That is what makes a surprising observation debuggable: "why did the model
look here" has an answer in the data rather than in a reconstruction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

from ai.providers.base import ModelInfo, StoredObservation, VisionObservation
from backend.core.ids import new_id
from backend.database.connection import Database, dumps, loads

_COLUMNS = (
    "id, project_id, media_id, timestamp, description, labels, confidence, hud, "
    "region_start, region_end, sources, model_name, model_version, prompt_id, "
    "prompt_version, created_at"
)


class VisionRepository:
    """CRUD for the ``vision_observations`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_media(
        self,
        project_id: str,
        media_id: str,
        observations: Iterable[StoredObservation],
    ) -> int:
        """Replace a file's observations.

        Wholesale: a re-run means the model, the prompt or the candidate plan
        changed, and merging would leave descriptions from a configuration
        nobody is using next to ones from the current model (§49).
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "id": new_id("vision_observation"),
                "project_id": project_id,
                "media_id": media_id,
                "timestamp": max(item.timestamp, 0.0),
                "description": item.description,
                "labels": dumps(list(item.labels)),
                "confidence": min(max(item.confidence, 0.0), 1.0),
                "hud": dumps(item.hud),
                "region_start": item.region_start,
                "region_end": item.region_end,
                "sources": dumps(list(item.sources)),
                "model_name": item.model_name,
                "model_version": item.model_version,
                "prompt_id": item.prompt_id,
                "prompt_version": item.prompt_version,
                "created_at": now,
            }
            for item in observations
        ]
        self._db.execute("DELETE FROM vision_observations WHERE media_id = ?", (media_id,))
        if rows:
            self._db.executemany(
                f"INSERT OR REPLACE INTO vision_observations ({_COLUMNS}) VALUES ("
                ":id, :project_id, :media_id, :timestamp, :description, :labels, "
                ":confidence, :hud, :region_start, :region_end, :sources, :model_name, "
                ":model_version, :prompt_id, :prompt_version, :created_at)",
                rows,
            )
        return len(rows)

    def list_for_media(
        self,
        media_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        min_confidence: float | None = None,
        label: str | None = None,
    ) -> list[StoredObservation]:
        """Observations in chronological order, filtered as asked."""
        sql = f"SELECT {_COLUMNS} FROM vision_observations WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if start is not None:
            sql += " AND timestamp >= ?"
            parameters.append(start)
        if end is not None:
            sql += " AND timestamp <= ?"
            parameters.append(end)
        if min_confidence is not None:
            sql += " AND confidence >= ?"
            parameters.append(min_confidence)
        sql += " ORDER BY timestamp ASC"
        rows = [_from_row(row) for row in self._db.fetch_all(sql, parameters)]
        if label is None:
            return rows
        # Filtered in Python: labels are a JSON array, and a LIKE over it would
        # match "combat" inside "non_combat".
        return [item for item in rows if label in item.labels]

    def list_for_project(self, project_id: str) -> list[StoredObservation]:
        return [
            _from_row(row)
            for row in self._db.fetch_all(
                f"SELECT {_COLUMNS} FROM vision_observations WHERE project_id = ? "
                "ORDER BY media_id ASC, timestamp ASC",
                (project_id,),
            )
        ]

    def count_for_media(self, media_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM vision_observations WHERE media_id = ?",
            (media_id,),
        )
        return int(row["total"]) if row is not None else 0

    def label_counts(self, media_id: str) -> dict[str, int]:
        """Tally of labels, for the analysis screen and the stage log."""
        tally: dict[str, int] = {}
        for item in self.list_for_media(media_id):
            for label in item.labels:
                tally[label] = tally.get(label, 0) + 1
        return tally

    def models_used(self, media_id: str) -> set[tuple[str, str]]:
        """Distinct ``(model, version)`` pairs behind this file's observations.

        More than one pair means a stage was re-run with a different model and
        the results were not replaced — which §49 exists to make visible.
        """
        return {
            (str(row["model_name"]), str(row["model_version"]))
            for row in self._db.fetch_all(
                "SELECT DISTINCT model_name, model_version FROM vision_observations "
                "WHERE media_id = ?",
                (media_id,),
            )
        }

    def delete_for_media(self, media_id: str) -> int:
        return self._db.execute(
            "DELETE FROM vision_observations WHERE media_id = ?", (media_id,)
        ).rowcount


def observations_from(
    observations: Sequence[VisionObservation],
    *,
    info: ModelInfo,
    prompt_id: str | None,
    prompt_version: int | None,
    region_start: float | None = None,
    region_end: float | None = None,
    sources: Sequence[str] = (),
) -> list[StoredObservation]:
    """Attach provenance to a batch of observations, ready for storage."""
    return [
        StoredObservation(
            observation=observation,
            region_start=region_start,
            region_end=region_end,
            sources=tuple(sources),
            model_name=info.name,
            model_version=info.version,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
        )
        for observation in observations
    ]


def _from_row(row: sqlite3.Row) -> StoredObservation:
    labels = loads(row["labels"]) or []
    hud = loads(row["hud"]) or {}
    return StoredObservation(
        observation=VisionObservation(
            timestamp=row["timestamp"],
            description=row["description"],
            labels=tuple(str(label) for label in labels if isinstance(labels, list)),
            confidence=row["confidence"],
            hud=hud if isinstance(hud, dict) else {},
        ),
        region_start=row["region_start"],
        region_end=row["region_end"],
        sources=tuple(loads(row["sources"]) or []),
        model_name=row["model_name"],
        model_version=row["model_version"],
        prompt_id=row["prompt_id"],
        prompt_version=row["prompt_version"],
    )


__all__ = ["StoredObservation", "VisionRepository", "observations_from"]
