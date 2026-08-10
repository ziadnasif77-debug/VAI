"""Forward-only SQL migrations.

Migrations are numbered ``.sql`` files applied in order and recorded in
``schema_migrations``. Each is applied inside a transaction, so a failure
leaves the database exactly as it was.

An applied migration's checksum is stored. Editing a file that has already run
on a user's machine would silently diverge their schema from the code's
expectations, so the migrator refuses to continue when it detects that -- fix
forward with a new migration instead.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.core.errors import ErrorCode, StorageError
from backend.core.logging import LogChannel, get_logger
from backend.database.connection import Database

logger = get_logger("database.migrator", LogChannel.APPLICATION)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        """Hash of the normalised SQL.

        Line endings are normalised first so a Windows checkout does not appear
        to have modified every migration.
        """
        normalised = self.sql.replace("\r\n", "\n").encode("utf-8")
        return hashlib.sha256(normalised).hexdigest()


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Return every migration file, ordered by version.

    Raises:
        StorageError: on a misnamed file or a duplicated version number.
            Both would make the applied order ambiguous.
    """
    source = Path(directory) if directory is not None else MIGRATIONS_DIR
    if not source.is_dir():
        raise StorageError(
            f"Migrations directory not found: {source}",
            code=ErrorCode.MIGRATION_FAILED,
            details={"directory": str(source)},
            recoverable=False,
        )

    migrations: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(source.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise StorageError(
                f"Migration filename {path.name!r} does not match NNNN_name.sql.",
                code=ErrorCode.MIGRATION_FAILED,
                details={"file": str(path)},
                recoverable=False,
            )
        version = int(match.group(1))
        if version in seen:
            raise StorageError(
                f"Duplicate migration version {version}: {seen[version]} and {path.name}.",
                code=ErrorCode.MIGRATION_FAILED,
                details={"version": version},
                recoverable=False,
            )
        seen[version] = path.name
        migrations.append(Migration(version=version, name=match.group(2), path=path))

    return migrations


def applied_versions(database: Database) -> dict[int, str]:
    """Return ``{version: checksum}`` for migrations already applied."""
    database.execute(_SCHEMA_MIGRATIONS_DDL)
    rows = database.fetch_all("SELECT version, checksum FROM schema_migrations")
    return {int(row["version"]): str(row["checksum"]) for row in rows}


def current_version(database: Database) -> int:
    """Return the highest applied migration version, or 0 for a fresh database."""
    applied = applied_versions(database)
    return max(applied) if applied else 0


def migrate(database: Database, directory: Path | None = None) -> list[Migration]:
    """Apply every pending migration and return those applied.

    Idempotent: running it on an up-to-date database applies nothing.

    Raises:
        StorageError: if an already-applied migration file has changed, or if a
            migration fails to execute.
    """
    migrations = discover_migrations(directory)
    applied = applied_versions(database)

    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is None:
            continue
        if recorded != migration.checksum:
            raise StorageError(
                f"Migration {migration.version:04d}_{migration.name} has changed since it "
                "was applied. Applied migrations are immutable -- add a new migration "
                "instead of editing this one.",
                code=ErrorCode.MIGRATION_FAILED,
                details={
                    "version": migration.version,
                    "expected_checksum": recorded,
                    "actual_checksum": migration.checksum,
                },
                recoverable=False,
            )

    pending = [item for item in migrations if item.version not in applied]
    if not pending:
        logger.debug(
            "Database schema is up to date",
            extra={"schema_version": current_version(database)},
        )
        return []

    for migration in pending:
        logger.info(
            "Applying migration",
            extra={"version": migration.version, "migration": migration.name},
        )
        try:
            with database.transaction() as connection:
                connection.executescript(migration.sql)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(
                f"Migration {migration.version:04d}_{migration.name} failed: {exc}",
                code=ErrorCode.MIGRATION_FAILED,
                details={"version": migration.version, "migration": migration.name},
                recoverable=False,
                cause=exc,
            ) from exc

    logger.info(
        "Database migrated",
        extra={"applied": len(pending), "schema_version": current_version(database)},
    )
    return pending


def verify_schema_version(database: Database, expected: int) -> None:
    """Assert the database matches the schema version the code expects.

    Called at startup: running new code against an un-migrated database is a
    fast, clear failure rather than a confusing "no such column" later.
    """
    actual = current_version(database)
    if actual != expected:
        raise StorageError(
            f"Database schema version {actual} does not match the expected {expected}. "
            "Run the migrations.",
            code=ErrorCode.MIGRATION_FAILED,
            details={"actual": actual, "expected": expected},
            recoverable=False,
        )


__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "applied_versions",
    "current_version",
    "discover_migrations",
    "migrate",
    "verify_schema_version",
]
