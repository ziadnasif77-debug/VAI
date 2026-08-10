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
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.media.ffmpeg import CancelledError
from backend.rendering.composition import Composition

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
        logger.warning("Remotion is unavailable; continuing without an overlay",
                       extra={"reason": why})
        return OverlayResult(path=None, skipped=True, reason=why)

    directory = project_dir(config, repo_root)
    composition_path = write_composition(
        composition, directory / "public" / config.input_filename
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)

    argv = _render_command(config, composition_path, output_path, directory)
    started = time.perf_counter()
    with log_duration(
        logger,
        "Rendered the overlay",
        frames=composition.duration_in_frames,
        output=str(output_path),
    ) as fields:
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
        frames=composition.duration_in_frames,
        duration_seconds=time.perf_counter() - started,
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
        f"--concurrency={config.concurrency}",
        f"--props={composition_path}",
        # An overlay has no sound. Without this Remotion adds a silent Opus
        # track, which the composite would then have to know to ignore.
        "--muted",
        "--log=info",
        *extra,
    ]


def _node_executable() -> str:
    """The node binary, resolved rather than assumed to be on the child's PATH."""
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
        raise RenderError(
            f"Remotion exited with code {returncode}.",
            code=ErrorCode.REMOTION_FAILED,
            details={"returncode": returncode, "tail": tail[-15:]},
        )


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
