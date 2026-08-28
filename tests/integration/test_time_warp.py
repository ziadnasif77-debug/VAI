"""Freeze and ramp, run through FFmpeg and measured (doctrine §11, §12).

The unit tests own the graph strings and the re-lay arithmetic. What only a
real decode can settle is whether the constructions *keep the timeline's
promise*: a 6 s source clip carrying a 1.5 s freeze must come back from
ffprobe as 7.5 s -- picture and sound both -- with the held frame actually
held and the hold actually silent. Every number below is a measurement of a
file, because the whole point of the re-lay is that the §76 duration gate can
keep comparing files against the timeline without learning anything new.

CPU only: the encoder is pinned to libx264 by handing ``render_programme`` an
explicit :class:`EncoderChoice`, so no NVENC probe ever runs. No models, no
Chromium.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from backend.media.ffmpeg import FFmpegRunner
from backend.rendering import audio_mix
from backend.rendering.encoder import EncoderChoice, EncodeTarget
from backend.rendering.ffmpeg_renderer import render_programme
from backend.timeline import retime
from backend.timeline.models import TimelineClip

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]

MEDIA = "media-aaaaaaaaaaaa"

X264 = EncoderChoice(name="libx264", hardware=False, reason="pinned for the test")
TARGET = EncodeTarget(width=320, height=240, fps=30)


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


def _frozen_clip(hold: float = 1.5, at: float = 2.5) -> TimelineClip:
    return TimelineClip(
        id="clip-0000itfroze",
        media_id=MEDIA,
        clip_index=0,
        source_in=0.0,
        source_out=6.0,
        timeline_start=0.0,
        timeline_end=6.0 + hold,
        metadata={"retime": {"effect": "freeze_frame", "at": at, "extra_seconds": hold}},
    )


def _ramped_clip(window: float, factor: float, at: float = 2.0) -> TimelineClip:
    extra = window * (1.0 / factor - 1.0)
    return TimelineClip(
        id="clip-00000itramp",
        media_id=MEDIA,
        clip_index=0,
        source_in=0.0,
        source_out=6.0,
        timeline_start=0.0,
        timeline_end=6.0 + extra,
        metadata={
            "retime": {
                "effect": "speed_ramp",
                "at": at,
                "extra_seconds": extra,
                "window_seconds": window,
                "factor": factor,
            }
        },
    )


def _frame_at(runner: FFmpegRunner, path: Path, second: float) -> np.ndarray:
    """One decoded frame as flat RGB integers, for pixel arithmetic."""
    completed = subprocess.run(
        [
            *runner.base_arguments(),
            "-ss", f"{second}",
            "-i", str(path),
            "-frames:v", "1",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return np.frombuffer(completed.stdout, dtype=np.uint8).astype(int)


def _mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
        channels = handle.getnchannels()
    return raw.reshape(-1, channels).mean(axis=1) / 32768.0, rate


def _rms(samples: np.ndarray, rate: int, start: float, end: float) -> float:
    window = samples[int(start * rate) : int(end * rate)]
    return float(np.sqrt(np.mean(window**2)))


class TestFrozenPicture:
    """A 6 s source with a 1.5 s freeze yields 7.5 s, holding the frame."""

    @pytest.fixture
    def programme(self, runner, config, test_clip: Path, tmp_path: Path):
        result = render_programme(
            [_frozen_clip()],
            {MEDIA: test_clip},
            destination=tmp_path / "programme.mp4",
            work_dir=tmp_path,
            runner=runner,
            config=config,
            encoder=X264,
            target=TARGET,
        )
        return result

    def test_the_file_is_the_re_laid_duration(self, programme) -> None:
        # 6 s of source + 1.5 s hold, measured off the file, not restated.
        assert programme.duration_seconds == pytest.approx(7.5, abs=0.05)
        assert any("baked" in note for note in programme.notes)

    def test_the_anchor_frame_is_actually_held(self, programme, runner) -> None:
        # Inside the hold [2.5 s, 4.0 s] the picture must not move; after it,
        # it must. Compared by mean pixel difference rather than equality,
        # because two encodes of the same frame differ by codec noise --
        # measured 0.02 across the hold against 6.5 across real motion.
        inside_a = _frame_at(runner, programme.path, 2.7)
        inside_b = _frame_at(runner, programme.path, 3.7)
        after = _frame_at(runner, programme.path, 4.5)

        assert np.abs(inside_a - inside_b).mean() < 0.5, "the hold moved"
        assert np.abs(inside_a - after).mean() > 2.0, "the picture never resumed"

    def test_a_rerender_reuses_the_warped_segment(
        self, programme, runner, config, test_clip: Path, tmp_path: Path
    ) -> None:
        # §47: the segment's name carries the warp, and its duration check
        # compares against the re-laid clip duration -- so the second render
        # must reuse rather than re-cut.
        again = render_programme(
            [_frozen_clip()],
            {MEDIA: test_clip},
            destination=tmp_path / "programme2.mp4",
            work_dir=tmp_path,
            runner=runner,
            config=config,
            encoder=X264,
            target=TARGET,
        )

        assert again.reused_segments == 1
        assert again.duration_seconds == pytest.approx(7.5, abs=0.05)


class TestRampedPicture:
    def test_the_file_is_the_sum_of_the_phases(
        self, runner, config, test_clip: Path, tmp_path: Path
    ) -> None:
        # 2 s real + 1 s of source at 0.5 (= 2 s) + 3 s real = 7 s.
        result = render_programme(
            [_ramped_clip(window=1.0, factor=0.5)],
            {MEDIA: test_clip},
            destination=tmp_path / "programme.mp4",
            work_dir=tmp_path,
            runner=runner,
            config=config,
            encoder=X264,
            target=TARGET,
        )

        assert result.duration_seconds == pytest.approx(7.0, abs=0.05)

    def test_a_warped_clip_concatenates_with_a_plain_one(
        self, runner, config, test_clip: Path, tmp_path: Path
    ) -> None:
        # The rest of the pipeline sees one segment of the re-laid length, so
        # the join arithmetic must come out at the timeline's total.
        plain = TimelineClip(
            id="clip-0000itplain",
            media_id=MEDIA,
            clip_index=1,
            source_in=1.0,
            source_out=5.0,
            timeline_start=7.5,
            timeline_end=11.5,
        )
        result = render_programme(
            [_frozen_clip(), plain],
            {MEDIA: test_clip},
            destination=tmp_path / "programme.mp4",
            work_dir=tmp_path,
            runner=runner,
            config=config,
            encoder=X264,
            target=TARGET,
        )

        assert result.clips == 2
        assert result.duration_seconds == pytest.approx(11.5, abs=0.08)


class TestWarpedAudio:
    """The audio graph, run for real: same lengths, silence where promised."""

    def _rendered_wav(
        self, runner: FFmpegRunner, source: Path, clip: TimelineClip, tmp_path: Path
    ) -> Path:
        warp = retime.clip_retime(clip)
        assert warp is not None
        graph = audio_mix.warped_clip_audio(
            0, clip, warp, out_label="a0", fade_in=0.03, fade_out=0.03
        )
        destination = tmp_path / "clip_audio.wav"
        runner.run(
            [
                *runner.base_arguments(),
                "-i", str(source),
                "-filter_complex", graph,
                "-map", "[a0]",
                "-c:a", "pcm_s16le",
                str(destination),
            ]
        )
        return destination

    def test_freeze_audio_is_the_re_laid_length_with_silence_under_the_hold(
        self, runner, config, test_clip: Path, tmp_path: Path
    ) -> None:
        path = self._rendered_wav(runner, test_clip, _frozen_clip(), tmp_path)
        samples, rate = _mono(path)

        assert len(samples) / rate == pytest.approx(7.5, abs=0.05)
        # The fixture clip is a steady sine: audible either side of the hold,
        # written silence inside it -- a freeze with looping audio is worse
        # than quiet, and stingers are a later layer.
        assert _rms(samples, rate, 1.0, 2.3) > 0.02
        assert _rms(samples, rate, 2.7, 3.8) < 0.001
        assert _rms(samples, rate, 4.3, 6.5) > 0.02

    def test_ramp_audio_with_a_sub_half_factor_is_the_re_laid_length(
        self, runner, config, test_clip: Path, tmp_path: Path
    ) -> None:
        # factor 0.4 forces the chained atempo (0.5 x 0.8); 0.8 s of source
        # plays for 2.0 s and the whole clip for 7.2 s.
        path = self._rendered_wav(
            runner, test_clip, _ramped_clip(window=0.8, factor=0.4), tmp_path
        )
        samples, rate = _mono(path)

        assert len(samples) / rate == pytest.approx(7.2, abs=0.05)
        # The stretch is sound, not padding: the slow window still carries
        # signal well above silence.
        assert _rms(samples, rate, 2.3, 3.7) > 0.02
