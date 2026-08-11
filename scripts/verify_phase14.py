"""Phase 14 acceptance: the profile earns its place, on real footage.

§111 asks for one real game validated before more profiles are written. The
claim that has to be true for the architecture to be worth anything is narrow
and checkable:

    **With a profile, the same recording yields events the generic path
    cannot produce at all.**

So this samples frames from a real GTA V capture, reads them twice — once with
the game's profile and once with the generic one — and reports what each found.
The generic profile declares no HUD, so its wanted-level event count is zero by
construction; what the run measures is whether the profile's count is *usefully*
above zero on footage nobody tuned it against, and how often it declines to
read rather than guessing.

    python scripts/verify_phase14.py "D:/Gaming 2026/2026-05-16 22-24-49.mkv"

Frames are decoded straight from the source with FFmpeg rather than run through
the pipeline: this is a measurement of the reader, and a two-hour analysis in
front of it would only add variables.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.loader import load_config
from backend.gaming.events import observations_from_hud
from backend.gaming.hud import HudReading, ReadingQuality, read_frame, track
from backend.gaming.profiles import GENERIC_PROFILE, load_profile

DEFAULT_SOURCE = "D:/Gaming 2026/2026-05-16 22-24-49.mkv"


def decode(source: str, seconds: float):
    """One RGB frame, straight from the source."""
    import numpy as np
    from PIL import Image

    result = subprocess.run(
        [
            "ffmpeg", "-ss", str(seconds), "-i", source, "-frames:v", "1",
            "-f", "image2pipe", "-vcodec", "png", "-loglevel", "error", "-",
        ],
        capture_output=True,
        check=False,
    )
    if not result.stdout:
        return None
    return np.asarray(Image.open(io.BytesIO(result.stdout)).convert("RGB"))


def duration_of(source: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", source],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", default=DEFAULT_SOURCE)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--game", default="gta_v")
    args = parser.parse_args()

    source = args.source
    if not Path(source).is_file():
        print(f"FAILED: no such recording: {source}")
        return 1

    config = load_config()
    profiles_dir = Path(__file__).resolve().parents[1] / "profiles"
    resolution = load_profile(args.game, profiles_dir)
    if not resolution.exact:
        print(f"FAILED: no profile for {args.game!r}")
        return 1

    length = duration_of(source)
    if length <= 0:
        print("FAILED: could not read the recording's duration")
        return 1
    # Skip the first and last minute: recordings start and end on a desktop.
    step = (length - 120) / max(args.samples - 1, 1)
    times = [60 + step * index for index in range(args.samples)]

    print(f"source:  {Path(source).name}  ({length / 60:.1f} min)")
    print(f"profile: {resolution.profile.name}  ({resolution.profile.summary()})")
    print(f"samples: {len(times)} frames, every {step:.0f}s\n")

    readings: list[HudReading] = []
    generic_readings: list[HudReading] = []
    for seconds in times:
        frame = decode(source, seconds)
        if frame is None:
            continue
        readings.extend(read_frame(frame, resolution.profile, timestamp_seconds=seconds))
        generic_readings.extend(read_frame(frame, GENERIC_PROFILE, timestamp_seconds=seconds))

    usable = [item for item in readings if item.confidence >= 0.35]
    declined = [item for item in readings if item.confidence < 0.35]
    values = sorted({int(item.value) for item in usable if item.value is not None})

    print(f"readings:  {len(readings)}")
    print(f"  usable:  {len(usable)}  (values seen: {values})")
    print(f"  declined:{len(declined)}")
    for reason in sorted({str(item.detail.get('reason', 'low confidence')) for item in declined}):
        count = sum(
            1 for item in declined if item.detail.get("reason", "low confidence") == reason
        )
        print(f"           {count:3d}  {reason}")
    unsure = sum(1 for item in readings if item.quality is ReadingQuality.UNCERTAIN)
    print(f"  flagged uncertain: {unsure}")

    changes = track(readings)
    events = observations_from_hud(readings, resolution.profile)
    print(f"\nstate changes: {len(changes)}")
    for change in changes[:12]:
        minutes, secs = divmod(int(change.timestamp_seconds), 60)
        print(f"  {minutes:3d}:{secs:02d}  {change.previous} -> {change.current}"
              f"  (conf {change.confidence:.2f})")
    if len(changes) > 12:
        print(f"  ... and {len(changes) - 12} more")

    print(f"\nevents from the profile: {len(events)}")
    for event in events[:10]:
        minutes, secs = divmod(int(event.start_seconds), 60)
        print(
            f"  {minutes:3d}:{secs:02d}  {event.event_type.value:18s} "
            f"conf={event.confidence:.2f}"
        )

    generic_events = observations_from_hud(generic_readings, GENERIC_PROFILE)
    print(f"events from the generic profile: {len(generic_events)}")

    print()
    if not events:
        print("FAILED: the profile produced no events on this recording.")
        return 1
    if generic_events:
        print("FAILED: the generic profile is not supposed to read a HUD.")
        return 1
    print(f"PASSED: {len(events)} events the generic path cannot produce, "
          f"from {len(usable)} usable readings of {len(readings)}.")
    print(f"        Config duration band: {config.duration_policy.min_seconds}-"
          f"{config.duration_policy.max_seconds}s (unused here; loaded to prove config health).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
