"""Read what the audience did with the videos this system published (V2-P9).

Explicit on purpose. There is no ambient polling and no automatic stage: this
reads the owner's own analytics with the owner's own credentials, and a network
call made on someone's behalf is asked for, not assumed -- the same rule §51
applies to publishing, applied to reading.

    python scripts/fetch_outcomes.py               # every published video
    python scripts/fetch_outcomes.py --days 7      # a shorter window
    python scripts/fetch_outcomes.py --video ID    # just one

It stores nothing it cannot attribute: a video with no publication row has no
edit behind it, and an outcome nobody can trace to a decision is a number
rather than evidence.

Nothing here learns. The numbers are recorded and shown; changing a decision
because of them is P10's job, inside the bounds P8 declared.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.analytics.projection import project
from backend.analytics.store import OutcomeStore
from backend.analytics.youtube import YouTubeAnalytics
from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.database.connection import Database
from backend.database.migrator import migrate

#: How far back a fetch looks when nobody says. Long enough that a video has
#: had its first week, short enough that the window still describes one video's
#: life rather than the channel's history.
DEFAULT_DAYS: int = 28


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--video", default=None, help="one video id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would be fetched, and ask YouTube for nothing",
    )
    args = parser.parse_args()

    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    database = Database(paths.database_path, config.application.database)
    migrate(database)
    store = OutcomeStore(database)

    published = store.published()
    if args.video:
        published = [row for row in published if row["video_id"] == args.video]

    if not published:
        print(
            "Nothing to measure: no video in this database has been published "
            "through this system.\n"
            "  A video on the channel that this system did not publish has no "
            "edit behind it,\n"
            "  so there is nothing to attribute an outcome to. Publish through "
            "the Export screen\n"
            "  and the row appears here.",
        )
        print(f"\noutcomes stored so far: {store.count()}")
        database.close()
        return 0

    end = date.today()
    start = end - timedelta(days=max(1, args.days))
    print(f"window {start.isoformat()} .. {end.isoformat()}  ({len(published)} video(s))")

    if args.dry_run:
        for row in published:
            print(f"  would fetch {row['video_id']}  project {row['project_id']}")
        database.close()
        return 0

    analytics = _client(config, paths)
    if analytics is None:
        print(
            "\nYouTube is not configured on this machine: no client id or no "
            "secret file.\nNothing was fetched."
        )
        database.close()
        return 2
    refusal = analytics.why_not()
    if refusal:
        print(f"\n{refusal}")
        print(
            "\nNothing was fetched and nothing was written. Uploading is "
            "unaffected;\nthe analytics scope is only needed to read."
        )
        database.close()
        return 2

    read = 0
    for row in published:
        video_id = row["video_id"]
        try:
            totals = analytics.totals(
                video_id, start_date=start.isoformat(), end_date=end.isoformat()
            )
            points = analytics.retention(
                video_id, start_date=start.isoformat(), end_date=end.isoformat()
            )
            outcome = store.record(totals, points)
        except Exception as error:
            print(f"  {video_id}: {type(error).__name__}: {str(error)[:160]}")
            continue
        read += 1
        views = outcome.metrics.get("views")
        share = outcome.metrics.get("averageViewPercentage")
        print(
            f"  {video_id}  views {views if views is not None else '-'}  "
            f"average share {f'{share:.0f}%' if share is not None else '-'}  "
            f"curve {len(outcome.points)} point(s)  style {row.get('style') or '-'}"
        )
        _report(database, config, outcome)

    print(f"\n{read} of {len(published)} video(s) measured; {store.count()} stored")
    database.close()
    return 0


def _report(database, config, outcome) -> None:
    """The curve beside the edit, when both are available."""
    projection = project(database, outcome, config=config)
    if projection is None:
        return
    print(
        f"      {projection.duration_seconds:.0f}s video, "
        f"{projection.matched_fraction:.0%} of the curve placed on a shot"
        + (f", style {projection.style}" if projection.style else "")
    )
    for dip in projection.dips[:3]:
        print(f"      - {dip.describe()}")


def _client(config, paths) -> YouTubeAnalytics | None:
    """The analytics reader, sharing the publisher's own OAuth session.

    Not a second credential path: the token, the client id and the secret file
    are already resolved once for publishing, and a reader that found them its
    own way would be a second place to get them wrong.
    """
    from backend.publishing import build_token_provider

    tokens = build_token_provider(config, paths.data_root)
    return None if tokens is None else YouTubeAnalytics(tokens)


if __name__ == "__main__":
    raise SystemExit(main())
