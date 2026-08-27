"""The J-cut, run through FFmpeg and measured (backend/rendering/jl.py).

The planner and the graph builder are unit-tested as data; this is the one
test that pays for a real FFmpeg run, because the claim it checks cannot be
checked as a string: that the assembled track is the length of the timeline
and that the incoming clip's sound is *actually there* before the picture
cut. Two tones make the measurement unambiguous — the outgoing clip hums at
440 Hz, the incoming at 1760 Hz, so "the lead exists" is a frequency the
spectrum either contains or does not.

Audio only, CPU only: no encoder, no NVENC, no Chromium.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from backend.core.models.enums import TrackKind
from backend.media.ffmpeg import FFmpegRunner
from backend.rendering.jl import assembly_arguments, plan_boundaries
from backend.timeline.models import Timeline, TimelineClip, Track

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]

MEDIA_A = "media-aaaaaaaaaaaa"
MEDIA_B = "media-bbbbbbbbbbbb"

OUTGOING_HZ = 440
INCOMING_HZ = 1760


@pytest.fixture
def runner(config, repo_root: Path) -> FFmpegRunner:
    """The repo's own FFmpeg when the checkout bundles one, PATH otherwise."""
    bundled = repo_root / "tools" / "ffmpeg" / "ffmpeg.exe"
    section = config.ffmpeg
    if bundled.is_file():
        section = section.model_copy(
            update={
                "binary": str(bundled),
                "ffprobe_binary": str(bundled.with_name("ffprobe.exe")),
            }
        )
    return FFmpegRunner(section)


def _tone(runner: FFmpegRunner, path: Path, hertz: int) -> Path:
    runner.run(
        [
            *runner.base_arguments(),
            "-f", "lavfi",
            "-i", f"sine=frequency={hertz}:sample_rate=48000",
            "-t", "4",
            "-c:a", "pcm_s16le",
            str(path),
        ]
    )
    return path


def _mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
        channels = handle.getnchannels()
    return raw.reshape(-1, channels).mean(axis=1) / 32768.0, rate


def _band_energy(window: np.ndarray, rate: int, hertz: float) -> float:
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    frequencies = np.fft.rfftfreq(len(window), 1.0 / rate)
    band = (frequencies > hertz - 30) & (frequencies < hertz + 30)
    return float(spectrum[band].sum())


def test_a_j_boundary_plays_the_incoming_sound_before_the_picture_cut(
    runner: FFmpegRunner, tmp_path: Path
) -> None:
    sources = {
        MEDIA_A: _tone(runner, tmp_path / "a.wav", OUTGOING_HZ),
        MEDIA_B: _tone(runner, tmp_path / "b.wav", INCOMING_HZ),
    }
    timeline = Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
        Track(
            kind=TrackKind.VIDEO,
            clips=(
                TimelineClip(
                    id="clip-000000000000",
                    media_id=MEDIA_A,
                    clip_index=0,
                    source_in=1.0,
                    source_out=3.0,
                    timeline_start=0.0,
                    timeline_end=2.0,
                ),
                TimelineClip(
                    id="clip-000000000001",
                    media_id=MEDIA_B,
                    clip_index=1,
                    source_in=1.0,
                    source_out=3.0,
                    timeline_start=2.0,
                    timeline_end=4.0,
                ),
            ),
        )
    )

    from backend.config.schema import JLCutsConfig

    config = JLCutsConfig(enabled=True)
    plans = plan_boundaries(timeline, {MEDIA_B: [(1.1, 2.5)]}, config)
    assert [plan.kind for plan in plans] == ["j"], "the fixture must earn a J"
    assert plans[0].dt == pytest.approx(0.6)

    destination = tmp_path / "programme_audio.wav"
    argv = assembly_arguments(
        timeline.video_clips(),
        plans,
        sources=sources,
        destination=destination,
        config=config,
    )
    runner.run([*runner.base_arguments(), *argv])

    samples, rate = _mono(destination)

    # The track is the timeline's length: the lead moved sound across the
    # boundary, it did not add a frame anywhere.
    assert len(samples) / rate == pytest.approx(4.0, abs=0.05)

    # The boundary itself is not a hole.
    cut = samples[int(1.9 * rate) : int(2.1 * rate)]
    assert float(np.sqrt(np.mean(cut**2))) > 0.02, "silence at the boundary"

    # The J-cut, as a spectrum: inside the 0.6 s lead (past its 0.12 s fade),
    # the incoming clip's 1760 Hz is already sounding under the outgoing
    # 440 Hz picture...
    lead = samples[int(1.55 * rate) : int(1.95 * rate)]
    assert _band_energy(lead, rate, INCOMING_HZ) > 10 * _band_energy(
        lead, rate, INCOMING_HZ + 700
    )
    assert _band_energy(lead, rate, OUTGOING_HZ) > 10 * _band_energy(
        lead, rate, OUTGOING_HZ + 700
    )

    # ...and before the lead begins there is no trace of it: the incoming
    # audio starts at t_cut - dt, not at the top of the video.
    before = samples[int(0.3 * rate) : int(1.3 * rate)]
    assert _band_energy(before, rate, INCOMING_HZ) < 0.1 * _band_energy(
        lead, rate, INCOMING_HZ
    )
