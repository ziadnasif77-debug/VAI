"""Help a person annotate a recording for the golden dataset (SPEC §117).

Annotating an hour of gameplay by scrubbing a video player is the reason golden
datasets do not get built. This makes it a reviewable sheet instead: frames at
a fixed interval, tiled, with a printed index so every tile has a timestamp.

The interval is *fixed* on purpose. Sampling where the pipeline already found
something would produce labels that agree with it by construction, and a
benchmark built that way reports the system's own opinion back to itself. What
is wanted is a systematic sweep with no idea what the system thinks.

    python scripts/annotate.py "D:/Gaming 2026/session.mkv" --from 30:00 --to 40:00

Writes contact sheets and a skeleton dataset next to them. The skeleton has the
recording's identity filled in and no spans: the spans are the human's job, and
this deliberately cannot guess them.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.duration import parse_duration
from backend.quality.dataset import DATASET_SUFFIX, SCHEMA_VERSION

TILE_WIDTH = 480
COLUMNS = 5
ROWS = 4


def probe(source: str) -> tuple[float, int]:
    """The recording's duration and size."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", source],
        capture_output=True, text=True, check=False,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        duration = 0.0
    return duration, Path(source).stat().st_size


def extract(source: str, seconds: float, target: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-ss", f"{seconds:.3f}", "-i", source, "-frames:v", "1",
         "-vf", f"scale={TILE_WIDTH}:-2", "-q:v", "3", "-y", str(target),
         "-loglevel", "error"],
        capture_output=True, check=False,
    )
    return result.returncode == 0 and target.is_file()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--from", dest="start", default="0:00", help="M:SS or H:MM:SS")
    parser.add_argument("--to", dest="end", default=None, help="M:SS or H:MM:SS")
    parser.add_argument("--every", type=float, default=5.0, help="seconds between frames")
    parser.add_argument("--out", default=None, help="where the sheets go")
    parser.add_argument("--game", default="")
    args = parser.parse_args()

    source = args.source
    if not Path(source).is_file():
        print(f"No such recording: {source}")
        return 1

    duration, size = probe(source)
    if duration <= 0:
        print("Could not read the recording's duration.")
        return 1

    start = parse_duration(args.start)
    end = parse_duration(args.end) if args.end else duration
    end = min(end, duration)
    if end <= start:
        print("The window must end after it starts.")
        return 1

    out = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / ".tmp" / "annotate"
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.jpg"):
        stale.unlink()

    times = []
    seconds = start
    while seconds < end:
        times.append(seconds)
        seconds += args.every

    print(f"{Path(source).name}: {duration / 60:.1f} min")
    print(f"window {args.start} -> {args.end or 'end'}  ({len(times)} frames every {args.every}s)")

    per_sheet = COLUMNS * ROWS
    sheets = 0
    index_lines: list[str] = []
    for sheet_number, offset in enumerate(range(0, len(times), per_sheet), start=1):
        batch = times[offset : offset + per_sheet]
        paths: list[Path] = []
        for position, moment in enumerate(batch):
            tile = out / f"tile_{position:02d}.jpg"
            if extract(source, moment, tile):
                paths.append(tile)
                minutes, secs = divmod(int(moment), 60)
                index_lines.append(
                    f"sheet {sheet_number} pos {position:2d} (row {position // COLUMNS + 1}, "
                    f"col {position % COLUMNS + 1}) = {minutes}:{secs:02d}"
                )
        if not paths:
            continue
        sheet = out / f"sheet_{sheet_number:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-start_number", "0", "-i", str(out / "tile_%02d.jpg"),
             "-vf", f"tile={COLUMNS}x{ROWS}", "-q:v", "3", "-y", str(sheet),
             "-loglevel", "error"],
            capture_output=True, check=False,
        )
        for tile in paths:
            tile.unlink(missing_ok=True)
        sheets += 1

    (out / "index.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    skeleton = {
        "schema_version": SCHEMA_VERSION,
        "name": Path(source).stem,
        "description": "",
        "recordings": [
            {
                "source_path": str(Path(source)),
                "size_bytes": size,
                "duration_seconds": round(duration, 3),
                "game": args.game,
                "annotated_from_seconds": round(start, 3),
                "annotated_to_seconds": round(end, 3),
                "spans": [],
                "note": "",
            }
        ],
    }
    skeleton_path = out / f"{Path(source).stem}{DATASET_SUFFIX}"
    skeleton_path.write_text(json.dumps(skeleton, indent=2) + "\n", encoding="utf-8")

    print(f"\n{sheets} sheet(s) in {out}")
    print(f"index:    {out / 'index.txt'}")
    print(f"skeleton: {skeleton_path}")
    print("\nThe spans are yours to write. Nothing here guesses them, on purpose.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
