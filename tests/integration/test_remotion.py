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

from backend.core.errors import RenderError
from backend.core.models.enums import TrackKind
from backend.rendering.composite import _placed_segments
from backend.rendering.composition import build_composition
from backend.rendering.overlay_plan import OverlayPlan
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
def rendered_overlay(composition, config, repo_root: Path, tmp_path: Path):
    """The overlay Remotion produced, with the plan that says where it goes.

    The plan matters to every assertion below. Since §66 gained the shortcut,
    a caption in the second half of a two-second clip produces an overlay file
    that is *shorter than the video* and starts at the caption -- so "the
    overlay at 1.4 s" and "the video at 1.4 s" are no longer the same frame,
    and only the plan knows the difference.
    """
    result = render_overlay(
        composition,
        output_path=tmp_path / "overlay.webm",
        config=config.remotion,
        repo_root=repo_root,
    )
    assert result.exists, f"no overlay was produced: {result.reason}"
    return result


@pytest.fixture
def overlay(rendered_overlay) -> Path:
    return rendered_overlay.path


def _frame(video: Path, seconds: float, destination: Path, *, decoder: str | None = None) -> Path:
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if decoder:
        argv += ["-c:v", decoder]
    argv += ["-ss", str(seconds), "-i", str(video), "-frames:v", "1", str(destination)]
    subprocess.run(argv, check=True, capture_output=True, timeout=300)
    return destination


def _composite(
    gameplay: Path,
    overlay: Path,
    destination: Path,
    *,
    decoder: str | None,
    plan: OverlayPlan | None = None,
) -> Path:
    """Lay the overlay over the footage the way the render stage does.

    The filter comes from the production builder rather than being written out
    here, because "does the alpha survive the composite" is a question about
    the graph the pipeline actually runs.
    """
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(gameplay)]
    if decoder:
        argv += ["-c:v", decoder]
    if plan is None:
        filters, label = ["[0:v][1:v]overlay=format=auto[out]"], "out"
    else:
        filters, label = _placed_segments(plan)
    argv += [
        "-i",
        str(overlay),
        "-filter_complex",
        ";".join(filters),
        "-map",
        f"[{label}]",
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


def _inside_the_overlay(result, second: float) -> float:
    """Where a moment of the finished video sits inside the overlay file."""
    if result.plan is None:
        return second
    frame = round(second * result.plan.fps)
    segment = next(one for one in result.plan.segments if one.contains(frame))
    return (frame - segment.shift) / result.plan.fps


def _changed_pixels(left: Path, right: Path) -> int:
    """How many pixels differ, beyond encoder noise."""
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(left).convert("RGB")).astype(int)
    b = np.asarray(Image.open(right).convert("RGB")).astype(int)
    return int((np.abs(a - b).sum(axis=2) > 24).sum())


class TestTheRuntimeItStarts:
    """The renderer runs the interpreter that ships beside it, not the machine's.

    ``shutil.which("node")`` found ``C:\\Program Files\\nodejs`` on the
    development machine and the overlay render started from there — a runtime
    on the drive this application is required never to depend on, and which
    had 5.7 GB free when that requirement was written. ``scripts/rooted.py``
    puts ``tools/node`` first on PATH, but any process that skips the launcher
    inherits the machine's PATH instead.
    """

    def test_the_bundled_node_wins_over_the_machines(self, repo_root: Path) -> None:
        from backend.rendering.remotion import _node_executable

        bundled = repo_root / "tools" / "node" / "node.exe"
        if not bundled.is_file():
            pytest.skip("no bundled node in this checkout")

        assert Path(_node_executable()) == bundled


class TestTheCardIsHandedBack:
    """A montage leaves the machine as it found it.

    Not a nicety: the suite caught the pipeline failing its own render this
    way. The narration reader loads ``qwen2.5:7b-instruct`` during
    GAME_EVENTS, and it was still resident four stages later — *"335 MB of
    video memory is free; Ollama is holding qwen2.5:7b-instruct (4528 MB)"* —
    so Chromium had nothing to start in. Every provider unloads in a
    ``finally``, but only when that instance loaded the model; one left by an
    earlier run, another worker or a killed process is invisible to it.
    """

    def test_the_models_this_app_loads_are_released_by_name(self, config, monkeypatch) -> None:
        from backend.core import gpu

        asked: list[str] = []
        monkeypatch.setattr(gpu, "release_models", lambda names: asked.extend(names) or list(names))
        monkeypatch.setattr(gpu, "free_vram_mb", lambda *_, **__: 4000)
        monkeypatch.setattr(gpu, "release_local_caches", lambda: None)

        gpu.release_everything_we_loaded(config)

        assert config.models.vision.model in asked
        assert config.models.llm.model in asked

    def test_nothing_else_on_the_machine_is_touched(self, config, monkeypatch) -> None:
        # A model another program loaded is that program's to release.
        # Sweeping the card clean would be taking the machine over.
        from backend.core import gpu

        asked: list[str] = []
        monkeypatch.setattr(gpu, "release_models", lambda names: asked.extend(names) or [])
        monkeypatch.setattr(gpu, "free_vram_mb", lambda *_, **__: 4000)
        monkeypatch.setattr(gpu, "release_local_caches", lambda: None)

        gpu.release_everything_we_loaded(config)

        assert "qwen2.5-coder:7b" not in asked, "another tool's model is not ours to unload"

    def test_releasing_reports_what_it_freed(self, config, monkeypatch) -> None:
        # A before and an after, so a claim about the card can be checked
        # rather than believed.
        from backend.core import gpu

        readings = iter([800, 5400])
        monkeypatch.setattr(gpu, "release_models", lambda names: ["qwen2.5vl:7b"])
        monkeypatch.setattr(gpu, "free_vram_mb", lambda *_, **__: next(readings))
        monkeypatch.setattr(gpu, "release_local_caches", lambda: None)

        freed = gpu.release_everything_we_loaded(config)

        assert freed["freed_mb"] == 4600
        assert freed["released"] == ["qwen2.5vl:7b"]

    def test_a_machine_without_ollama_is_not_an_error(self, monkeypatch) -> None:
        # A machine that only transcribes never runs Ollama, and tidying up
        # must not fail a finished video that is already on disk.
        import urllib.error

        from backend.core import gpu

        def refuse(*_args, **_kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(gpu.urllib.request, "urlopen", refuse)

        assert gpu.release_models(["qwen2.5vl:7b"]) == []
        assert gpu.resident_models() == []

    def test_an_unreadable_card_reports_nothing_rather_than_zero(self, config, monkeypatch) -> None:
        # No NVIDIA card, or a driver query that timed out. "Unknown" must not
        # be reported as a number somebody could act on.
        from backend.core import gpu

        monkeypatch.setattr(gpu, "free_vram_mb", lambda *_, **__: None)
        monkeypatch.setattr(gpu, "release_models", lambda names: [])
        monkeypatch.setattr(gpu, "release_local_caches", lambda: None)

        freed = gpu.release_everything_we_loaded(config)

        assert freed["freed_mb"] is None


class TestItLooksAtTheCardFirst:
    """The render assumed an empty card for the life of the project.

    §54's "one heavy model at a time" governs this pipeline's own stages and
    they honour it — every provider unloads, and the Ollama ones send
    ``keep_alive: 0``. It says nothing about the rest of the machine. On
    2026-08-15 a model *another program* had left resident held 4.7 GB of 8,
    with an expiry in the year 2318; Chromium could not connect, timed out
    after 25 s, and nineteen render-dependent tests failed twenty minutes into
    each of two full runs.
    """

    @staticmethod
    def _fitted(config, free_mb, monkeypatch):
        from backend.rendering import remotion as module

        monkeypatch.setattr(module, "free_vram_mb", lambda *_, **__: free_mb)
        monkeypatch.setattr(module, "resident_models", list)
        monkeypatch.setattr(module, "_VRAM_SETTLE_SECONDS", 0.0)
        return module._fitted_to_the_card(config)

    def test_a_card_that_frees_itself_is_not_refused(self, config, monkeypatch) -> None:
        # The render before this one has finished and its Chromium has not
        # handed the memory back yet. That resolves in seconds; a model
        # another program holds does not. Refusing the first would fail every
        # second render on a busy machine.
        from backend.rendering import remotion as module

        readings = iter([600, 4000])
        monkeypatch.setattr(module, "free_vram_mb", lambda *_, **__: next(readings))
        monkeypatch.setattr(module, "resident_models", list)
        monkeypatch.setattr(module, "_VRAM_SETTLE_SECONDS", 0.0)

        fitted = module._fitted_to_the_card(config.remotion.model_copy(update={"concurrency": 4}))

        assert fitted.concurrency == 4

    def test_a_full_card_fails_now_rather_than_in_twenty_minutes(self, config, monkeypatch) -> None:
        with pytest.raises(RenderError) as raised:
            self._fitted(config.remotion, 509, monkeypatch)

        assert "video memory" in str(raised.value)
        assert raised.value.recoverable, "closing something and retrying is the fix"

    def test_the_message_names_what_is_holding_the_memory(self, config, monkeypatch) -> None:
        # An operator told which model holds four gigabytes acts in seconds.
        # "Out of memory" sends them hunting.
        from backend.core.gpu import ResidentModel
        from backend.rendering import remotion as module

        monkeypatch.setattr(module, "free_vram_mb", lambda *_, **__: 400)
        monkeypatch.setattr(
            module,
            "resident_models",
            lambda: [ResidentModel(name="qwen2.5-coder:7b", vram_mb=4528)],
        )
        monkeypatch.setattr(
            module, "describe_pressure", lambda free: f"{free} MB free; qwen2.5-coder:7b"
        )

        with pytest.raises(RenderError) as raised:
            module._fitted_to_the_card(config.remotion)

        assert "qwen2.5-coder:7b" in str(raised.value)
        assert "qwen2.5-coder:7b (4528 MB)" in raised.value.details["resident_models"]

    def test_a_tight_card_renders_with_fewer_pages(self, config, monkeypatch) -> None:
        # Slower is a result. A timeout is not.
        remotion = config.remotion.model_copy(update={"concurrency": 10})

        fitted = self._fitted(remotion, 1500, monkeypatch)

        assert fitted.concurrency == 6, "1500 MB at 250 MB a page"

    def test_a_roomy_card_is_left_alone(self, config, monkeypatch) -> None:
        remotion = config.remotion.model_copy(update={"concurrency": 4})

        fitted = self._fitted(remotion, 6000, monkeypatch)

        assert fitted.concurrency == 4

    def test_a_machine_that_cannot_say_is_not_second_guessed(self, config, monkeypatch) -> None:
        # No NVIDIA card, no driver tool, a query that timed out. Reading
        # "unknown" as "empty" is the assumption that caused this defect;
        # reading it as "full" would break every machine without the card.
        fitted = self._fitted(config.remotion, None, monkeypatch)

        assert fitted is config.remotion

    def test_the_check_can_be_turned_off(self, config, monkeypatch) -> None:
        remotion = config.remotion.model_copy(update={"min_free_vram_mb": 0})

        assert self._fitted(remotion, 10, monkeypatch) is remotion


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
        self, rendered_overlay, overlay: Path, tmp_path: Path
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
                # 1.4 s of the *video*. The overlay file holds only the
                # stretches that draw something, so where that lands inside it
                # is the plan's answer, not the same number.
                f"{_inside_the_overlay(rendered_overlay, 1.4)}",
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
        self, gameplay: Path, rendered_overlay, overlay: Path, tmp_path: Path
    ) -> None:
        # The acceptance, stated as a measurement. A broken alpha channel shows
        # up here as thousands of changed pixels.
        composited = _composite(
            gameplay,
            overlay,
            tmp_path / "out.mp4",
            decoder="libvpx-vp9",
            plan=rendered_overlay.plan,
        )

        before = _frame(gameplay, 0.05, tmp_path / "g.png")
        after = _frame(composited, 0.05, tmp_path / "c.png")

        assert _changed_pixels(before, after) == 0

    def test_compositing_changes_only_the_caption_region(
        self, gameplay: Path, rendered_overlay, overlay: Path, tmp_path: Path
    ) -> None:
        composited = _composite(
            gameplay,
            overlay,
            tmp_path / "out2.mp4",
            decoder="libvpx-vp9",
            plan=rendered_overlay.plan,
        )

        before = _frame(gameplay, 1.4, tmp_path / "g2.png")
        after = _frame(composited, 1.4, tmp_path / "c2.png")
        changed = _changed_pixels(before, after)

        assert changed > 0, "the caption never reached the finished frame"
        # A caption occupies a strip, not the screen.
        assert changed < WIDTH * HEIGHT * 0.5

    def test_the_native_decoder_loses_the_alpha(
        self, gameplay: Path, rendered_overlay, overlay: Path, tmp_path: Path
    ) -> None:
        """Why `overlay_input_arguments` names a decoder at all.

        FFmpeg's native VP9 decoder discards the side-channel alpha without a
        word, and the overlay composites as an opaque rectangle. This asserts
        the failure exists, so the fix is not mistaken for superstition.

        Measured at the caption, against the same frame the working decoder
        changes by a strip: the failure is the size of the change, not its
        presence.
        """
        composited = _composite(
            gameplay,
            overlay,
            tmp_path / "out3.mp4",
            decoder=None,
            plan=rendered_overlay.plan,
        )

        before = _frame(gameplay, 1.4, tmp_path / "g3.png")
        after = _frame(composited, 1.4, tmp_path / "c3.png")

        assert _changed_pixels(before, after) > WIDTH * HEIGHT * 0.5, (
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
