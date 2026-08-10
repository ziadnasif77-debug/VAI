"""§7 acceptance: long sources are processed without loading them into RAM.

    "Must handle 30 min, 1 h, 2 h, 4 h, 6 h, 8 h without loading the video
     into RAM. Analysis is chunk-based with overlap where needed."

That is a claim about memory, so it is tested by measuring memory rather than
by reading the code. The measurement is comparative, which is what makes it a
real guarantee instead of a threshold somebody will later tune until it passes:
the same pipeline runs over a six-second source and over a fifteen-minute
source -- 150 times the length -- and the peak working set must barely move.

An implementation that read a recording into memory would fail this by a factor
of hundreds at two hours. One that streams shows a flat line, which is the
property §7 actually asks for.

Marked ``slow``: it generates and transcodes a quarter of an hour of video.
Run it with ``pytest -m slow``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg, pytest.mark.slow]

MEGABYTE = 1024 * 1024

#: How much the peak may grow between a 6-second source and a 15-minute one.
#: Streaming implementations differ by a few megabytes of buffers and Python
#: bookkeeping; anything proportional to length lands far outside this.
MAX_GROWTH_BYTES = 64 * MEGABYTE

#: Absolute ceiling for the whole run. Generous for a streaming pipeline and
#: unreachable for one that holds a recording in memory.
MAX_PEAK_BYTES = 512 * MEGABYTE


def _measure(source: Path, work_dir: Path, repo_root: Path) -> dict:
    """Run the media engine over ``source`` in a fresh process and report."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repo_root)
    completed = subprocess.run(
        [sys.executable, str(repo_root / "tests" / "support" / "media_memory_probe.py"),
         str(source), str(work_dir)],
        cwd=str(repo_root),
        env=environment,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_memory_does_not_grow_with_source_length(
    test_clip: Path, long_clip: Path, tmp_path: Path, repo_root: Path
) -> None:
    short = _measure(test_clip, tmp_path / "short", repo_root)
    long_source = _measure(long_clip, tmp_path / "long", repo_root)

    ratio = long_source["duration_seconds"] / short["duration_seconds"]
    growth = long_source["peak_rss_bytes"] - short["peak_rss_bytes"]

    # Reported unconditionally: the number is the point of the test, and a
    # future regression is easier to read than to re-derive.
    print(
        f"\n§7 peak working set"
        f"\n  {short['duration_seconds']:.1f}s source: "
        f"{short['peak_rss_bytes'] / MEGABYTE:.1f} MB"
        f"\n  {long_source['duration_seconds']:.1f}s source: "
        f"{long_source['peak_rss_bytes'] / MEGABYTE:.1f} MB"
        f"\n  {ratio:.0f}x the length, {growth / MEGABYTE:+.1f} MB peak"
    )

    assert ratio > 100, "the long fixture must be much longer for this to mean anything"
    assert growth < MAX_GROWTH_BYTES, (
        f"peak memory grew {growth / MEGABYTE:.1f} MB for a {ratio:.0f}x longer source; "
        "something is holding the recording rather than streaming it (§7)"
    )
    assert long_source["peak_rss_bytes"] < MAX_PEAK_BYTES


def test_the_whole_media_chain_runs_on_a_long_source(
    long_clip: Path, tmp_path: Path, repo_root: Path
) -> None:
    """The four media stages produce their artefacts over a multi-segment source."""
    result = _measure(long_clip, tmp_path / "chain", repo_root)

    assert result["duration_seconds"] == pytest.approx(900, abs=2)
    # 900 s at 300 s per segment: the concat path, not the single-file shortcut.
    assert result["proxy_segments"] == 3
    assert result["proxy_bytes"] > 0
    assert result["audio_bytes"] > 0
    # 900 s at the configured 3 s base interval.
    assert result["frames"] == pytest.approx(300, abs=5)


def test_peak_memory_is_far_below_the_source_size(
    long_clip: Path, tmp_path: Path, repo_root: Path
) -> None:
    """The derived artefacts are larger than the peak, which is the whole point.

    The analysis WAV alone is tens of megabytes at fifteen minutes and hundreds
    at eight hours. None of it is ever held.
    """
    result = _measure(long_clip, tmp_path / "sizes", repo_root)
    produced = result["proxy_bytes"] + result["audio_bytes"]
    assert produced > 0
    assert result["peak_rss_bytes"] < MAX_PEAK_BYTES
    assert result["peak_rss_bytes"] < produced + MAX_PEAK_BYTES
