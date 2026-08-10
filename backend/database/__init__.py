"""SQLite persistence (SPEC sections 44, 45).

The database stores entities and their relationships. It holds no pipeline
logic, no rendering knowledge and no AI behaviour -- those live in their own
layers and reach persistence through repositories.
"""

from backend.database.connection import Database, connect, dumps, loads
from backend.database.migrator import current_version, migrate, verify_schema_version

__all__ = [
    "Database",
    "connect",
    "current_version",
    "dumps",
    "loads",
    "migrate",
    "verify_schema_version",
]
