"""BT.601 recordings are cut as BT.709, untagged ones are left alone (BL-018).

The caption overlay is encoded BT.709 (BL-014). A recording tagged BT.601
that is cut without conversion keeps its 601 tags and 601 pixel values, so
anything that reads it as BT.709 -- the overlay composite, most players --
shifts a saturated yellow #FFD400 to 255,201,0. Both cases are measured on
real files: the tag ffprobe reports, and the colour a BT.709 reading gives.

CPU only, libx264 pinned, no models.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from backend.media.ffmpeg import FFmpegRunner
from backend.rendering.composite import composite
from backend.rendering.encoder import EncoderChoice, EncodeTarget
from backend.rendering.ffmpeg_renderer import render_programme
from backend.timeline.models import TimelineClip

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]

MEDIA = "media-aaaaaaaaaaaa"
X264 = EncoderChoice(name="libx264", hardware=False, reason="pinned for the test")
TARGET = EncodeTarget(width=320, height=240, fps=30)
YELLOW = (255, 212, 0)


@pytest.fixture
def runner(config) -> FFmpegRunner:
    return FFmpegRunner(config.ffmpeg)


def _yellow(runner: FFmpegRunner, path: Path, *, bt601: bool) -> Path:
    """Two seconds of #FFD400, lossless, tagged BT.601 or not tagged at all."""
    matrix = (
        ["-vf", "scale=out_color_matrix=bt601,format=yuv420p",
         "-colorspace", "bt470bg", "-color_primaries", "bt470bg", "-color_trc", "smpte170m"]
        if bt601
        else ["-vf", "format=yuv420p"]
    )
    subprocess.run(
        [
            *runner.base_arguments(),
            "-f", "lavfi", "-i", "color=c=0xFFD400:s=320x240:r=30:d=2",
            *matrix,
            "-c:v", "libx264", "-qp", "0",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _render(runner, config, source: Path, tmp_path: Path) -> Path:
    clip = TimelineClip(
        id="clip-0000itcolor",
        media_id=MEDIA,
        clip_index=0,
        source_in=0.0,
        source_out=1.5,
        timeline_start=0.0,
        timeline_end=1.5,
    )
    return render_programme(
        [clip],
        {MEDIA: source},
        destination=tmp_path / "programme.mp4",
        work_dir=tmp_path,
        runner=runner,
        config=config,
        encoder=X264,
        target=TARGET,
    ).path


def _matrix_tag(runner: FFmpegRunner, path: Path) -> str | None:
    completed = subprocess.run(
        [
            runner.ffprobe_path, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=color_space", "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)["streams"][0].get("color_space")


def _centre(runner: FFmpegRunner, path: Path, *, read_as: str | None) -> np.ndarray:
    """The centre pixel as RGB, decoded with ``read_as`` or with the file's own tag."""
    scale = f"scale=in_color_matrix={read_as}," if read_as else ""
    completed = subprocess.run(
        [
            *runner.base_arguments(),
            "-ss", "0.5", "-i", str(path), "-frames:v", "1",
            "-vf", f"{scale}format=rgb24", "-f", "rawvideo", "-",
        ],
        check=True,
        capture_output=True,
    )
    frame = np.frombuffer(completed.stdout, dtype=np.uint8).reshape(240, 320, 3)
    return frame[120, 160].astype(int)


class TestBt601Recording:
    def test_the_programme_is_tagged_bt709(self, runner, config, tmp_path: Path) -> None:
        source = _yellow(runner, tmp_path / "bt601.mp4", bt601=True)
        assert _matrix_tag(runner, source) == "bt470bg"

        programme = _render(runner, config, source, tmp_path)

        assert _matrix_tag(runner, programme) == "bt709"

    def test_a_bt709_reading_keeps_the_yellow(self, runner, config, tmp_path: Path) -> None:
        # Unconverted, this reads 255,201,0: the green channel off by 11.
        source = _yellow(runner, tmp_path / "bt601.mp4", bt601=True)
        programme = _render(runner, config, source, tmp_path)

        pixel = _centre(runner, programme, read_as="bt709")

        assert np.abs(pixel - np.array(YELLOW)).max() <= 5, pixel


class TestUntaggedRecording:
    def test_an_untagged_recording_is_not_converted(self, runner, config, tmp_path: Path) -> None:
        # swscale reads untagged input as BT.601; converting it would shift
        # the ordinary capture. Same reading in, same colour out.
        source = _yellow(runner, tmp_path / "untagged.mp4", bt601=False)
        programme = _render(runner, config, source, tmp_path)

        before = _centre(runner, source, read_as="bt709")
        after = _centre(runner, programme, read_as="bt709")

        assert _matrix_tag(runner, programme) in (None, "unknown")
        assert np.abs(before - after).max() <= 2, (before, after)


class TestFinalEncode:
    def test_the_finished_video_keeps_the_bt709_tag(
        self, runner, config, tmp_path: Path
    ) -> None:
        # The final encode copies its tags from the frames it is given, so a
        # converted programme must come out of composite still BT.709.
        source = _yellow(runner, tmp_path / "bt601.mp4", bt601=True)
        programme = _render(runner, config, source, tmp_path)

        final = composite(
            programme,
            overlay=None,
            mix=None,
            destination=tmp_path / "final.mp4",
            runner=runner,
            config=config,
            encoder=X264,
            target=TARGET,
            duration_seconds=1.5,
        )

        assert _matrix_tag(runner, final.path) == "bt709"
        pixel = _centre(runner, final.path, read_as="bt709")
        assert np.abs(pixel - np.array(YELLOW)).max() <= 5, pixel
