"""Drive one daily-policy heartbeat by hand.

The scheduler inside ``serve.py`` does this every thirty seconds on its own;
this exists for a person who wants to watch a cycle happen now, or to catch a
day up after the machine slept through 02:00. Idempotent by the same ledger
the scheduler uses: running it five times produces nothing twice.

    python scripts/daily_cycle.py            # one tick, real clock
    python scripts/daily_cycle.py --force    # claim today even before 02:00
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.config.loader import load_config
from backend.config.paths import build_paths
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.services.daily_producer import DailyProducer, tick


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="claim today even if the production time has not arrived yet",
    )
    arguments = parser.parse_args()

    config = load_config()
    paths = build_paths(config).create()
    database = Database(paths.database_path, config.application.database)
    migrate(database)
    producer = DailyProducer(database, paths, config)

    now = producer.now_local()
    if arguments.force:
        production = producer._at(
            producer.today(now), config.daily.production_time
        )
        if now < production:
            now = production

    tick(producer, now)

    day = producer.today(now)
    row = database.fetch_one("SELECT report FROM daily_runs WHERE day = ?", (day,))
    if row is not None and row["report"]:
        print(json.dumps(json.loads(row["report"]), ensure_ascii=False, indent=1))
    else:
        print(f"day {day}: not due yet (production time is "
              f"{config.daily.production_time} {config.daily.timezone})")
    database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
