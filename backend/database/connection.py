"""SQLite connection management (SPEC section 44).

SQLite is the MVP store. Long analysis runs write from a worker while the API
reads, so WAL journalling and a busy timeout are not optional extras -- without
them a render and a status poll deadlock each other.

No ORM: the §45 schema is fixed and hand-written SQL keeps the database free of
pipeline logic.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.config.schema import DatabaseConfig
from backend.core.errors import ErrorCode, StorageError
from backend.core.logging import LogChannel, get_logger

logger = get_logger("database.connection", LogChannel.APPLICATION)


def connect(database_path: Path, config: DatabaseConfig | None = None) -> sqlite3.Connection:
    """Open a configured connection to ``database_path``.

    Applies the pragmas the application depends on:

    * ``foreign_keys`` -- SQLite disables enforcement per connection by
      default, so the cascades in the schema only work if this is set.
    * ``journal_mode=WAL`` -- concurrent reader while a worker writes.
    * ``busy_timeout`` -- wait instead of raising "database is locked".
    """
    settings = config or DatabaseConfig()
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        connection = sqlite3.connect(
            str(path),
            timeout=settings.busy_timeout_ms / 1000,
            isolation_level=None,  # explicit transactions via `transaction()`
            check_same_thread=False,
        )
    except sqlite3.Error as exc:
        raise StorageError(
            f"Cannot open database {path}: {exc}",
            code=ErrorCode.DATABASE_ERROR,
            details={"database_path": str(path)},
            cause=exc,
        ) from exc

    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    try:
        if settings.foreign_keys:
            cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA journal_mode = {settings.journal_mode}")
        cursor.execute(f"PRAGMA synchronous = {settings.synchronous}")
        cursor.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
    except sqlite3.Error as exc:
        connection.close()
        raise StorageError(
            f"Cannot configure database {path}: {exc}",
            code=ErrorCode.DATABASE_ERROR,
            details={"database_path": str(path)},
            cause=exc,
        ) from exc
    finally:
        cursor.close()

    return connection


class Database:
    """Thin wrapper over :class:`sqlite3.Connection`.

    Deliberately small: it owns connection lifecycle, transactions and error
    translation. Table knowledge lives in the repositories.
    """

    def __init__(self, database_path: Path, config: DatabaseConfig | None = None) -> None:
        self.path = Path(database_path)
        self.config = config or DatabaseConfig()
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle ------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The open connection, opening one on first use."""
        if self._connection is None:
            self._connection = connect(self.path, self.config)
        return self._connection

    def close(self) -> None:
        """Close the connection if open. Safe to call repeatedly."""
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- transactions ---------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one transaction.

        Commits on success, rolls back on any exception. Nesting reuses the
        outer transaction, so a repository method can be called standalone or as
        part of a larger unit of work without a partial commit.
        """
        connection = self.connection
        if connection.in_transaction:
            yield connection
            return
        try:
            connection.execute("BEGIN")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    # -- queries --------------------------------------------------------

    def execute(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Execute one statement, translating SQLite errors to typed errors."""
        try:
            return self.connection.execute(sql, parameters)
        except sqlite3.IntegrityError as exc:
            raise StorageError(
                f"Constraint violation: {exc}",
                code=ErrorCode.DATABASE_ERROR,
                details={"sql": _summarise(sql)},
                recoverable=False,
                cause=exc,
            ) from exc
        except sqlite3.Error as exc:
            raise StorageError(
                f"Database error: {exc}",
                code=ErrorCode.DATABASE_ERROR,
                details={"sql": _summarise(sql)},
                cause=exc,
            ) from exc

    def executemany(
        self, sql: str, parameter_sets: Sequence[Sequence[Any] | dict[str, Any]]
    ) -> sqlite3.Cursor:
        """Execute one statement over many parameter sets."""
        try:
            return self.connection.executemany(sql, parameter_sets)
        except sqlite3.Error as exc:
            raise StorageError(
                f"Database error: {exc}",
                code=ErrorCode.DATABASE_ERROR,
                details={"sql": _summarise(sql), "rows": len(parameter_sets)},
                cause=exc,
            ) from exc

    def fetch_one(
        self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        cursor = self.execute(sql, parameters)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def fetch_all(
        self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()
    ) -> list[sqlite3.Row]:
        cursor = self.execute(sql, parameters)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def table_names(self) -> set[str]:
        """Return every user table currently defined."""
        rows = self.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return {row["name"] for row in rows}


def dumps(value: Any) -> str:
    """Serialise a JSON column value."""
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    """Deserialise a JSON column value, tolerating NULL."""
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        logger.warning(
            "Malformed JSON in database column; falling back to default",
            extra={"error_code": ErrorCode.DATABASE_ERROR},
        )
        return default


def _summarise(sql: str) -> str:
    """Return a short single-line form of a statement, for error details."""
    collapsed = " ".join(sql.split())
    return collapsed[:200] + ("..." if len(collapsed) > 200 else "")


__all__ = ["Database", "connect", "dumps", "loads"]
