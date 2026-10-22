"""The mix's ceiling is a ceiling (§72, §74).

FFmpeg's ``alimiter`` has an ``level`` option, on by default, that scales the
limited signal back up by ``1 / limit`` -- "auto level", in its own words. With
it on, ``limit`` stops being a ceiling: a -6 dB limit on a loud burst comes out
peaking at 0 dBFS, and the shipped -0.3 dB limit after ``loudnorm`` lifts the
whole programme by 0.3 dB, past the -1 dB true peak the configuration asks for.
Measured on FFmpeg 6.1.1 before the mix turned it off: peak 0.00 dB against a
-6 dB ceiling, and -0.70 dB against the -1 dB target.

These run the planned graph through a real FFmpeg on a synthetic signal and read
the peak back, because the defect is in what the filter does, not in the string
the planner writes.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from backend.rendering import audio_mix

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]

#: astats reports to six decimals; a limiter's lookahead lets a sample or two
#: through by a hair. Anything past this is the auto level, not rounding.
TOLERANCE_DB = 0.1


def _signal(path: Path) -> Path:
    """Six seconds of pink noise with a burst four times louder in the middle."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anoisesrc=d=6:c=pink:a=0.9:seed=7",
            "-af", "volume='if(between(t,2,3),1,0.25)':eval=frame",
            "-ar", "48000", "-ac", "2", str(path),
        ],
        check=True,
    )
    return path


def _peak_db(plan: audio_mix.MixPlan) -> float:
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats",
            *plan.input_arguments(),
            "-filter_complex",
            f"{plan.filter_complex};[{plan.output_label}]"
            "astats=measure_overall=Peak_level:measure_perchannel=none[measured]",
            "-map", "[measured]", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    found = re.findall(r"Peak level dB:\s*(-?[\d.]+|-inf)", completed.stderr)
    assert found, completed.stderr[-2000:]
    return float(found[-1])


def _plan(config, game: Path) -> audio_mix.MixPlan:
    return audio_mix.plan_mix(
        game=game,
        microphone=None,
        music=[],
        music_envelope=None,
        game_envelope=None,
        config=config,
        duration_seconds=6.0,
    )


def test_the_limiter_holds_its_own_ceiling(config, tmp_path: Path) -> None:
    audio = config.audio.model_copy(deep=True)
    audio.mix.normalization.enabled = False
    audio.mix.limiter.ceiling_db = -6.0
    audio.fades.program_fade_in_ms = 0
    audio.fades.program_fade_out_ms = 0

    peak = _peak_db(_plan(audio, _signal(tmp_path / "game.wav")))

    assert peak <= -6.0 + TOLERANCE_DB


def test_the_shipped_chain_stays_under_the_true_peak_target(config, tmp_path: Path) -> None:
    audio = config.audio
    assert audio.mix.normalization.enabled and audio.mix.limiter.enabled

    peak = _peak_db(_plan(audio, _signal(tmp_path / "game.wav")))

    # loudnorm's true-peak target is the stricter of the two ceilings in the
    # shipped chain; the limiter after it must not lift the programme past it.
    assert peak <= audio.mix.normalization.true_peak_db + TOLERANCE_DB
