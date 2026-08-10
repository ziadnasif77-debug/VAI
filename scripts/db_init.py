#!/usr/bin/env python3
"""Create or migrate the application database.

    python scripts/db_init.py [--data-root PATH]

Idempotent: running it against an up-to-date database applies nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.loader import load_config
from backend.config.paths import build_paths
from backend.database.connection import Database
from backend.database.migrator import current_version, migrate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Override where the database and projects live.",
    )
    arguments = parser.parse_args()

    config = load_config()
    paths = build_paths(config, data_root=arguments.data_root).create()

    print(f"Database: {paths.database_path}")
    database = Database(paths.database_path, config.application.database)
    try:
        before = current_version(database)
        applied = migrate(database)
        after = current_version(database)
    finally:
        database.close()

    if applied:
        for migration in applied:
            print(f"  applied {migration.version:04d}_{migration.name}")
        print(f"Schema version {before} -> {after}")
    else:
        print(f"Already up to date (schema version {after}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
