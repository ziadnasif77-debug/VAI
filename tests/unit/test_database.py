"""Database and migration tests (SPEC sections 44, 45)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.config.schema import DatabaseConfig
from backend.core.errors import ErrorCode, StorageError
from backend.database.connection import Database, dumps, loads
from backend.database.migrator import (
    current_version,
    discover_migrations,
    migrate,
    verify_schema_version,
)

pytestmark = pytest.mark.unit

#: The core entities of §45, plus the tables the job system, the analysis cache
#: and the delivery layer require.
EXPECTED_TABLES = {
    "projects",
    "media",
    "media_tracks",
    "analysis_jobs",
    "analysis_cache",
    "scenes",
    "frames",
    "audio_events",
    "transcript_segments",
    "game_events",
    "moments",
    "timeline_clips",
    "timeline_effects",
    "captions",
    "music",
    "renders",
    "qa_results",
    "video_metadata",
    "publications",
}


class TestMigrations:
    def test_creates_every_expected_table(self, database: Database) -> None:
        assert database.table_names() >= EXPECTED_TABLES

    def test_is_idempotent(self, database: Database) -> None:
        before = current_version(database)
        assert migrate(database) == []
        assert current_version(database) == before

    def test_records_the_applied_version(self, database: Database) -> None:
        assert current_version(database) == 1

    def test_verify_schema_version_accepts_a_match(self, database: Database) -> None:
        verify_schema_version(database, 1)

    def test_verify_schema_version_rejects_a_mismatch(self, database: Database) -> None:
        with pytest.raises(StorageError) as exc_info:
            verify_schema_version(database, 99)
        assert exc_info.value.code is ErrorCode.MIGRATION_FAILED

    def test_editing_an_applied_migration_is_refused(
        self, database: Database, tmp_path: Path
    ) -> None:
        # Simulate a developer editing a migration that already ran on a user's
        # machine: the migrator must refuse rather than silently diverge.
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "0001_initial.sql").write_text("CREATE TABLE a (id TEXT);", "utf-8")

        fresh_db = Database(tmp_path / "edited.db", DatabaseConfig())
        migrate(fresh_db, directory)
        (directory / "0001_initial.sql").write_text("CREATE TABLE b (id TEXT);", "utf-8")
        with pytest.raises(StorageError, match="has changed since it was applied"):
            migrate(fresh_db, directory)
        fresh_db.close()

    def test_misnamed_migration_is_rejected(self, tmp_path: Path) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "initial.sql").write_text("SELECT 1;", "utf-8")
        with pytest.raises(StorageError, match="does not match"):
            discover_migrations(directory)

    def test_duplicate_version_is_rejected(self, tmp_path: Path) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "0001_a.sql").write_text("SELECT 1;", "utf-8")
        (directory / "0001_b.sql").write_text("SELECT 1;", "utf-8")
        with pytest.raises(StorageError, match="Duplicate migration version"):
            discover_migrations(directory)


class TestConnection:
    def test_foreign_keys_are_enforced(self, database: Database) -> None:
        # SQLite disables enforcement per connection by default; without the
        # pragma every cascade in the schema would be decorative.
        row = database.fetch_one("PRAGMA foreign_keys")
        assert row is not None and row[0] == 1

    def test_wal_is_enabled(self, database: Database) -> None:
        row = database.fetch_one("PRAGMA journal_mode")
        assert row is not None
        assert str(row[0]).lower() == "wal"

    def test_orphan_child_row_is_rejected(self, database: Database) -> None:
        with pytest.raises(StorageError) as exc_info:
            database.execute(
                "INSERT INTO media (id, project_id, source_path, filename, container, "
                "size_bytes, checksum, created_at, updated_at) "
                "VALUES ('m1', 'nonexistent', '/x.mp4', 'x.mp4', '.mp4', 1, 'abc', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            )
        assert exc_info.value.code is ErrorCode.DATABASE_ERROR

    def test_transaction_rolls_back_on_error(self, database: Database) -> None:
        with pytest.raises(RuntimeError), database.transaction() as connection:
            if True:  # keeps the failing statement and the raise in one block
                connection.execute(
                    "INSERT INTO projects (id, name, created_at, updated_at, "
                    "target_duration_seconds, project_directory, application_version, "
                    "analysis_version, schema_version) VALUES "
                    "('p1', 'x', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
                    "1200, '/tmp/p1', '0.1.0', 1, 1)"
                )
                raise RuntimeError("boom")
        assert database.fetch_one("SELECT 1 FROM projects WHERE id = 'p1'") is None

    def test_nested_transaction_reuses_the_outer_one(self, database: Database) -> None:
        # The nesting is the behaviour under test: an inner block must join the
        # outer transaction rather than committing early. Written as separate
        # statements so the two contexts stay genuinely nested.
        outer = database.transaction()
        outer.__enter__()
        with database.transaction():
            database.execute("SELECT 1")
        assert database.connection.in_transaction
        outer.__exit__(None, None, None)
        assert not database.connection.in_transaction


class TestDurationConstraint:
    """§6's band is enforced by the database too, not only in Python."""

    def _insert(self, database: Database, seconds: int) -> None:
        database.execute(
            "INSERT INTO projects (id, name, created_at, updated_at, "
            "target_duration_seconds, project_directory, application_version, "
            "analysis_version, schema_version) VALUES "
            "(?, 'x', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', ?, "
            "'/tmp/p', '0.1.0', 1, 1)",
            (f"p{seconds}", seconds),
        )

    @pytest.mark.parametrize("seconds", [599, 0, 3601, 7200])
    def test_rejects_out_of_band_targets(self, database: Database, seconds: int) -> None:
        with pytest.raises(StorageError):
            self._insert(database, seconds)

    @pytest.mark.parametrize("seconds", [600, 1200, 3600])
    def test_accepts_in_band_targets(self, database: Database, seconds: int) -> None:
        self._insert(database, seconds)


class TestJsonColumns:
    def test_round_trip(self) -> None:
        payload = {"a": 1, "b": ["x", "y"], "c": {"nested": True}}
        assert loads(dumps(payload)) == payload

    def test_null_falls_back_to_default(self) -> None:
        assert loads(None, {}) == {}

    def test_malformed_json_falls_back_rather_than_crashing(self) -> None:
        assert loads("{not json", {"fallback": True}) == {"fallback": True}


class TestErrorTranslation:
    def test_sqlite_error_becomes_a_typed_error(self, database: Database) -> None:
        with pytest.raises(StorageError) as exc_info:
            database.execute("SELECT * FROM table_that_does_not_exist")
        assert exc_info.value.code is ErrorCode.DATABASE_ERROR
        assert isinstance(exc_info.value.__cause__, sqlite3.Error)
