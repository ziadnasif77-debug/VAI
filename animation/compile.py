"""P0 Episode Compiler — the Python side of the layering contract.

Owns everything Godot must not: validating the Scene Document, synthesising
the P0 stand-in audio (no TTS yet — beeps with the right timing), invoking
the renderer as a black box, and mastering frames+wav into an MP4 with the
machine's NVENC. Zero imports from ``backend/`` on purpose: P0 proves the
renderer, not the platform (docs/PLATFORM_AUDIT.md §3).

    python animation/compile.py animation/scenes/p0_hello_walk.json \
        --out animation/out/run_a
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
GODOT = Path(os.environ.get("GODOT_EXE", ROOT / "tools" / "godot" / "godot.exe"))
FFMPEG = Path(
    os.environ.get("FFMPEG_EXE", ROOT / "tools" / "ffmpeg" / "ffmpeg.exe")
)

REQUIRED = ("id", "duration_s", "fps", "layers", "characters", "timeline")


def fail(message: str) -> NoReturn:
    print(f"P0 ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def validate(doc: dict) -> None:
    for key in REQUIRED:
        if key not in doc:
            fail(f"scene document missing required key: {key}")
    if doc["fps"] not in (24, 30):
        fail("fps must be 24 or 30")
    if not (0 < float(doc["duration_s"]) <= 120):
        fail("duration_s out of range")
    for ev in doc["timeline"]:
        if "t" not in ev or "track" not in ev:
            fail(f"timeline event missing t/track: {ev}")


def ensure_p0_audio(assets: Path, doc: dict) -> None:
    """Stand-in sounds with honest timing; real TTS is P1's business."""
    recipes = {
        "sfx/footsteps.wav": (
            "sine=frequency=95:duration=3.4",
            "volume='0.5*abs(sin(2*PI*t*3.5))':eval=frame,alimiter=limit=0.6",
        ),
        "sfx/sad_chirp.wav": (
            "sine=frequency=520:duration=0.8",
            "vibrato=f=6:d=0.6,afade=t=out:st=0.3:d=0.5",
        ),
        "audio/line1.wav": (
            "sine=frequency=300:duration=2.0",
            "vibrato=f=4:d=0.9,volume=0.7,afade=t=out:st=1.7:d=0.3",
        ),
    }
    for rel, (source, filters) in recipes.items():
        destination = assets / rel
        if destination.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        run(
            [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", source, "-af", filters,
             "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(destination)],
            "synthesise " + rel,
        )
    visemes = assets / "audio" / "line1.visemes.json"
    if not visemes.is_file():
        visemes.write_text(json.dumps({
            "audio": "audio/line1.wav", "fps": doc["fps"],
            "frames": [
                {"f": 0, "v": "A", "open": 0.9}, {"f": 3, "v": "I", "open": 0.7},
                {"f": 6, "v": "LNT", "open": 0.5}, {"f": 10, "v": "A", "open": 0.95},
                {"f": 14, "v": "MBP", "open": 0.0}, {"f": 17, "v": "I", "open": 0.75},
                {"f": 24, "v": "A", "open": 0.85}, {"f": 30, "v": "U", "open": 0.6},
                {"f": 38, "v": "REST", "open": 0.1},
            ]}), encoding="utf-8")


def run(argv: list[str], what: str) -> None:
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"{what} failed (rc={result.returncode}):\n{result.stderr[-1500:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--assets", type=Path, default=ROOT / "animation" / "assets")
    arguments = parser.parse_args()

    if not GODOT.is_file():
        fail(f"Godot runtime not found at {GODOT} (set GODOT_EXE or vendor it "
             "under tools/godot/)")
    if not FFMPEG.is_file():
        fail(f"ffmpeg not found at {FFMPEG}")

    doc = json.loads(arguments.scene.read_text(encoding="utf-8"))
    validate(doc)

    out = arguments.out
    frames_dir = out / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    assets = arguments.assets.resolve()
    ensure_p0_audio(assets, doc)

    movie = frames_dir / "f.png"
    # Not --headless: Movie Maker needs a real rendering context, and the
    # dummy driver writes zero frames silently. A window appears for the
    # seconds of the render; on this desktop machine that is the honest
    # trade until an offscreen context proves itself.
    run(
        [str(GODOT),
         "--path", str(ROOT / "animation" / "godot"),
         "--write-movie", str(movie),
         "--fixed-fps", str(doc["fps"]),
         "--", "--scene", str(arguments.scene.resolve()),
         "--assets", str(assets)],
        "godot render",
    )

    produced = sorted(frames_dir.glob("f*.png"))
    expected = round(float(doc["duration_s"]) * doc["fps"])
    if len(produced) < expected - 1:
        fail(f"renderer produced {len(produced)} frames, expected ~{expected}")
    wav = movie.with_suffix(".wav")

    final = out / f"{doc['id']}.mp4"
    encode = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
              "-framerate", str(doc["fps"]),
              "-i", str(frames_dir / "f%08d.png")]
    if wav.is_file():
        encode += ["-i", str(wav), "-c:a", "aac", "-b:a", "192k", "-shortest"]
    encode += ["-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "8M",
               "-pix_fmt", "yuv420p", str(final)]
    result = subprocess.run(encode, capture_output=True, text=True)
    if result.returncode != 0:
        # NVENC can be busy or absent in odd sessions; software x264 is the
        # honest fallback, stated out loud.
        print("NVENC unavailable, falling back to libx264", file=sys.stderr)
        encode[encode.index("h264_nvenc")] = "libx264"
        run(encode, "ffmpeg encode")

    print(json.dumps({
        "scene": doc["id"],
        "frames": len(produced),
        "audio": wav.is_file(),
        "output": str(final),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
