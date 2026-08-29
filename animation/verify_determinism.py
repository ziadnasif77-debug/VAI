"""P0 gate (approved amendment #2): deterministic FRAMES, not bytes of MP4.

Renders the same Scene Document twice and compares:
  * frames: >= 99.5% byte-identical PNGs, every differing frame PSNR >= 48 dB
    (GPU raster allowance);
  * audio: sample-identical (it is authored, not captured);
  * MP4 byte-identity: explicitly out of contract.

    python animation/verify_determinism.py animation/scenes/p0_hello_walk.json
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = Path(
    os.environ.get("FFMPEG_EXE", ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
)

IDENTICAL_FLOOR = 0.995
PSNR_FLOOR_DB = 48.0


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def psnr(a: Path, b: Path) -> float:
    result = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-i", str(a), "-i", str(b),
         "-lavfi", "psnr", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"average:(inf|[\d.]+)", result.stderr)
    if match is None:
        return 0.0
    return float("inf") if match.group(1) == "inf" else float(match.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    arguments = parser.parse_args()

    out = ROOT / "animation" / "out"
    runs = []
    for name in ("run_a", "run_b"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "animation" / "compile.py"),
             str(arguments.scene), "--out", str(out / name)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(result.stderr[-2000:], file=sys.stderr)
            print(f"VERDICT: FAIL (render {name} died)")
            return 1
        runs.append(out / name)

    frames_a = sorted((runs[0] / "frames").glob("f*.png"))
    frames_b = sorted((runs[1] / "frames").glob("f*.png"))
    if len(frames_a) != len(frames_b) or not frames_a:
        print(f"VERDICT: FAIL (frame counts differ: {len(frames_a)} vs {len(frames_b)})")
        return 1

    identical = 0
    worst_psnr = float("inf")
    differing: list[str] = []
    for a, b in zip(frames_a, frames_b, strict=True):
        if sha(a) == sha(b):
            identical += 1
            continue
        value = psnr(a, b)
        worst_psnr = min(worst_psnr, value)
        differing.append(f"{a.name}: psnr={value:.1f}dB")

    share = identical / len(frames_a)
    wav_a = runs[0] / "frames" / "f.wav"
    wav_b = runs[1] / "frames" / "f.wav"
    audio_ok = wav_a.is_file() and wav_b.is_file() and sha(wav_a) == sha(wav_b)

    print(f"frames: {len(frames_a)}  identical: {identical} ({share:.2%})")
    if differing:
        print("differing (up to 10 shown):")
        for line in differing[:10]:
            print("  " + line)
        print(f"worst PSNR: {worst_psnr:.1f} dB (floor {PSNR_FLOOR_DB})")
    print(f"audio identical: {audio_ok}")

    ok = share >= IDENTICAL_FLOOR and (
        not differing or worst_psnr >= PSNR_FLOOR_DB
    ) and audio_ok
    print(f"VERDICT: {'PASS' if ok else 'FAIL'} "
          f"(gate: >={IDENTICAL_FLOOR:.1%} identical, diffs >= {PSNR_FLOOR_DB} dB, audio exact)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
