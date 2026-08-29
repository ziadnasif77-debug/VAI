"""Arabic text → viseme timeline (the architecture's §5, first real cut).

Deterministic by construction: the letters of the line, classed into nine
mouth shapes, spread over the measured duration of the spoken audio with
long vowels weighted double, then smoothed -- a minimum hold per viseme and
merged repeats (co-articulation). No model at runtime; forced alignment can
replace the uniform spread in P1 without changing the output contract.

    python animation/lipsync.py "أين أمي؟" --audio line1.wav --fps 24 \
        --out line1.visemes.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

#: Arabic letters → viseme class. Nine shapes, per the approved table.
_CLASSES: dict[str, str] = {}
for letters, viseme in [
    ("اأإآى", "A"),
    ("ي", "I"),
    ("وؤ", "U"),
    ("مب", "MBP"),
    ("ف", "FV"),
    ("لنتدطضظ", "LNT"),
    ("سصزشثذ", "SD"),
    ("كقغخعحهجرء", "KG"),
]:
    for letter in letters:
        _CLASSES[letter] = viseme

#: Diacritics carry the vowel truth when present.
_HARAKAT = {"َ": "A", "ُ": "U", "ِ": "I", "ً": "A",
            "ٌ": "U", "ٍ": "I"}
_SUKUN = "ْ"
_SHADDA = "ّ"

#: How open each viseme plays, 0..1.
_OPEN = {"A": 0.95, "I": 0.6, "U": 0.55, "MBP": 0.0, "FV": 0.25,
         "LNT": 0.45, "SD": 0.3, "KG": 0.7, "REST": 0.1}

#: Long vowels and shadda take twice the time of a plain consonant.
_LONG = set("اأإآىوي")


def text_to_units(text: str) -> list[tuple[str, float]]:
    """(viseme, weight) per sounding unit, in order."""
    units: list[tuple[str, float]] = []
    for ch in text:
        if ch in _HARAKAT:
            units.append((_HARAKAT[ch], 1.0))
            continue
        if ch == _SHADDA and units:
            units.append((units[-1][0], 1.0))
            continue
        if ch == _SUKUN:
            continue
        viseme = _CLASSES.get(ch)
        if viseme is None:
            if ch.isspace() or ch in "؟!.,،؛:":
                units.append(("REST", 0.7))
            continue
        units.append((viseme, 2.0 if ch in _LONG else 1.0))
    # collapse runs of REST
    out: list[tuple[str, float]] = []
    for viseme, weight in units:
        if out and viseme == "REST" and out[-1][0] == "REST":
            continue
        out.append((viseme, weight))
    return out


def units_to_frames(
    units: list[tuple[str, float]], duration_s: float, fps: int,
    min_hold: int = 2,
) -> list[dict]:
    total_frames = max(1, round(duration_s * fps))
    total_weight = sum(w for _, w in units) or 1.0
    frames: list[dict] = []
    cursor = 0.0
    last = None
    for viseme, weight in units:
        f = round(cursor)
        cursor += weight / total_weight * total_frames
        if last is not None and viseme == last["v"]:
            continue  # co-articulation: merged repeats
        if last is not None and f - last["f"] < min_hold:
            continue  # too quick to read; the previous shape holds
        entry = {"f": int(f), "v": viseme, "open": _OPEN[viseme]}
        frames.append(entry)
        last = entry
    frames.append({"f": total_frames, "v": "REST", "open": 0.1})
    return frames


def wav_duration(path: Path, ffprobe: Path) -> float:
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--ffprobe", type=Path,
        default=Path(__file__).resolve().parent.parent / "tools" / "ffmpeg" / "ffprobe.exe",
    )
    arguments = parser.parse_args()

    duration = wav_duration(arguments.audio, arguments.ffprobe)
    frames = units_to_frames(text_to_units(arguments.text), duration, arguments.fps)
    arguments.out.write_text(
        json.dumps(
            {"audio": arguments.audio.name, "fps": arguments.fps,
             "duration_s": round(duration, 3), "text": arguments.text,
             "frames": frames},
            ensure_ascii=False, indent=1,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"duration_s": round(duration, 3), "visemes": len(frames)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
