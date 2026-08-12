"""Phase 9 acceptance: EDL → Remotion → an alpha overlay that composites.

    **Acceptance: EDL → Remotion → an alpha overlay that composites correctly.**

"Correctly" is the whole test, and it is not "the file exists". An overlay
without a working alpha channel is an opaque rectangle over the gameplay —
which renders, encodes, uploads, and is only discovered by watching the video.
So the check is a measurement: composite the overlay over known footage and
count the pixels that changed. Before the caption appears, that number must be
**zero**.

One trap is pinned here on purpose. VP9 in WebM carries its alpha as a
per-block side channel rather than in the pixel format, so `ffprobe` reports
`yuv420p` and FFmpeg's *native* VP9 decoder silently discards the alpha.
Naming `libvpx-vp9` is the fix, and `test_the_native_decoder_loses_the_alpha`
exists so nobody removes it.

These tests need Node and an installed Remotion project, so they are marked
``requires_node`` and skip cleanly on a machine without one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.models.enums import TrackKind
from backend.rendering.composition import build_composition
from backend.rendering.remotion import (
    is_available,
    overlay_input_arguments,
    project_dir,
    render_overlay,
    write_composition,
)
from backend.timeline.captions import Caption
from backend.timeline.models import Timeline, TimelineClip, Track

pytestmark = [pytest.mark.integration, pytest.mark.requires_ffmpeg]

MEDIA = "media-aaaaaaaaaaaa"
PROJECT = "proj-aaaaaaaaaaaa"

#: Small and short. The acceptance is about alpha, not about throughput, and a
#: 1080p60 render would turn a test suite into a coffee break.
WIDTH, HEIGHT, FPS, SECONDS = 320, 180, 15, 2


@pytest.fixture
def timeline() -> Timeline:
    clip = TimelineClip(
        id="clip-000000000000",
        media_id=MEDIA,
        clip_index=0,
        source_in=0.0,
        source_out=float(SECONDS),
        timeline_start=0.0,
        timeline_end=float(SECONDS),
    )
    return Timeline(project_id=PROJECT).with_track(Track(kind=TrackKind.VIDEO, clips=(clip,)))


@pytest.fixture
def composition(timeline, config):
    """A description with one caption in the second half of a two-second clip."""
    caption = Caption(
        id="cap-000000000000",
        index=0,
        timeline_start=1.0,
        timeline_end=1.9,
        text="NO WAY",
        language="en",
        clip_id="clip-000000000000",
        words=(("NO", 1.0, 1.4), ("WAY", 1.4, 1.9)),
    )
    return build_composition(
        timeline,
        captions=[caption],
        caption_config=config.captions,
        width=WIDTH,
        height=HEIGHT,
        fps=FPS,
    )


@pytest.fixture
def gameplay(tmp_path: Path) -> Path:
    """Known footage to composite over."""
    target = tmp_path / "gameplay.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={SECONDS}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return target


@pytest.fixture
def overlay(composition, config, repo_root: Path, tmp_path: Path) -> Path:
    result = render_overlay(
        composition,
        output_path=tmp_path / "overlay.webm",
        config=config.remotion,
        repo_root=repo_root,
    )
    assert result.exists, f"no overlay was produced: {result.reason}"
    return result.path


def _frame(video: Path, seconds: float, destination: Path, *, decoder: str | None = None) -> Path:
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if decoder:
        argv += ["-c:v", decoder]
    argv += ["-ss", str(seconds), "-i", str(video), "-frames:v", "1", str(destination)]
    subprocess.run(argv, check=True, capture_output=True, timeout=300)
    return destination


def _composite(gameplay: Path, overlay: Path, destination: Path, *, decoder: str | None) -> Path:
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(gameplay)]
    if decoder:
        argv += ["-c:v", decoder]
    argv += [
        "-i",
        str(overlay),
        "-filter_complex",
        "[0:v][1:v]overlay=format=auto",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(destination),
    ]
    subprocess.run(argv, check=True, capture_output=True, timeout=600)
    return destination


def _changed_pixels(left: Path, right: Path) -> int:
    """How many pixels differ, beyond encoder noise."""
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(left).convert("RGB")).astype(int)
    b = np.asarray(Image.open(right).convert("RGB")).astype(int)
    return int((np.abs(a - b).sum(axis=2) > 24).sum())


class TestTheDescription:
    """Runs anywhere: no Node needed to check what would be handed over."""

    def test_the_composition_file_is_written_where_remotion_reads_it(
        self, composition, config, repo_root: Path, tmp_path: Path
    ) -> None:
        path = write_composition(composition, tmp_path / "composition.json")

        assert path.is_file()
        assert path.read_text(encoding="utf-8").endswith("\n")

    def test_availability_is_reported_rather_than_assumed(self, config, repo_root: Path) -> None:
        available, reason = is_available(config.remotion, repo_root)

        assert isinstance(available, bool)
        assert reason, "an unavailable renderer must say why (§95)"

    def test_the_project_ships_with_the_code(self, config, repo_root: Path) -> None:
        # Not under the data root: the composition is code, like game profiles.
        assert project_dir(config.remotion, repo_root) == repo_root / "remotion"

    def test_an_empty_composition_skips_the_chromium_pass(
        self, timeline, config, repo_root: Path, tmp_path: Path
    ) -> None:
        # D-008: a caption-free video never pays for Chromium.
        empty = build_composition(
            timeline, caption_config=config.captions, width=WIDTH, height=HEIGHT, fps=FPS
        )
        assert empty.is_empty

        result = render_overlay(
            empty,
            output_path=tmp_path / "unused.webm",
            config=config.remotion,
            repo_root=repo_root,
        )

        assert result.skipped
        assert result.path is None
        assert not (tmp_path / "unused.webm").exists()

    def test_an_unavailable_renderer_degrades_rather_than_failing(
        self, composition, config, repo_root: Path, tmp_path: Path
    ) -> None:
        # §95: a machine without Node produces a video without captions, not a
        # failed render.
        disabled = config.remotion.model_copy(update={"enabled": False})

        result = render_overlay(
            composition,
            output_path=tmp_path / "unused.webm",
            config=disabled,
            repo_root=repo_root,
        )

        assert result.skipped
        assert "enabled" in result.reason


@pytest.mark.requires_node
@pytest.mark.slow
class TestWhenChromiumCrashes:
    """A page crash is memory, and the remedy is known (§95).

    Ten concurrent 1080p pages crashed at frame 13,711 of 18,572 on a 32 GB
    machine with 16 GB free. What made it expensive was not the crash: it was
    that the failure said "Remotion exited with code 1" and nothing else, so
    finding "Page crashed!" meant running the CLI again by hand. The tail had
    been captured all along and thrown away.
    """

    def test_the_failure_says_what_remotion_said(self) -> None:
        from backend.rendering.remotion import _reason_from

        tail = [
            "Rendered 13711/18572, time remaining: 1m 57s",
            "[31mError: Page crashed![39m",
            "[31m    at #onTargetCrashed (BrowserPage.js:246:28)[39m",
        ]

        assert _reason_from(tail) == "Error: Page crashed!"

    def test_progress_lines_are_not_a_diagnosis(self) -> None:
        from backend.rendering.remotion import _reason_from

        assert _reason_from(["Rendered 1/10", "Rendered 2/10"]) == ""

    def test_a_page_crash_retries_with_half_the_pages(self, config) -> None:
        from backend.core.errors import ErrorCode, RenderError
        from backend.rendering.remotion import _fewer_pages, resolved_concurrency

        crash = RenderError(
            "Remotion exited with code 1: Error: Page crashed!",
            code=ErrorCode.REMOTION_FAILED,
        )

        retry = _fewer_pages(config.remotion, crash)

        assert retry == max(1, resolved_concurrency(config.remotion) // 2)

    def test_any_other_failure_is_not_retried(self, config) -> None:
        # Repeating a twenty-minute pass hoping it behaves differently is not a
        # strategy. Only the failure with a known remedy gets a second go.
        from backend.core.errors import ErrorCode, RenderError
        from backend.rendering.remotion import _fewer_pages

        missing = RenderError(
            "Remotion exited with code 1: Error: ENOENT no such file",
            code=ErrorCode.REMOTION_FAILED,
        )

        assert _fewer_pages(config.remotion, missing) is None

    def test_a_crash_at_one_page_is_a_different_problem(self, config) -> None:
        from backend.core.errors import ErrorCode, RenderError
        from backend.rendering.remotion import _fewer_pages

        crash = RenderError("Page crashed", code=ErrorCode.REMOTION_FAILED)
        single = config.remotion.model_copy(update={"concurrency": 1})

        assert _fewer_pages(single, crash) is None


class TestAcceptance:
    """**EDL → Remotion → an alpha overlay that composites correctly.**"""

    def test_the_overlay_is_produced(self, overlay: Path) -> None:
        assert overlay.is_file()
        assert overlay.stat().st_size > 0

    def test_the_overlay_carries_no_audio(self, overlay: Path) -> None:
        # An overlay is a picture. A silent Opus track would be one more thing
        # the composite has to know to ignore.
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(overlay),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.stdout.strip() == ""

    def test_the_overlay_matches_the_requested_format(self, overlay: Path) -> None:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(overlay),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.stdout.strip() == f"{WIDTH},{HEIGHT}"

    def test_the_alpha_channel_is_transparent_where_nothing_is_drawn(
        self, overlay: Path, tmp_path: Path
    ) -> None:
        import numpy as np
        from PIL import Image

        # Before the caption starts at 1.0 s.
        frame = tmp_path / "alpha_early.png"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-c:v",
                "libvpx-vp9",
                "-i",
                str(overlay),
                "-vf",
                "alphaextract,format=gray",
                "-frames:v",
                "1",
                str(frame),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        alpha = np.asarray(Image.open(frame).convert("L"))

        assert alpha.max() == 0, "the overlay is opaque before it draws anything"

    def test_the_alpha_channel_is_opaque_where_the_caption_is(
        self, overlay: Path, tmp_path: Path
    ) -> None:
        import numpy as np
        from PIL import Image

        frame = tmp_path / "alpha_mid.png"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-c:v",
                "libvpx-vp9",
                "-i",
                str(overlay),
                "-ss",
                "1.4",
                "-vf",
                "alphaextract,format=gray",
                "-frames:v",
                "1",
                str(frame),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        alpha = np.asarray(Image.open(frame).convert("L"))

        assert alpha.max() > 200, "the caption drew nothing"
        # And most of the frame is still see-through: a caption, not a curtain.
        assert (alpha < 20).mean() > 0.5

    def test_compositing_changes_nothing_before_the_caption(
        self, gameplay: Path, overlay: Path, tmp_path: Path
    ) -> None:
        # The acceptance, stated as a measurement. A broken alpha channel shows
        # up here as thousands of changed pixels.
        composited = _composite(gameplay, overlay, tmp_path / "out.mp4", decoder="libvpx-vp9")

        before = _frame(gameplay, 0.05, tmp_path / "g.png")
        after = _frame(composited, 0.05, tmp_path / "c.png")

        assert _changed_pixels(before, after) == 0

    def test_compositing_changes_only_the_caption_region(
        self, gameplay: Path, overlay: Path, tmp_path: Path
    ) -> None:
        composited = _composite(gameplay, overlay, tmp_path / "out2.mp4", decoder="libvpx-vp9")

        before = _frame(gameplay, 1.4, tmp_path / "g2.png")
        after = _frame(composited, 1.4, tmp_path / "c2.png")
        changed = _changed_pixels(before, after)

        assert changed > 0, "the caption never reached the finished frame"
        # A caption occupies a strip, not the screen.
        assert changed < WIDTH * HEIGHT * 0.5

    def test_the_native_decoder_loses_the_alpha(
        self, gameplay: Path, overlay: Path, tmp_path: Path
    ) -> None:
        """Why `overlay_input_arguments` names a decoder at all.

        FFmpeg's native VP9 decoder discards the side-channel alpha without a
        word, and the overlay composites as an opaque rectangle. This asserts
        the failure exists, so the fix is not mistaken for superstition.
        """
        composited = _composite(gameplay, overlay, tmp_path / "out3.mp4", decoder=None)

        before = _frame(gameplay, 0.05, tmp_path / "g3.png")
        after = _frame(composited, 0.05, tmp_path / "c3.png")

        assert _changed_pixels(before, after) > 0, (
            "the native decoder now preserves alpha; the explicit decoder may be "
            "unnecessary and this test should be revisited"
        )

    def test_the_helper_names_the_decoder_that_works(self, overlay: Path) -> None:
        arguments = overlay_input_arguments(overlay, "webm_vp9_alpha")

        assert arguments[:2] == ["-c:v", "libvpx-vp9"]
        assert arguments[-1] == str(overlay)

    def test_progress_is_reported_while_rendering(
        self, composition, config, repo_root: Path, tmp_path: Path
    ) -> None:
        # A render with no feedback is indistinguishable from a hung one.
        seen: list[float] = []
        render_overlay(
            composition,
            output_path=tmp_path / "progress.webm",
            config=config.remotion,
            repo_root=repo_root,
            on_progress=lambda fraction, _message: seen.append(fraction),
        )

        assert seen, "the render reported no progress at all"
        assert all(0.0 <= fraction <= 1.0 for fraction in seen)
