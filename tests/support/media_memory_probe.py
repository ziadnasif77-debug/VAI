"""Run the media engine over one source and report peak memory.

Executed as a subprocess by the §7 acceptance test. A separate process is what
makes the number meaningful: peak working set is monotonic for the life of a
process, so measuring inside a pytest session would report the high-water mark
of every test that ran before.

    python tests/support/media_memory_probe.py <source> <work-dir>

Prints one JSON object on stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The repository root goes to the front of the path explicitly. Run as a script,
# sys.path[0] is this directory, and an unrelated `tests` package installed in
# site-packages would otherwise shadow this one.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config.loader import load_config
from backend.media.audio import extract_all_tracks
from backend.media.ffmpeg import FFmpegRunner
from backend.media.frames import extract_uniform, plan_base_sampling
from backend.media.probe import probe_media
from backend.media.proxy import generate_proxy
from tests.support.memory import peak_rss_bytes


def main(argv: list[str]) -> int:
    source = Path(argv[1])
    work_dir = Path(argv[2])
    work_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    runner = FFmpegRunner(config.ffmpeg)

    baseline = peak_rss_bytes()

    probe = probe_media(source, runner, require_video=True)

    proxy = generate_proxy(
        source,
        work_dir / "proxy.mp4",
        runner,
        config.proxy,
        probe=probe,
        segment_seconds=300.0,
    )

    streams = extract_all_tracks(
        source, work_dir / "audio", runner, config.audio.analysis, probe=probe
    )

    sampling = config.analysis.frame_sampling
    plan = plan_base_sampling(probe.duration_seconds, sampling)
    frames = extract_uniform(
        proxy.path,
        work_dir / "frames",
        plan,
        runner,
        jpeg_quality=sampling.jpeg_quality,
        duration_seconds=probe.duration_seconds,
    )

    print(
        json.dumps(
            {
                "source_bytes": source.stat().st_size,
                "duration_seconds": probe.duration_seconds,
                "baseline_rss_bytes": baseline,
                "peak_rss_bytes": peak_rss_bytes(),
                "proxy_bytes": proxy.size_bytes,
                "proxy_segments": proxy.segment_count,
                "audio_bytes": sum(stream.size_bytes for stream in streams),
                "frames": len(frames),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
