"""What the base frames would have said, had anyone asked them (V2-P0.4).

The exclusion layer refuses what a detector saw. On the acceptance render it
left a three-second pause menu at 2:43, and it did not miss it -- nothing had
looked: the nearest OCR and vision samples sat 11.4 s apart, both gameplay.
The FRAMES stage had extracted a frame every 3 s the whole time, one of them
showing the menu in full, and OCR reads candidate frames only.

This script measures the pass before it is built: it runs the real OCR engine
over the base frames that fall inside the *planned* clips, feeds the reads
into ``content.read`` next to the stored ones, and reports what changes.

    python scripts/base_frame_reads.py                 # the benchmark project
    python scripts/base_frame_reads.py --project ID
    python scripts/base_frame_reads.py --min-distance 2 # only frames nobody sampled near

Two numbers matter. How many of the unsampled stretches inside the edit the
pass now observes -- coverage. And how many seconds of *gameplay* it newly
refuses -- the cost, and the one that decides whether this ships. A pass that
finds the menu and also refuses ten seconds of a firefight because the OCR
read a shop sign is not a pass.

**It writes nothing to the database.** Reads are cached on disk under
``.cache/base_frame_reads/`` so re-running the comparison is free; the cache
is per project and per engine.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import logging

logging.disable(logging.WARNING)

import backend.database.repositories  # noqa: F401  (registers repositories)
from ai.ocr import create_ocr_provider
from ai.providers.base import TextDetection
from backend.analysis import frame_state
from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.database.connection import Database
from backend.database.repositories.frames import FrameRepository
from backend.database.repositories.gaming import OcrRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.vision import VisionRepository
from backend.gaming import content
from backend.gaming.ocr import read_frames
from backend.gaming.profiles import GENERIC_PROFILE, load_profile

BENCHMARK: str = "proj-5db1780821a6"

#: A second is "observed" when a detector looked this close to it. The same
#: radius bridge_safety.py uses, so the two measurements agree on the word.
OBSERVED_RADIUS: float = 3.5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", default=BENCHMARK)
    parser.add_argument(
        "--min-distance",
        type=float,
        default=0.0,
        help="read only base frames at least this far from any stored sample (default: all)",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=3.0,
        help="also read base frames this close outside a planned clip (default: one "
        "base interval, 3 s) -- a menu seen just before a clip opens reaches into it",
    )
    parser.add_argument("--no-cache", action="store_true", help="read the frames again")
    arguments = parser.parse_args()

    root = find_repository_root()
    config = load_config()
    paths = build_paths(config, root=root)
    database = Database(paths.database_path, config.application.database)
    cache_dir = root / ".cache" / "base_frame_reads"
    cache_dir.mkdir(parents=True, exist_ok=True)

    media = MediaRepository(database).list_for_project(arguments.project)
    if not media:
        print(f"no media for {arguments.project}")
        return 1

    provider = create_ocr_provider(config)
    if provider is None or not provider.is_available():
        print("no usable OCR engine on this machine")
        return 1
    engine = provider.info().provider

    print("=" * 78)
    print(f"BASE FRAME READS  ·  {arguments.project}  ·  engine {engine}")
    print("=" * 78)

    totals = {
        "frames": 0,
        "with_text": 0,
        "seconds_read": 0.0,
        "unsampled_stretches": 0,
        "now_observed": 0,
        "new_excluded_in_clips": 0.0,
        "clips_touched": 0,
    }
    for item in media:
        duration = float(getattr(getattr(item, "metadata", None), "duration_seconds", 0.0) or 0.0)
        if not duration:
            continue
        clips = _clips(database, arguments.project, item.id)
        if not clips:
            continue
        stored = OcrRepository(database).list_for_media(item.id)
        observations = VisionRepository(database).list_for_media(item.id)
        looked = sorted(
            {d.timestamp for d in stored}
            | {float(getattr(o, "timestamp", 0.0)) for o in observations}
        )
        frames = _base_frames_inside(
            database, item.id, clips, looked, arguments.min_distance, arguments.margin
        )
        print(f"\nrecording {item.id}   {duration / 60:.1f} min   {len(clips)} clips")
        print(f"  base frames inside the planned clips: {len(frames)}")
        if not frames:
            continue

        cache = cache_dir / f"{arguments.project}-{item.id}-{engine}.json"
        reads, seconds = _read(frames, provider, config, paths, cache, refresh=arguments.no_cache)
        print(f"  frames with text: {len({d.timestamp for d in reads})}   read in {seconds:.0f}s")

        profile = _profile(database, paths, item.id)
        frame_spans = frame_state.non_gameplay(
            frame_state.spans(observations, duration_seconds=duration)
        )
        before = content.excluded_spans(
            content.read(
                detections=stored,
                frame_spans=frame_spans,
                profile=profile,
                duration_seconds=duration,
            ),
            observed_at=looked,
        )
        after_looked = sorted(set(looked) | {t for t, _ in frames})
        after = content.excluded_spans(
            content.read(
                detections=[*stored, *reads],
                frame_spans=frame_spans,
                profile=profile,
                duration_seconds=duration,
            ),
            observed_at=after_looked,
        )
        added = _difference(after, before)
        stretches = _unsampled_stretches(clips, looked)
        observed_now = [(lo, hi) for lo, hi in stretches if any(lo <= t <= hi for t, _ in frames)]
        newly_excluded = _inside_clips(added, clips)
        touched = {
            index for index, (lo, hi) in enumerate(clips) for a, b in added if a < hi and lo < b
        }

        print(f"  excluded spans: {len(before)} before, {len(after)} after, {len(added)} new")
        print(
            f"  unsampled stretches (>= 2s, no sample within {OBSERVED_RADIUS}s) inside the edit: "
            f"{len(stretches)}, now observed: {len(observed_now)}"
        )
        print(
            f"  newly excluded inside planned clips: {newly_excluded:.2f}s "
            f"over {len(touched)} clips"
        )
        if added:
            print("\n  NEW EXCLUSIONS -- each one is a claim to check against the frame:")
            by_time = sorted(reads, key=lambda d: d.timestamp)
            for a, b in added:
                texts = [
                    f"{d.timestamp:.0f}s {d.text!r}"
                    for d in by_time
                    if a - 3.0 <= d.timestamp <= b + 3.0
                    and any(
                        rule.matches(d.text, region=d.region) for rule in content.rules_for(profile)
                    )
                ]
                in_clip = _inside_clips([(a, b)], clips)
                print(
                    f"    [{a:8.1f} -- {b:8.1f}]  {b - a:5.1f}s  in clips {in_clip:5.1f}s   "
                    + "; ".join(texts[:4])
                )

        totals["frames"] += len(frames)
        totals["with_text"] += len({d.timestamp for d in reads})
        totals["seconds_read"] += seconds
        totals["unsampled_stretches"] += len(stretches)
        totals["now_observed"] += len(observed_now)
        totals["new_excluded_in_clips"] += newly_excluded
        totals["clips_touched"] += len(touched)

    database.close()
    print("\n" + "=" * 78)
    print("TOTAL")
    print(f"  frames read            : {totals['frames']}  ({totals['seconds_read']:.0f}s of OCR)")
    print(
        f"  unsampled stretches    : {totals['unsampled_stretches']}, "
        f"now observed {totals['now_observed']}"
    )
    print(
        f"  newly excluded in edit : {totals['new_excluded_in_clips']:.2f}s over "
        f"{totals['clips_touched']} clips"
    )
    print("  every new exclusion above must be checked against its frame before this ships")
    print("=" * 78)
    return 0


def _read(frames, provider, config, paths, cache: Path, *, refresh: bool):
    """OCR the frames that are not cached yet, and return every read.

    The cache is per frame, keyed by timestamp, and a frame that read as
    nothing is cached as nothing -- so widening the selection costs only the
    frames it adds.
    """
    known: dict[str, list[dict]] = {}
    seconds = 0.0
    if cache.is_file() and not refresh:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        known = payload["frames"]
        seconds = float(payload["seconds"])

    pending = [(at, path) for at, path in frames if f"{at:.3f}" not in known]
    if pending:
        work_dir = cache.parent / "crops"
        started = time.monotonic()
        try:
            provider.load()
            results = read_frames(
                pending, provider, config.analysis.ocr, GENERIC_PROFILE, work_dir=work_dir
            )
        finally:
            provider.unload()
            shutil.rmtree(work_dir, ignore_errors=True)
        seconds += time.monotonic() - started
        for at, _ in pending:
            known[f"{at:.3f}"] = []
        for frame in results:
            known[f"{frame.timestamp:.3f}"] = [
                {
                    "text": d.text,
                    "confidence": d.confidence,
                    "timestamp": d.timestamp,
                    "region": d.region,
                    "box": list(d.box) if d.box else None,
                }
                for d in frame.detections
            ]
        cache.write_text(
            json.dumps({"seconds": seconds, "frames": known}, indent=1), encoding="utf-8"
        )
        print(f"  read {len(pending)} frames not yet cached")

    wanted = {f"{at:.3f}" for at, _ in frames}
    reads = [
        TextDetection(**row)
        for key, rows in known.items()
        if key in wanted
        for row in rows
    ]
    return reads, seconds


def _base_frames_inside(database, media_id, clips, looked, min_distance, margin=0.0):
    rows = FrameRepository(database).list_for_media(media_id, level="base")
    chosen = []
    for row in rows:
        at = float(row.timestamp)
        if not any(lo - margin <= at <= hi + margin for lo, hi in clips):
            continue
        if min_distance > 0 and _distance(looked, at) < min_distance:
            continue
        if Path(row.image_path).is_file():
            chosen.append((at, Path(row.image_path)))
    return chosen


def _distance(looked, at):
    index = bisect.bisect_left(looked, at)
    best = float("inf")
    for j in (index - 1, index):
        if 0 <= j < len(looked):
            best = min(best, abs(looked[j] - at))
    return best


def _unsampled_stretches(clips, looked, *, radius=OBSERVED_RADIUS, step=0.25):
    """Stretches of >= 2 s inside the planned clips that no sample sits near."""
    runs = []
    for lo, hi in clips:
        at, current = lo, None
        while at < hi:
            if _distance(looked, at) > radius:
                if current is None:
                    current = [at, at]
                    runs.append(current)
                current[1] = at
            else:
                current = None
            at += step
    return [(a, b) for a, b in runs if b - a >= 2.0]


def _difference(after, before):
    """Parts of ``after`` that ``before`` does not cover."""
    pieces = []
    for a, b in after:
        cursor = a
        for lo, hi in sorted(before):
            if hi <= cursor or lo >= b:
                continue
            if lo > cursor:
                pieces.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < b:
            pieces.append((cursor, b))
    return pieces


def _inside_clips(spans, clips):
    total = 0.0
    for a, b in spans:
        for lo, hi in clips:
            total += max(0.0, min(b, hi) - max(a, lo))
    return total


def _clips(database, project_id, media_id):
    rows = database.fetch_all(
        "SELECT source_in, source_out FROM timeline_clips "
        "WHERE project_id = ? AND media_id = ? AND enabled = 1 ORDER BY clip_index",
        (project_id, media_id),
    )
    return [(float(r["source_in"]), float(r["source_out"])) for r in rows]


def _profile(database, paths, media_id):
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
