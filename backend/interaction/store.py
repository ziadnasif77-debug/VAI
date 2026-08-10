"""Persistence for the interaction layer.

Three small repositories: the intent update log, the conversation, and edit
versions. They are kept here rather than in ``backend/database/repositories``
because they belong to a layer the pipeline does not know about.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from backend.core.errors import ErrorCode, NotFoundError
from backend.core.ids import new_id
from backend.database.connection import Database, dumps, loads
from backend.interaction.models import (
    ConversationMessage,
    EditingIntent,
    EditVersion,
    IntentDelta,
    IntentSource,
    IntentUpdate,
    InteractionType,
    MessageRole,
)


class IntentStore:
    """The ordered log of intent changes, plus the cached resolved intent."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def preset_for(self, project_id: str) -> str | None:
        row = self._db.fetch_one(
            "SELECT preset FROM editing_intents WHERE project_id = ?", (project_id,)
        )
        return row["preset"] if row is not None else None

    def set_preset(self, project_id: str, preset: str, resolved: EditingIntent) -> None:
        """Choose the preset, discarding no instruction history."""
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO editing_intents (project_id, preset, resolved, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (project_id) DO UPDATE SET preset = excluded.preset, "
                "resolved = excluded.resolved, updated_at = excluded.updated_at",
                (
                    project_id,
                    preset,
                    dumps(resolved.model_dump(mode="json")),
                    _now(),
                ),
            )

    def cache_resolved(self, project_id: str, preset: str, resolved: EditingIntent) -> None:
        """Store the resolved intent so readers do not replay the log."""
        self.set_preset(project_id, preset, resolved)

    def cached(self, project_id: str) -> EditingIntent | None:
        row = self._db.fetch_one(
            "SELECT resolved FROM editing_intents WHERE project_id = ?", (project_id,)
        )
        if row is None:
            return None
        payload = loads(row["resolved"], {})
        return EditingIntent.model_validate(payload) if payload else None

    def append(
        self,
        project_id: str,
        delta: IntentDelta,
        *,
        source: IntentSource,
        raw_text: str | None = None,
        confidence: float = 1.0,
    ) -> IntentUpdate:
        """Record a change at the end of the log."""
        sequence = self.next_sequence(project_id)
        update = IntentUpdate(
            sequence=sequence,
            source=source,
            delta=delta,
            raw_text=raw_text,
            confidence=confidence,
        )
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO editing_intent_updates "
                "(id, project_id, sequence, source, raw_text, delta, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("job").replace("job-", "intent-"),
                    project_id,
                    sequence,
                    source.value,
                    raw_text,
                    dumps(delta.model_dump(mode="json", exclude_none=True)),
                    confidence,
                    update.created_at.isoformat(),
                ),
            )
        return update

    def updates(self, project_id: str) -> list[IntentUpdate]:
        rows = self._db.fetch_all(
            "SELECT * FROM editing_intent_updates WHERE project_id = ? ORDER BY sequence",
            (project_id,),
        )
        return [_intent_update(row) for row in rows]

    def next_sequence(self, project_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COALESCE(MAX(sequence), -1) AS last FROM editing_intent_updates "
            "WHERE project_id = ?",
            (project_id,),
        )
        return (int(row["last"]) + 1) if row is not None else 0

    def clear(self, project_id: str) -> int:
        """Drop every instruction, returning to the preset alone."""
        with self._db.transaction():
            cursor = self._db.execute(
                "DELETE FROM editing_intent_updates WHERE project_id = ?", (project_id,)
            )
        return cursor.rowcount


class ConversationStore:
    """Per-project chat history (§11)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def append(
        self,
        project_id: str,
        *,
        role: MessageRole,
        text: str,
        interaction_type: InteractionType | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ConversationMessage:
        message = ConversationMessage(
            id=new_id("job").replace("job-", "msg-"),
            project_id=project_id,
            role=role,
            text=text,
            interaction_type=interaction_type,
            payload=dict(payload or {}),
            created_at=datetime.now(timezone.utc),
        )
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO conversation_messages "
                "(id, project_id, role, interaction_type, text, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    project_id,
                    role.value,
                    interaction_type.value if interaction_type else None,
                    text,
                    dumps(message.payload),
                    message.created_at.isoformat(),
                ),
            )
        return message

    def history(self, project_id: str, *, limit: int = 50) -> list[ConversationMessage]:
        """Return the most recent messages, oldest first."""
        rows = self._db.fetch_all(
            "SELECT * FROM conversation_messages WHERE project_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (project_id, limit),
        )
        return [_message(row) for row in reversed(rows)]

    def clear(self, project_id: str) -> int:
        with self._db.transaction():
            cursor = self._db.execute(
                "DELETE FROM conversation_messages WHERE project_id = ?", (project_id,)
            )
        return cursor.rowcount


class EditVersionStore:
    """Snapshots of the edit, so a rollback costs no analysis (§19)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def snapshot(
        self,
        project_id: str,
        intent: EditingIntent,
        clips: list[dict[str, Any]],
        *,
        reason: str | None = None,
        label: str | None = None,
    ) -> EditVersion:
        version = self.latest_version(project_id) + 1
        record = EditVersion(
            id=new_id("job").replace("job-", "edit-"),
            project_id=project_id,
            version=version,
            label=label,
            reason=reason,
            intent=intent,
            clips=clips,
            created_at=datetime.now(timezone.utc),
        )
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO edit_versions "
                "(id, project_id, version, label, reason, intent, clips, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    project_id,
                    version,
                    label,
                    reason,
                    dumps(intent.model_dump(mode="json")),
                    dumps(clips),
                    record.created_at.isoformat(),
                ),
            )
        return record

    def latest_version(self, project_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COALESCE(MAX(version), 0) AS latest FROM edit_versions WHERE project_id = ?",
            (project_id,),
        )
        return int(row["latest"]) if row is not None else 0

    def get(self, project_id: str, version: int) -> EditVersion:
        row = self._db.fetch_one(
            "SELECT * FROM edit_versions WHERE project_id = ? AND version = ?",
            (project_id, version),
        )
        if row is None:
            raise NotFoundError(
                f"Edit version {version} does not exist for this project.",
                code=ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": project_id, "version": version},
                recoverable=False,
            )
        return _edit_version(row)

    def list(self, project_id: str, *, limit: int = 20) -> list[EditVersion]:
        rows = self._db.fetch_all(
            "SELECT * FROM edit_versions WHERE project_id = ? ORDER BY version DESC LIMIT ?",
            (project_id, limit),
        )
        return [_edit_version(row) for row in rows]

    def prune(self, project_id: str, keep: int) -> int:
        """Keep only the newest ``keep`` versions."""
        with self._db.transaction():
            cursor = self._db.execute(
                "DELETE FROM edit_versions WHERE project_id = ? AND version <= "
                "(SELECT COALESCE(MAX(version), 0) - ? FROM edit_versions WHERE project_id = ?)",
                (project_id, keep, project_id),
            )
        return cursor.rowcount


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _intent_update(row: sqlite3.Row) -> IntentUpdate:
    return IntentUpdate(
        sequence=row["sequence"],
        source=IntentSource(row["source"]),
        delta=IntentDelta.model_validate(loads(row["delta"], {})),
        raw_text=row["raw_text"],
        confidence=row["confidence"],
        created_at=_parse(row["created_at"]),
    )


def _message(row: sqlite3.Row) -> ConversationMessage:
    return ConversationMessage(
        id=row["id"],
        project_id=row["project_id"],
        role=MessageRole(row["role"]),
        text=row["text"],
        interaction_type=(
            InteractionType(row["interaction_type"]) if row["interaction_type"] else None
        ),
        payload=loads(row["payload"], {}),
        created_at=_parse(row["created_at"]),
    )


def _edit_version(row: sqlite3.Row) -> EditVersion:
    return EditVersion(
        id=row["id"],
        project_id=row["project_id"],
        version=row["version"],
        label=row["label"],
        reason=row["reason"],
        intent=EditingIntent.model_validate(loads(row["intent"], {})),
        clips=loads(row["clips"], []),
        created_at=_parse(row["created_at"]),
    )


__all__ = ["ConversationStore", "EditVersionStore", "IntentStore"]
