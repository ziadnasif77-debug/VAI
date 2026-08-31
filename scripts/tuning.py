"""Controlled tuning: look at it, and undo it (V2-P10).

    python scripts/tuning.py status              # what is in force, and why not more
    python scripts/tuning.py propose             # what the evidence would suggest
    python scripts/tuning.py apply KEY           # record one suggested step
    python scripts/tuning.py revert ID           # undo one
    python scripts/tuning.py revert --all        # undo everything, always works

``revert --all`` is the command that matters. A mechanism allowed to change the
channel without being asked needs a way back that takes one command and no
thought, and it must work whatever state the ledger is in -- so it marks every
active row and never has to reconstruct anything, because the file was never
rewritten in the first place.

Today every one of these prints a refusal: the switch is off and no video has
been measured. That is the intended state, and printing *why* is the point.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.analytics.store import OutcomeStore
from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.tuning.deltas import RefusedError, TuningLedger
from backend.tuning.proposer import propose, tunable_keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    proposing = sub.add_parser("propose")
    proposing.add_argument("--style", default=None)
    applying = sub.add_parser("apply")
    applying.add_argument("key")
    applying.add_argument("--style", default=None)
    reverting = sub.add_parser("revert")
    reverting.add_argument("delta_id", nargs="?", default=None)
    reverting.add_argument("--all", action="store_true")
    args = parser.parse_args()

    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    database = Database(paths.database_path, config.application.database)
    migrate(database)
    ledger = TuningLedger(database, config)
    style = getattr(args, "style", None) or config.style.default

    try:
        if args.command == "status":
            return _status(database, config, ledger)
        if args.command == "propose":
            return _propose(database, config, style)
        if args.command == "apply":
            return _apply(database, config, ledger, style, args.key)
        if args.command == "revert":
            return _revert(ledger, args)
    finally:
        database.close()
    return 0


def _status(database, config, ledger) -> int:
    tuning = config.style.tuning
    measured = OutcomeStore(database).count()
    print(f"switch          {'on' if tuning.enabled else 'OFF'}")
    print(f"measured videos {measured} (need {tuning.minimum_videos} to propose)")
    print(f"largest step    {tuning.max_step_fraction:.0%} of a key's declared range")
    print(f"cooldown        {tuning.cooldown_videos} video(s) before a key moves again")
    print(f"tunable keys    {len(tunable_keys(config))}")

    active = [
        item for name in config.style.bible for item in ledger.active(name)
    ]
    print(f"\nin force        {len(active)} adjustment(s)")
    for item in active:
        print(f"  {item.id}  {item.describe()}")
    if not active:
        print("  nothing: every style is exactly what config/style.yaml says")

    past = [item for item in ledger.history() if item.status != "active"]
    if past:
        print(f"\nended           {len(past)}")
        for item in past[:10]:
            print(f"  {item.status:<11} {item.describe()}")
    return 0


def _propose(database, config, style: str) -> int:
    print(f"style {style}, metric {config.style.tuning.metric}\n")
    suggested = 0
    for key in tunable_keys(config):
        proposal = propose(database, config, style=style, key=key)
        if proposal.refusal:
            print(f"  {key:<38} {proposal.refusal}")
            continue
        suggested += 1
        print(f"  {key:<38} {proposal.base_value:g} -> "
              f"{proposal.base_value + proposal.delta:g}")
        print(f"  {'':38} {proposal.reason()}")
        for note in proposal.notes:
            print(f"  {'':38} ({note})")
    print(f"\n{suggested} suggestion(s)")
    return 0


def _apply(database, config, ledger, style: str, key: str) -> int:
    proposal = propose(database, config, style=style, key=key)
    if proposal.refusal:
        print(f"No change: {proposal.refusal}")
        return 2
    try:
        recorded = ledger.apply(
            style=style,
            key=key,
            delta=proposal.delta,
            reason=proposal.reason(),
            evidence=proposal.evidence(),
            videos=proposal.videos,
        )
    except RefusedError as refusal:
        print(f"Refused: {refusal}")
        return 2
    print(f"Applied {recorded.id}: {recorded.describe()}")
    print("Undo with:  python scripts/tuning.py revert " + recorded.id)
    return 0


def _revert(ledger, args) -> int:
    if args.all:
        undone = ledger.revert_all()
        print(f"{undone} adjustment(s) reverted; every style is the file again")
        return 0
    if not args.delta_id:
        print("Give a delta id, or --all")
        return 2
    if ledger.revert(args.delta_id):
        print(f"{args.delta_id} reverted")
        return 0
    print(f"{args.delta_id} is not an active adjustment")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
