"""What to look at while labelling the benchmark by hand.

The golden dataset (docs/BRIEF_P0.md, HUMAN-LABELED GOLDEN DATASET) is written
by a person watching the recording. Eighty-eight minutes is a long watch, so
this prints, in source time and in order, everything the pipeline already knows
the position of -- detector events, the moments it proposed with their cores,
the stretches it refused as non-gameplay, and the gaps the bridge closed
between two refusals -- and, on request, a contact sheet for every 30-second
window so a stretch can be found by eye before it is watched.

    python scripts/label_helper.py                       # the candidates, printed
    python scripts/label_helper.py --sheets              # ... and the contact sheets
    python scripts/label_helper.py --from 600 --to 1200  # one stretch of the recording
    python scripts/label_helper.py --project ID --out DIR

**It proposes no label and fills no line.** The CSV's columns are
``start,end,label,note`` in seconds of the source; the label is one of the
eight in ``tests/golden/labels/README.md`` and every one of them is a person's
decision. This script never writes under ``tests/golden`` -- an ``--out`` there
is refused -- and the candidates it prints are the pipeline's own proposals,
not a verdict on them: a moment it lists may be ``unimportant``, a stretch it
never lists may be the best of the session.

The sheets go under the application's cache directory by default
(``<cache>/labeling/<project>/``), never under the repository's tests. Each
sheet is one window of 30 s, six frames five seconds apart, read left to right
and top to bottom: the top row is +0, +5, +10 s from the window's start, the
bottom row +15, +20, +25 s. The window's start is in the file name.
``index.txt`` beside them lists every sheet with its window.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from backend.gaming.exclusions import profile_for

#: The canonical session the brief names.
BENCHMARK: str = "proj-5db1780821a6"
#: One contact sheet per this many seconds of source.
WINDOW_SECONDS: float = 30.0
#: Frames per sheet, this far apart. Six frames at 5 s cover the window.
FRAME_EVERY_SECONDS: float = 5.0
SHEET_COLUMNS, SHEET_ROWS = 3, 2
FRAME_WIDTH = 640
#: The directory this script must never write into (HUMAN-LABELED).
LABELS_DIR = Path(__file__).resolve().parents[1] / "tests" / "golden"


@dataclass(frozen=True, slots=True)
class Candidate:
    start: float
    end: float
    kind: str
    detail: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", default=BENCHMARK, help="project to list")
    parser.add_argument("--sheets", action="store_true", help="also write the contact sheets")
    parser.add_argument("--from", dest="start", type=float, default=0.0, help="seconds from")
    parser.add_argument("--to", dest="end", type=float, default=None, help="seconds to")
    parser.add_argument("--out", type=Path, default=None, help="where the sheets go")
    arguments = parser.parse_args(argv)

    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    out = arguments.out or (paths.cache_dir / "labeling" / arguments.project)
    if _inside(out, LABELS_DIR):
        print(f"refused: {out} lies under {LABELS_DIR}; the labels are written by a person")
        return 2

    database = Database(paths.database_path, config.application.database)
    try:
        media = MediaRepository(database).list_for_project(arguments.project)
        if not media:
            print(f"no media for {arguments.project}")
            return 1
        for item in media:
            duration = float(
                getattr(getattr(item, "metadata", None), "duration_seconds", 0.0) or 0.0
            )
            if duration <= 0.0:
                print(f"{item.id}: no duration stored; skipped")
                continue
            low = max(0.0, arguments.start)
            high = min(duration, arguments.end) if arguments.end is not None else duration
            candidates = _candidates(database, paths.profiles_dir, item, duration)
            _print(arguments.project, item, duration, candidates, low, high)
            if arguments.sheets:
                _sheets(config, item, out, low, high)
    finally:
        database.close()
    return 0


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


def _candidates(
    database: Database, profiles_dir: Path, item: Any, duration: float
) -> list[Candidate]:
    found: list[Candidate] = []

    for row in database.fetch_all(
        "SELECT event_type, start_seconds, end_seconds, confidence, sources "
        "FROM game_events WHERE media_id = ? ORDER BY start_seconds",
        (item.id,),
    ):
        found.append(
            Candidate(
                float(row["start_seconds"]),
                float(row["end_seconds"]),
                "event",
                f"{row['event_type']}  conf {float(row['confidence']):.2f}  {row['sources']}",
            )
        )

    for row in database.fetch_all(
        "SELECT moment_type, start_seconds, end_seconds, context_start, context_end, score "
        "FROM moments WHERE media_id = ? ORDER BY context_start",
        (item.id,),
    ):
        found.append(
            Candidate(
                float(row["context_start"]),
                float(row["context_end"]),
                "moment",
                f"{row['moment_type']}  core {float(row['start_seconds']):.1f}"
                f"-{float(row['end_seconds']):.1f}  score {float(row['score']):.2f}",
            )
        )

    detections = OcrRepository(database).list_for_media(item.id)
    observations = VisionRepository(database).list_for_media(item.id)
    non_gameplay = frame_state.non_gameplay(
        frame_state.spans(observations, duration_seconds=duration)
    )
    states = content.read(
        detections=detections,
        frame_spans=non_gameplay,
        profile=profile_for(database, item.id, profiles_dir),
        duration_seconds=duration,
    )
    looked = [d.timestamp for d in detections] + [
        float(getattr(o, "timestamp", 0.0)) for o in observations
    ]
    seen = content.gameplay_samples(states, detections, observations, non_gameplay)
    bridged = content.excluded_spans(states, observed_at=looked, gameplay_at=seen)
    unbridged = content.excluded_spans(states, bridge_seconds=0.0, gameplay_at=seen)

    excluding = [s for s in states if s.excludes]
    for start, end in bridged:
        names = sorted(
            {s.state.value for s in excluding if s.start < end and s.end > start}
        )
        found.append(Candidate(start, end, "exclusion", " / ".join(names) or "?"))

    for start, end in _pieces_outside(bridged, unbridged):
        found.append(
            Candidate(
                start, end, "gap",
                "closed by the bridge between two refusals; nobody sampled it",
            )
        )

    return sorted(found, key=lambda c: (c.start, c.end, c.kind))


def _pieces_outside(
    spans: list[tuple[float, float]], covered: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """The parts of ``spans`` inside none of ``covered``."""
    out: list[tuple[float, float]] = []
    for span in spans:
        pieces = [span]
        for lo, hi in sorted(covered):
            next_pieces = []
            for a, b in pieces:
                if hi <= a or lo >= b:
                    next_pieces.append((a, b))
                    continue
                if a < lo:
                    next_pieces.append((a, lo))
                if hi < b:
                    next_pieces.append((hi, b))
            pieces = next_pieces
        out.extend((a, b) for a, b in pieces if b - a > 0.01)
    return sorted(out)


def _print(
    project: str, item: Any, duration: float, candidates: list[Candidate], low: float, high: float
) -> None:
    name = Path(str(getattr(item, "source_path", "") or "")).name or item.id
    print("=" * 96)
    print(f"LABEL HELPER  ·  {project}  ·  {item.id}  ·  {name}  ·  {duration / 60:.1f} min")
    print("=" * 96)
    print(
        "CSV: start,end,label,note   "
        "(seconds of the source; the label is yours -- none is proposed)"
    )
    print(
        "labels: best_moment unimportant event_start payoff reaction "
        "dead_time failed_attempt non_gameplay"
    )
    print(f"showing {low:.1f}-{high:.1f} s")
    print()
    print(f"{'at':>9}  {'start':>9}  {'end':>9}  {'kind':<9}  detail")
    shown: dict[str, int] = {}
    for c in candidates:
        if c.end < low or c.start > high:
            continue
        shown[c.kind] = shown.get(c.kind, 0) + 1
        print(f"{_clock(c.start):>9}  {c.start:9.3f}  {c.end:9.3f}  {c.kind:<9}  {c.detail}")
    print()
    print("  ".join(f"{kind}: {n}" for kind, n in sorted(shown.items())) or "nothing in range")


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


# ---------------------------------------------------------------------------
# contact sheets
# ---------------------------------------------------------------------------


def _sheets(config: Any, item: Any, out: Path, low: float, high: float) -> None:
    source = Path(str(item.source_path))
    if not source.is_file():
        print(f"\nno sheets: the recording is not at {source}")
        return
    binary = _ffmpeg(config)
    if binary is None:
        print("\nno sheets: ffmpeg is not on PATH and not configured")
        return
    out.mkdir(parents=True, exist_ok=True)
    index = out / "index.txt"
    lines = [
        f"# contact sheets for {item.id}: one per {WINDOW_SECONDS:.0f} s window, "
        f"{SHEET_COLUMNS * SHEET_ROWS} frames {FRAME_EVERY_SECONDS:.0f} s apart, "
        "left to right then top to bottom, from the window's start",
        "# sheet  window_start_s  window_end_s",
    ]
    made = skipped = 0
    at = low - (low % WINDOW_SECONDS)
    while at < high:
        target = out / f"sheet_{int(at):05d}s_{_clock(at).replace(':', '-')}.jpg"
        lines.append(f"{target.name}  {at:.0f}  {at + WINDOW_SECONDS:.0f}")
        if target.is_file():
            skipped += 1
        elif _sheet(binary, source, at, target):
            made += 1
        at += WINDOW_SECONDS
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{made} sheet(s) written, {skipped} already there -> {out}")


def _sheet(binary: str, source: Path, at: float, target: Path) -> bool:
    """One window's sheet. Seeks before opening the file: an OBS recording
    can be corrupt mid-file, and a seeked window reads around that."""
    command = [
        binary, "-hide_banner", "-loglevel", "error", "-y",
        "-skip_frame", "nokey",
        "-ss", f"{at:.3f}", "-t", f"{WINDOW_SECONDS:.3f}",
        "-i", str(source),
        "-an", "-vf",
        f"fps=1/{FRAME_EVERY_SECONDS:g},scale={FRAME_WIDTH}:-1,"
        f"tile={SHEET_COLUMNS}x{SHEET_ROWS}",
        "-frames:v", "1", "-q:v", "4", str(target),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"\n  {target.name}: {error}")
        return False
    if completed.returncode != 0 or not target.is_file():
        detail = completed.stderr.strip()[:160]
        print(f"\n  {target.name}: ffmpeg exit {completed.returncode} {detail}")
        return False
    return True


def _ffmpeg(config: Any) -> str | None:
    with contextlib.suppress(Exception):
        from backend.media.ffmpeg import FFmpegRunner

        return FFmpegRunner(config.ffmpeg).ffmpeg_path
    return shutil.which("ffmpeg")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
