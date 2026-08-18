"""Running Remotion (SPEC §64, §66, §67; decision D-008).

Remotion rasterises every frame through Chromium. That is the right cost for
captions and motion graphics and the wrong cost for stitching twenty minutes of
gameplay, so this renders an **overlay only**: a transparent canvas that FFmpeg
composites over footage it cut itself (§67's
`Source → EDL → Timeline → Remotion Composition → FFmpeg → Final MP4`).

Three things follow from that, and all three are enforced here rather than
hoped for:

**The pass is skippable.** A video with no captions and no overlay effects has
nothing to draw, and paying for Chromium to render twenty minutes of
transparency would be absurd. ``skip_when_no_overlays`` is the config knob;
:attr:`Composition.is_empty` is what it reads.

**Nothing is decided here.** §64 is explicit that Remotion executes and does
not choose. The composition file is written from an already-finished
description, and this module adds nothing to it.

**No shell.** §85. The CLI is invoked with an explicit argument vector, like
every other subprocess in this codebase.

Remotion is a Node dependency, and a machine without it is not broken -- it
just cannot draw overlays. :func:`is_available` reports that, and the caller
degrades (§95) rather than failing a render that FFmpeg could still complete
without captions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from backend.config.schema import RemotionConfig
from backend.core.errors import ErrorCode, RenderError
from backend.core.gpu import describe_pressure, free_vram_mb, resident_models
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.media.ffmpeg import CancelledError
from backend.rendering.composition import Composition
from backend.rendering.overlay_plan import OverlayPlan, compact, plan_overlay, whole

logger = get_logger("rendering.remotion", LogChannel.RENDERING)

ProgressCallback = Callable[[float, str], None]
CancelCheck = Callable[[], bool]

#: How often the render loop checks for cancellation, in seconds.
_POLL_SECONDS: Final[float] = 0.5

#: Remotion writes progress to stdout as a percentage; this is enough to find it.
_PROGRESS_MARKER: Final[str] = "%"


@dataclass(frozen=True, slots=True)
class OverlayResult:
    """What the Remotion pass produced, or why it produced nothing."""

    path: Path | None
    skipped: bool = False
    reason: str = ""
    frames: int = 0
    duration_seconds: float = 0.0
    #: Where the rendered stretches belong in the finished video. The composite
    #: needs this to put them back; ``None`` means the file covers the whole
    #: video, which is what it meant before segments existed.
    plan: OverlayPlan | None = None

    @property
    def exists(self) -> bool:
        return self.path is not None and self.path.is_file()

    def summary(self) -> dict[str, Any]:
        return {
            "overlay_path": str(self.path) if self.path else None,
            "skipped": self.skipped,
            "reason": self.reason,
            "frames": self.frames,
            "render_seconds": round(self.duration_seconds, 2),
            **({"plan": self.plan.summary()} if self.plan is not None else {}),
        }


def project_dir(config: RemotionConfig, repo_root: Path) -> Path:
    """Where the Remotion project lives.

    Resolved against the repository, not the data root: the project is code
    that ships with the application, in the same way game profiles are.
    """
    directory = Path(config.project_dir)
    return directory if directory.is_absolute() else repo_root / directory


def is_available(config: RemotionConfig, repo_root: Path) -> tuple[bool, str]:
    """Whether this machine can render an overlay, and why not if it cannot.

    Checked rather than assumed, because the answer decides between a video
    with captions and a video without -- and the second is a legitimate
    outcome (§95), not a crash.
    """
    if not config.enabled:
        return False, "remotion.enabled is false"
    if shutil.which("node") is None:
        return False, "node is not on PATH"
    directory = project_dir(config, repo_root)
    if not directory.is_dir():
        return False, f"the Remotion project is missing at {directory}"
    if not (directory / "package.json").is_file():
        return False, f"no package.json in {directory}"
    if not (directory / "node_modules").is_dir():
        return False, f"dependencies are not installed in {directory} (run npm install)"
    if not (directory / "node_modules" / "@remotion" / "cli" / "remotion-cli.js").is_file():
        return False, f"the Remotion CLI is not installed in {directory} (run npm install)"
    return True, "ready"


def write_composition(composition: Composition, destination: Path) -> Path:
    """Write the description Remotion reads (§64).

    Sorted keys and a trailing newline: the file is an input to a cached render
    (§48), so two runs over the same edit must produce the same bytes.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(composition.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)
    destination.write_text(payload + "\n", encoding="utf-8")
    logger.info(
        "Wrote the composition description",
        extra={"path": str(destination), **composition.summary()},
    )
    return destination


def render_overlay(
    composition: Composition,
    *,
    output_path: Path,
    config: RemotionConfig,
    repo_root: Path,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> OverlayResult:
    """Render the overlay layer, or explain why it was not rendered.

    Args:
        composition: the finished description. Nothing is added to it here.
        output_path: where the alpha overlay goes.
        config: ``remotion`` section of the render configuration.
        repo_root: repository root, for locating the project.
        on_progress: called with ``(fraction, message)``.
        should_cancel: polled between progress reads; §82 requires a
            cancellation to leave the project consistent, and an unfinished
            overlay file is removed rather than left to be composited.

    Returns:
        An :class:`OverlayResult`. ``skipped`` is a normal outcome, not a
        failure.
    """
    if config.skip_when_no_overlays and composition.is_empty:
        # D-008's whole point: a caption-free video never pays for Chromium.
        logger.info("No overlay elements; skipping the Remotion pass")
        return OverlayResult(path=None, skipped=True, reason="nothing to draw")

    available, why = is_available(config, repo_root)
    if not available:
        # §95: a missing renderer degrades to a video without captions. It does
        # not fail a render FFmpeg could still complete.
        logger.warning(
            "Remotion is unavailable; continuing without an overlay", extra={"reason": why}
        )
        return OverlayResult(path=None, skipped=True, reason=why)

    config = _fitted_to_the_card(config)

    plan = plan_overlay(
        composition,
        merge_gap_frames=max(0, round(config.overlay_merge_gap_seconds * composition.fps)),
        max_segments=config.max_overlay_segments,
        min_saved_ratio=config.min_overlay_saving,
    )
    rendered = compact(composition, plan)
    if rendered is composition and not plan.is_whole:
        # compact() refused the plan, so the file will cover everything.
        plan = whole(
            composition.duration_in_frames,
            fps=composition.fps,
            reason="the plan did not map cleanly",
        )
    if not plan.is_whole:
        logger.info(
            "Rendering only the stretches that carry an overlay",
            extra=plan.summary(),
        )

    directory = project_dir(config, repo_root)
    composition_path = write_composition(rendered, directory / "public" / config.input_filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)

    argv = _render_command(config, composition_path, output_path, directory)
    started = time.perf_counter()
    with log_duration(
        logger,
        "Rendered the overlay",
        frames=rendered.duration_in_frames,
        output=str(output_path),
    ) as fields:
        try:
            _run(argv, directory, config, on_progress, should_cancel, output_path)
        except RenderError as error:
            # A page crash is Chromium running out of memory, and the remedy is
            # known: fewer pages. Ten tabs each holding a 1080p document crashed
            # at frame 13,711 of 18,572 on a 32 GB machine. Halving is slower
            # than failing is useless.
            retry = _fewer_pages(config, error)
            if retry is None:
                raise
            logger.warning(
                "Remotion crashed a page; retrying with fewer at once",
                extra={"concurrency": retry, "reason": str(error)[:160]},
            )
            if on_progress is not None:
                on_progress(0.0, f"Retrying the overlay with {retry} pages at once")
            argv = _render_command(
                config.model_copy(update={"concurrency": retry}),
                composition_path,
                output_path,
                directory,
            )
            _run(argv, directory, config, on_progress, should_cancel, output_path)
        fields["size_bytes"] = output_path.stat().st_size if output_path.is_file() else 0

    if not output_path.is_file():
        raise RenderError(
            "Remotion reported success but produced no overlay file.",
            code=ErrorCode.REMOTION_FAILED,
            details={"expected": str(output_path), "composition": str(composition_path)},
        )
    return OverlayResult(
        path=output_path,
        frames=rendered.duration_in_frames,
        duration_seconds=time.perf_counter() - started,
        plan=None if plan.is_whole else plan,
        reason=plan.reason if not plan.is_whole else "",
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _render_command(
    config: RemotionConfig, composition_path: Path, output_path: Path, directory: Path
) -> list[str]:
    """The Remotion CLI invocation (§85: an explicit argv, never a shell).

    ``--codec`` carries the alpha format from configuration rather than being
    hard-coded, because the trade -- VP9's small files against ProRes 4444's
    lossless quality -- is a deployment choice, not a code one.
    """
    codec, extra = _codec_arguments(config)
    entry = _entry_point(directory)
    return [
        # `node <cli>.js` rather than `npx remotion`: npx is a shell shim, which
        # on Windows means `npx.cmd` and a subprocess call that cannot find it
        # without a shell -- and §85 says no shell. The installed CLI is a
        # plain JavaScript file, and node runs it identically everywhere.
        _node_executable(),
        str(_cli_entry(directory)),
        "render",
        str(entry),
        config.composition_id,
        str(output_path),
        f"--codec={codec}",
        f"--concurrency={resolved_concurrency(config)}",
        f"--props={composition_path}",
        # An overlay has no sound. Without this Remotion adds a silent Opus
        # track, which the composite would then have to know to ignore.
        "--muted",
        "--log=info",
        *extra,
    ]


def _node_executable() -> str:
    """The node binary, resolved rather than assumed to be on the child's PATH.

    The bundled ``tools/node`` wins over whatever PATH offers. This machine
    has a system install under ``C:\\Program Files``, and PATH found it: the
    render was starting a runtime from the drive the application is required
    never to depend on, and which had 5.7 GB free when that rule was written.
    ``scripts/rooted.py`` puts the bundled directory first, but a process that
    did not go through the launcher -- a test, a script, an editor's terminal
    -- inherits the machine's PATH and silently uses the other one.
    """
    from backend.config.paths import find_repository_root

    bundled = find_repository_root() / "tools" / "node" / "node.exe"
    if bundled.is_file():
        return str(bundled)

    found = shutil.which("node")
    if found is None:
        raise RenderError(
            "node is not on PATH, so the Remotion CLI cannot be started.",
            code=ErrorCode.REMOTION_FAILED,
            recoverable=False,
        )
    return found


def _cli_entry(directory: Path) -> Path:
    """The installed Remotion CLI's JavaScript entry point."""
    candidate = directory / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
    if not candidate.is_file():
        raise RenderError(
            f"The Remotion CLI is not installed in {directory}.",
            code=ErrorCode.REMOTION_FAILED,
            details={"expected": str(candidate), "remedy": "npm install"},
            recoverable=False,
        )
    return candidate


def _codec_arguments(config: RemotionConfig) -> tuple[str, list[str]]:
    """Map the configured overlay format onto Remotion's codec flags.

    Only alpha-capable codecs are offered. An overlay without an alpha channel
    is an opaque rectangle over the gameplay, which is worse than no overlay at
    all and would not be obvious until someone watched the finished video.
    """
    # --image-format=png is not optional for either: Remotion rasterises to
    # JPEG by default, and JPEG has no alpha channel to carry.
    if config.overlay_format == "prores_4444":
        return "prores", [
            "--image-format=png",
            "--pixel-format=yuva444p10le",
            "--prores-profile=4444",
        ]
    if config.overlay_format == "webm_vp9_alpha":
        return "vp9", ["--image-format=png", "--pixel-format=yuva420p"]
    raise RenderError(
        f"remotion.overlay_format {config.overlay_format!r} is not an alpha-capable format.",
        code=ErrorCode.REMOTION_FAILED,
        details={"supported": ["webm_vp9_alpha", "prores_4444"]},
        recoverable=False,
    )


def _entry_point(directory: Path) -> Path:
    """The project's Remotion entry file."""
    for candidate in ("src/index.ts", "src/index.tsx", "src/index.js"):
        path = directory / candidate
        if path.is_file():
            return path
    raise RenderError(
        f"No Remotion entry point in {directory}.",
        code=ErrorCode.REMOTION_FAILED,
        details={"looked_for": ["src/index.ts", "src/index.tsx", "src/index.js"]},
        recoverable=False,
    )


def _run(
    argv: Sequence[str],
    directory: Path,
    config: RemotionConfig,
    on_progress: ProgressCallback | None,
    should_cancel: CancelCheck | None,
    output_path: Path,
) -> None:
    """Run the CLI, relaying progress and honouring cancellation (§82)."""
    command = [str(part) for part in argv]
    logger.info("Running Remotion", extra={"command": command[:6], "cwd": str(directory)})
    try:
        process = subprocess.Popen(  # explicit argv, no shell (§85)
            command,
            cwd=str(directory),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_environment(directory),
            shell=False,
            **_platform_kwargs(),
        )
    except OSError as exc:
        raise RenderError(
            f"Cannot run the Remotion CLI: {exc}",
            code=ErrorCode.REMOTION_FAILED,
            details={"command": command[:6], "cwd": str(directory)},
            cause=exc,
        ) from exc

    tail: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip()
            if text:
                tail.append(text)
                del tail[:-40]
                _relay(text, on_progress)
            if should_cancel is not None and should_cancel():
                process.kill()
                process.wait(timeout=30)
                output_path.unlink(missing_ok=True)
                raise CancelledError("Overlay render cancelled.")
        returncode = process.wait(timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        output_path.unlink(missing_ok=True)
        raise RenderError(
            f"Remotion timed out after {config.timeout_seconds}s.",
            code=ErrorCode.REMOTION_FAILED,
            details={"tail": tail[-10:]},
            cause=exc,
        ) from exc
    finally:
        if process.stdout is not None:
            process.stdout.close()

    if returncode != 0:
        output_path.unlink(missing_ok=True)
        # The tail was always captured and never surfaced, so every Remotion
        # failure read as "exited with code 1" and learning why meant running
        # the CLI again by hand. The reason is on the last few lines.
        reason = _reason_from(tail)
        raise RenderError(
            f"Remotion exited with code {returncode}: {reason}"
            if reason
            else f"Remotion exited with code {returncode}.",
            code=ErrorCode.REMOTION_FAILED,
            details={"returncode": returncode, "tail": tail[-15:]},
        )


#: Remotion colours its output, and a colour code in an error message
#: is noise wherever that message is read.
_ANSI: Final = re.compile(r"\[[0-9;]*m")

#: Chromium losing a renderer process. Remotion reports it as a page crash and
#: exits 1; the cause is almost always memory, so the remedy is fewer pages.
PAGE_CRASH: Final[str] = "Page crashed"

#: How long to let a briefly-full card settle before believing it. The render
#: before this one may have finished with its Chromium still handing memory
#: back; a model another program is holding will still be there afterwards.
_VRAM_SETTLE_SECONDS: Final[float] = 4.0

#: Lines that are progress, not diagnosis.
_NOISE: Final[tuple[str, ...]] = ("Rendered ", "Bundling", "Compiled", "webpack")


def _reason_from(tail: Sequence[str]) -> str:
    """The most informative line Remotion printed before giving up."""
    for line in reversed(tail):
        text = _ANSI.sub("", line).strip()
        if not text or text.startswith(_NOISE) or text.startswith("at "):
            continue
        return text[:200]
    return ""


def _fitted_to_the_card(config: RemotionConfig) -> RemotionConfig:
    """Check the card has room for Chromium before spending twenty minutes.

    §54's "one heavy model at a time" governs this pipeline's own stages, and
    they honour it. It says nothing about the rest of the machine — and on
    2026-08-15 a model another program had left resident held 4.7 GB of 8,
    with an expiry in the year 2318. Chromium could not connect, timed out
    after 25 s, and the failure arrived at the end of a twenty-minute render
    rather than at the start of it.

    Two outcomes, and neither is a guess:

    * **Not enough to start at all** — say so immediately, and name what is
      holding the memory so it can be closed.
    * **Enough, but tight** — render with fewer pages. Slower is a result; a
      timeout is not.

    A machine that cannot report its VRAM is left completely alone. "Unknown"
    reading as "empty" is the assumption that caused this in the first place,
    and reading it as "full" would break every machine without an NVIDIA card.
    """
    if config.min_free_vram_mb <= 0:
        return config

    free_mb = free_vram_mb()
    if free_mb is None:
        return config

    if free_mb < config.min_free_vram_mb:
        # A card can be briefly full for an honest reason: the render before
        # this one has finished and its Chromium has not yet handed the memory
        # back. That resolves in seconds. A model another program is holding
        # does not resolve at all, and telling them apart costs one short
        # wait, taken only on a card that is already tight.
        time.sleep(_VRAM_SETTLE_SECONDS)
        free_mb = free_vram_mb() or free_mb

    if free_mb < config.min_free_vram_mb:
        raise RenderError(
            "There is not enough free video memory to start Chromium for the "
            f"overlay: {describe_pressure(free_mb)}. Close what is holding it "
            "and render again.",
            code=ErrorCode.REMOTION_FAILED,
            details={
                "free_vram_mb": free_mb,
                "required_vram_mb": config.min_free_vram_mb,
                "resident_models": [str(model) for model in resident_models()],
            },
            recoverable=True,
        )

    requested = resolved_concurrency(config)
    affordable = max(1, free_mb // config.vram_per_page_mb)
    if affordable >= requested:
        return config

    logger.warning(
        "Rendering the overlay with fewer pages: the card is tight",
        extra={
            "free_vram_mb": free_mb,
            "requested_concurrency": requested,
            "concurrency": affordable,
        },
    )
    return config.model_copy(update={"concurrency": affordable})


def _fewer_pages(config: RemotionConfig, error: RenderError) -> int | None:
    """The concurrency to retry a crashed render at, or ``None`` not to retry.

    Only a page crash is retried, and only once -- a second crash at half the
    pages is a different problem, and repeating a twenty-minute pass hoping it
    behaves differently is not a strategy.
    """
    if PAGE_CRASH.lower() not in str(error).lower():
        return None
    current = resolved_concurrency(config)
    if current <= 1:
        return None
    return max(1, current // 2)


def _relay(line: str, on_progress: ProgressCallback | None) -> None:
    """Turn a Remotion log line into a progress report, when it carries one."""
    if on_progress is None:
        return
    if _PROGRESS_MARKER not in line:
        return
    for token in line.replace("(", " ").replace(")", " ").split():
        if token.endswith("%"):
            try:
                fraction = float(token[:-1]) / 100.0
            except ValueError:
                continue
            on_progress(min(max(fraction, 0.0), 1.0), line[:120])
            return


def _environment(directory: Path) -> dict[str, str]:
    """The child's environment.

    Remotion caches a browser download, and the default location is under the
    user's home directory. On a machine whose system drive is nearly full --
    the one this was built on -- that is where a multi-hundred-megabyte
    download should not go, so it is pointed inside the project.
    """
    env = dict(os.environ)
    env.setdefault("REMOTION_BROWSER_DOWNLOAD_DIR", str(directory / ".browser"))
    env.setdefault("PUPPETEER_CACHE_DIR", str(directory / ".browser"))
    # Chromium in a container or a constrained session needs this; harmless
    # elsewhere and cheaper than diagnosing it once.
    env.setdefault("REMOTION_DISABLE_HEADLESS_WARNING", "1")
    return env


def _platform_kwargs() -> dict[str, Any]:
    """Keep a console window from flashing on Windows, as FFmpeg does."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


#: The decoder FFmpeg must be told to use when reading a VP9 overlay.
#:
#: VP9 in WebM carries its alpha as a per-block side channel rather than in the
#: pixel format, so ``ffprobe`` reports ``yuv420p`` and FFmpeg's *native* VP9
#: decoder discards the alpha without a word. The result composites as an opaque
#: rectangle over the gameplay -- a failure invisible to every check except
#: watching the video. Naming the decoder is the whole fix.
VP9_ALPHA_DECODER: Final[str] = "libvpx-vp9"


def resolved_concurrency(config: Any) -> int:
    """How many Chromium tabs render in parallel.

    ``0`` sizes to the machine: every core but two, floor of two. The overlay
    pass is embarrassingly parallel -- each frame is an independent
    screenshot -- so this is the closest thing the render has to a free
    speed-up, and the shipped ``2`` on a 12-thread machine was measured
    costing most of an hour on a ten-minute video.
    """
    import os

    if config.concurrency and config.concurrency > 0:
        return int(config.concurrency)
    return max(2, (os.cpu_count() or 4) - 2)


def overlay_input_arguments(overlay_path: Path, overlay_format: str) -> list[str]:
    """FFmpeg input arguments that preserve an overlay's alpha channel.

    Used by the composite in Phase 10. It lives here because the reason is a
    property of how this module writes the file, not of how that one reads it.
    """
    arguments: list[str] = []
    if overlay_format == "webm_vp9_alpha":
        arguments += ["-c:v", VP9_ALPHA_DECODER]
    return [*arguments, "-i", str(overlay_path)]


__all__ = [
    "VP9_ALPHA_DECODER",
    "OverlayResult",
    "is_available",
    "overlay_input_arguments",
    "project_dir",
    "render_overlay",
    "write_composition",
]
