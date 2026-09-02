"""What the bridge costs, in seconds of gameplay nobody meant to lose.

V2-P0.2 closes short gaps between two refusals. A menu read at one instant and
a title card read at another leave an island between them that no detector ever
sampled, and the first render built with this layer carried three frames of a
loading screen out of exactly such an island -- 1.75 seconds called gameplay
because nobody looked, not because anybody saw a game.

Closing those gaps is right, and it is also the one part of the exclusion layer
that can take footage nobody asked it to take. This script measures that,
because "the bridge looks safe" is not a number and the acceptance gate wants
numbers.

    python scripts/bridge_safety.py                    # the benchmark project
    python scripts/bridge_safety.py --project ID
    python scripts/bridge_safety.py --window 15        # a wider neighbourhood

**It renders nothing and analyses nothing.** It reads the stored OCR, vision and
timeline, recomputes the excluded spans twice -- once with bridging off, once
with it on -- and compares. Computing both from the *same* inputs is the whole
point: the pre-bridge render on disk also predates the moment-level guard, so
comparing the two rendered files would confound two changes and answer a
different question than the one asked.

**"Observed gameplay" is deliberately narrow.** A second next to a menu counts
only when a detector actually looked at it and did not call it non-gameplay. A
second nobody sampled is not gameplay that was lost -- it is a second nobody can
speak for, and counting it either way would be inventing evidence.
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

import logging

logging.disable(logging.WARNING)

import backend.database.repositories  # noqa: F401  (registers repositories)
from backend.analysis import frame_state
from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.database.connection import Database
from backend.database.repositories.gaming import OcrRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.vision import VisionRepository
from backend.gaming import content
from backend.gaming.profiles import GENERIC_PROFILE, load_profile

#: The benchmark this layer was written against and is accepted on.
BENCHMARK: str = "proj-5db1780821a6"

#: How far either side of an excluded span to look for gameplay the bridge
#: might have eaten.
#:
#: Ten seconds, and the reason is measured rather than chosen: the widest gap
#: the bridge closes on this benchmark is 5.4s, so a ten-second neighbourhood
#: brackets every bridge with room on both sides. Narrower would miss footage
#: just outside a bridge; much wider would count gameplay no bridge could have
#: reached and dilute the number the gate reads. Override with ``--window``.
NEIGHBOUR_SECONDS: float = 10.0

#: A detector's word covers this much either side of the instant it looked.
#: Half the OCR sampling interval on the benchmark (7.1s), so the windows a
#: sample speaks for meet without overlapping into a claim nobody made.
OBSERVED_RADIUS: float = 3.5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=BENCHMARK, help="project to measure")
    parser.add_argument(
        "--window",
        type=float,
        default=NEIGHBOUR_SECONDS,
        help=f"seconds either side of an excluded span (default {NEIGHBOUR_SECONDS})",
    )
    arguments = parser.parse_args()

    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    database = Database(paths.database_path, config.application.database)

    media = MediaRepository(database).list_for_project(arguments.project)
    if not media:
        print(f"no media for {arguments.project}")
        return 1

    clips = _clips(database, arguments.project)
    print("=" * 78)
    print(f"BRIDGE SAFETY  ·  {arguments.project}  ·  window ±{arguments.window:.1f}s")
    print("=" * 78)

    totals = {"neighbouring": 0.0, "retained_off": 0.0, "retained_on": 0.0}
    for item in media:
        duration = float(getattr(getattr(item, "metadata", None), "duration_seconds", 0.0) or 0.0)
        if not duration:
            continue
        detections = OcrRepository(database).list_for_media(item.id)
        observations = VisionRepository(database).list_for_media(item.id)
        states = content.read(
            detections=detections,
            frame_spans=frame_state.non_gameplay(
                frame_state.spans(observations, duration_seconds=duration)
            ),
            profile=_profile(database, paths, item.id),
            duration_seconds=duration,
        )
        looked = [d.timestamp for d in detections] + [
            float(getattr(o, "timestamp", 0.0)) for o in observations
        ]
        without = content.excluded_spans(states, bridge_seconds=0.0)
        with_bridge = content.excluded_spans(states, observed_at=looked)
        mine = [c for c in clips if c[0] == item.id]

        print(f"\nrecording {item.id}   {duration / 60:.1f} min")
        print(f"  excluded spans: {len(without)} without bridging, {len(with_bridge)} with")
        numbers = _measure(without, with_bridge, looked, mine, arguments.window)
        for key in totals:
            totals[key] += numbers[key]
        _report(numbers)

    database.close()

    print("\n" + "=" * 78)
    print("TOTAL")
    _report(totals)
    removed = totals["retained_off"] - totals["retained_on"]
    print()
    print(f"  GAMEPLAY REMOVED BY THE BRIDGE: {removed:.2f}s")
    print("  " + ("PASS -- the bridge took none of it" if removed <= 0.001 else "FAIL"))
    print("=" * 78)
    return 0 if removed <= 0.001 else 1


def _measure(
    without: list[tuple[float, float]],
    with_bridge: list[tuple[float, float]],
    looked: list[float],
    clips: list[tuple[str, float, float]],
    window: float,
) -> dict[str, float]:
    """Gameplay beside each excluded span, and how much of it each build keeps.

    The neighbourhood is taken from the *unbridged* spans on purpose. Bridged
    spans are wider, so their neighbourhood would start further out and skip
    the very seconds a bridge could have swallowed -- which are the ones this
    measurement exists to find.
    """
    windows: list[tuple[float, float]] = []
    for start, end in without:
        windows.append((max(0.0, start - window), start))
        windows.append((end, end + window))

    neighbouring = 0.0
    retained_off = 0.0
    retained_on = 0.0
    step = 0.05
    for low, high in windows:
        at = low
        while at < high:
            if not _observed(looked, at):
                at += step
                continue
            if _inside(without, at):
                at += step
                continue
            neighbouring += step
            if _inside_clip(clips, at):
                retained_off += step
            if not _inside(with_bridge, at) and _inside_clip(clips, at):
                retained_on += step
            at += step
    return {
        "neighbouring": neighbouring,
        "retained_off": retained_off,
        "retained_on": retained_on,
    }


def _report(numbers: dict[str, float]) -> None:
    removed = numbers["retained_off"] - numbers["retained_on"]
    print(f"  neighbouring gameplay : {numbers['neighbouring']:8.2f}s")
    print(f"  retained, bridge off  : {numbers['retained_off']:8.2f}s")
    print(f"  retained, bridge on   : {numbers['retained_on']:8.2f}s")
    print(f"  removed by the bridge : {removed:8.2f}s")


def _observed(looked: list[float], at: float) -> bool:
    """Whether a detector looked close enough to speak for this instant."""
    return any(abs(when - at) <= OBSERVED_RADIUS for when in looked)


def _inside(spans: list[tuple[float, float]], at: float) -> bool:
    return any(low <= at < high for low, high in spans)


def _inside_clip(clips: list[tuple[str, float, float]], at: float) -> bool:
    return any(low <= at < high for _, low, high in clips)


def _clips(database: Database, project_id: str) -> list[tuple[str, float, float]]:
    rows = database.fetch_all(
        "SELECT media_id, source_in, source_out FROM timeline_clips "
        "WHERE project_id = ? ORDER BY clip_index",
        (project_id,),
    )
    return [(row["media_id"], row["source_in"], row["source_out"]) for row in rows]


def _profile(database: Database, paths, media_id: str):
    """The game's profile, or the generic table.

    A missing profiles directory raises, as it does in the workers (V2-P0.3):
    a measurement taken with every game silently generic is not a measurement
    of the profiles.
    """
    row = database.fetch_one(
        "SELECT game_profile FROM ocr_results WHERE media_id = ? "
        "AND game_profile IS NOT NULL LIMIT 1",
        (media_id,),
    )
    name = str(row["game_profile"]) if row is not None else ""
    if not name:
        return GENERIC_PROFILE
    return load_profile(name, paths.profiles_dir).profile


if __name__ == "__main__":
    raise SystemExit(main())
