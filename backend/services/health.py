"""Environment validation.

Backs the dashboard's system health panel (§58) and the startup check. A
missing GPU or model is reported as a warning, never a hard failure: §52
requires a CPU fallback and §95 requires graceful degradation, so the
application must start and report honestly on a machine with neither.

Every external command is invoked with an explicit argument list and
``shell=False`` (§85).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from backend.config.schema import AppConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import HardwareProfile, HealthStatus
from backend.core.models.health import HealthCheck, HealthReport
from backend.core.versions import APPLICATION_VERSION

logger = get_logger("services.health", LogChannel.APPLICATION)

#: Every probe is bounded: a hung binary must not hang the dashboard.
_COMMAND_TIMEOUT_SECONDS = 10
_HTTP_TIMEOUT_SECONDS = 3


class HealthService:
    """Runs the environment checks and aggregates them into a report."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def report(self) -> HealthReport:
        """Run every check and return the aggregate report."""
        gpu = self._check_gpu()
        checks: list[HealthCheck] = [
            self._check_python(),
            self._check_ffmpeg(),
            self._check_ffprobe(),
            self._check_sqlite(),
            self._check_node(),
            gpu,
            self._check_nvenc(gpu_present=gpu.status is HealthStatus.OK),
            self._check_speech_model(),
            self._check_scene_detector(),
            self._check_ollama(),
            self._check_ocr(),
        ]
        vram = _detected_vram_mb()
        return HealthReport(
            status=HealthReport.aggregate_status(checks),
            application_version=APPLICATION_VERSION,
            hardware_profile=self._config.hardware.select(vram),
            checks=checks,
        )

    def hardware_profile(self) -> HardwareProfile:
        """Return the profile the current machine resolves to (§53)."""
        return self._config.hardware.select(_detected_vram_mb())

    # -- individual checks ----------------------------------------------

    def _check_python(self) -> HealthCheck:
        version = ".".join(str(part) for part in sys.version_info[:3])
        supported = sys.version_info >= (3, 10)
        return HealthCheck(
            name="python",
            status=HealthStatus.OK if supported else HealthStatus.FAILED,
            required=True,
            detail=f"Python {version}",
            version=version,
            remediation=None if supported else "Install Python 3.10 or newer.",
        )

    def _check_ffmpeg(self) -> HealthCheck:
        return self._check_binary(
            name="ffmpeg",
            binary=self._config.ffmpeg.binary,
            required=True,
            remediation="Install FFmpeg and make sure it is on PATH.",
        )

    def _check_ffprobe(self) -> HealthCheck:
        return self._check_binary(
            name="ffprobe",
            binary=self._config.ffmpeg.ffprobe_binary,
            required=True,
            remediation="Install FFmpeg (ffprobe ships with it).",
        )

    def _check_node(self) -> HealthCheck:
        # Required only when Remotion is enabled, since Remotion renders the
        # overlay layer through Node.
        required = self._config.remotion.enabled
        return self._check_binary(
            name="node",
            binary="node",
            required=False,
            remediation=(
                "Install Node.js 20+ for the Remotion overlay pass."
                if required
                else "Install Node.js 20+ to enable Remotion overlays."
            ),
            version_args=["--version"],
        )

    def _check_sqlite(self) -> HealthCheck:
        import sqlite3

        return HealthCheck(
            name="sqlite",
            status=HealthStatus.OK,
            required=True,
            detail=f"SQLite {sqlite3.sqlite_version}",
            version=sqlite3.sqlite_version,
        )

    def _check_gpu(self) -> HealthCheck:
        """Detect an NVIDIA GPU. Absence is a warning: CPU fallback exists (§52)."""
        if not self._config.gpu.enabled:
            return HealthCheck(
                name="gpu",
                status=HealthStatus.SKIPPED,
                required=False,
                detail="GPU disabled in configuration.",
            )

        started = time.perf_counter()
        output = _run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ]
        )
        duration = time.perf_counter() - started

        if output is None:
            return HealthCheck(
                name="gpu",
                status=HealthStatus.WARNING,
                required=False,
                detail="No NVIDIA GPU detected. Analysis will run on CPU and be slower.",
                remediation="Install NVIDIA drivers to enable GPU acceleration.",
                duration_seconds=duration,
            )

        first = output.splitlines()[0]
        name, _, memory = first.partition(",")
        vram_mb = _parse_int(memory)
        usable = self._config.gpu.usable_vram_mb
        vision_vram = self._config.models.vision.estimated_vram_mb
        reserved = self._config.gpu.reserved_vram_mb
        headroom = (vram_mb - reserved) if vram_mb else 0
        tight = vram_mb is not None and vision_vram > headroom

        return HealthCheck(
            name="gpu",
            status=HealthStatus.WARNING if tight else HealthStatus.OK,
            required=False,
            detail=(
                f"{name.strip()} with {vram_mb} MB VRAM"
                + (
                    f"; the vision model needs ~{vision_vram} MB, which leaves no headroom. "
                    "Stages will run one model at a time."
                    if tight
                    else f" (budget {usable} MB usable)"
                )
            ),
            version=str(vram_mb) if vram_mb is not None else None,
            duration_seconds=duration,
        )

    def _check_nvenc(self, *, gpu_present: bool) -> HealthCheck:
        """Check for a usable NVENC encoder. Absence falls back to CPU x264 (§52).

        The encoder being compiled into FFmpeg is not enough: without an NVIDIA
        GPU it is listed and then fails at runtime. Reporting "available" in
        that case would be a lie the first render exposes.
        """
        preferred = self._config.render.encoder_preference
        hardware_encoders = [name for name in preferred if name.endswith("_nvenc")]
        if not hardware_encoders:
            return HealthCheck(
                name="nvenc",
                status=HealthStatus.SKIPPED,
                required=False,
                detail="No hardware encoder configured.",
            )

        output = _run([self._config.ffmpeg.binary, "-hide_banner", "-encoders"])
        if output is None:
            return HealthCheck(
                name="nvenc",
                status=HealthStatus.WARNING,
                required=False,
                detail="Cannot query FFmpeg encoders.",
                remediation="Verify the FFmpeg installation.",
            )

        available = [name for name in hardware_encoders if name in output]
        if available and gpu_present:
            return HealthCheck(
                name="nvenc",
                status=HealthStatus.OK,
                required=False,
                detail=f"Hardware encoding available: {', '.join(available)}.",
            )
        if available:
            fallback = next(
                (name for name in preferred if not name.endswith("_nvenc")), "libx264"
            )
            return HealthCheck(
                name="nvenc",
                status=HealthStatus.WARNING,
                required=False,
                detail=(
                    f"{', '.join(available)} is compiled into FFmpeg but no NVIDIA GPU was "
                    f"detected, so rendering will use {fallback}."
                ),
                remediation="Install NVIDIA drivers to use hardware encoding.",
            )

        fallback = next((name for name in preferred if not name.endswith("_nvenc")), "libx264")
        return HealthCheck(
            name="nvenc",
            status=HealthStatus.WARNING,
            required=False,
            detail=f"No NVENC encoder in this FFmpeg build; falling back to {fallback}.",
            remediation="Install an FFmpeg build with NVENC support for faster renders.",
        )

    def _check_speech_model(self) -> HealthCheck:
        """Check the speech backend is importable (Phase 3 dependency)."""
        provider = self._config.models.speech.provider
        module = {"faster_whisper": "faster_whisper", "whisperx": "whisperx"}.get(provider)
        if module is None:
            return HealthCheck(
                name="speech",
                status=HealthStatus.WARNING,
                required=False,
                detail=f"Unknown speech provider {provider!r}.",
                remediation="Set models.speech.provider to a supported backend.",
            )
        if importlib.util.find_spec(module) is None:
            return HealthCheck(
                name="speech",
                status=HealthStatus.WARNING,
                required=False,
                detail=f"{module} is not installed; transcription is unavailable.",
                remediation=f"pip install {module}",
            )
        return HealthCheck(
            name="speech",
            status=HealthStatus.OK,
            required=False,
            detail=f"{module} available ({self._config.models.speech.model}).",
            version=self._config.models.speech.version,
        )

    def _check_scene_detector(self) -> HealthCheck:
        """Check the scene detection backend (§17, Phase 4 dependency)."""
        if not self._config.analysis.vision.enabled:
            return HealthCheck(
                name="scenes",
                status=HealthStatus.SKIPPED,
                required=False,
                detail="Visual analysis disabled in configuration.",
            )
        if importlib.util.find_spec("scenedetect") is None:
            return HealthCheck(
                name="scenes",
                status=HealthStatus.WARNING,
                required=False,
                detail="PySceneDetect is not installed; shot boundaries are unavailable.",
                remediation="pip install scenedetect",
            )
        return HealthCheck(
            name="scenes",
            status=HealthStatus.OK,
            required=False,
            detail=f"PySceneDetect available ({self._config.analysis.scenes.detector} detector).",
        )

    def _check_ollama(self) -> HealthCheck:
        """Check the local model server and that the configured models exist."""
        vision = self._config.models.vision
        llm = self._config.models.llm
        if vision.provider != "ollama" and llm.provider != "ollama":
            return HealthCheck(
                name="ollama",
                status=HealthStatus.SKIPPED,
                required=False,
                detail="No Ollama-backed model configured.",
            )

        endpoint = vision.endpoint if vision.provider == "ollama" else llm.endpoint
        tags = _http_json(f"{endpoint.rstrip('/')}/api/tags")
        if tags is None:
            return HealthCheck(
                name="ollama",
                status=HealthStatus.WARNING,
                required=False,
                detail=f"Cannot reach Ollama at {endpoint}. Vision and LLM stages are unavailable.",
                remediation="Start Ollama, or point models.*.endpoint at a running instance.",
            )

        entries = tags.get("models", [])
        installed = {
            str(entry.get("name", "")) for entry in entries if isinstance(entry, dict)
        }
        wanted = {
            name
            for name, provider in ((vision.model, vision.provider), (llm.model, llm.provider))
            if provider == "ollama"
        }
        missing = sorted(name for name in wanted if name not in installed)
        if missing:
            return HealthCheck(
                name="ollama",
                status=HealthStatus.WARNING,
                required=False,
                detail=f"Ollama is running but these models are missing: {', '.join(missing)}.",
                remediation="ollama pull " + " && ollama pull ".join(missing),
            )
        return HealthCheck(
            name="ollama",
            status=HealthStatus.OK,
            required=False,
            detail=f"Ollama reachable with {len(installed)} model(s) installed.",
        )

    def _check_ocr(self) -> HealthCheck:
        """Check the OCR engine (Phase 5 dependency)."""
        engine = self._config.analysis.ocr.engine
        modules = {
            "paddleocr": "paddleocr",
            "easyocr": "easyocr",
            "tesseract": "pytesseract",
        }
        if not self._config.analysis.ocr.enabled:
            return HealthCheck(
                name="ocr",
                status=HealthStatus.SKIPPED,
                required=False,
                detail="OCR disabled in configuration.",
            )
        if engine == "auto":
            found = [name for name, module in modules.items() if importlib.util.find_spec(module)]
            if found:
                return HealthCheck(
                    name="ocr",
                    status=HealthStatus.OK,
                    required=False,
                    detail=f"OCR engines available: {', '.join(found)}.",
                )
            return HealthCheck(
                name="ocr",
                status=HealthStatus.WARNING,
                required=False,
                detail="No OCR engine installed; HUD text reading is unavailable.",
                remediation="pip install paddleocr",
            )

        module = modules.get(engine)
        if module is None or importlib.util.find_spec(module) is None:
            return HealthCheck(
                name="ocr",
                status=HealthStatus.WARNING,
                required=False,
                detail=f"OCR engine {engine!r} is not installed.",
                remediation=f"pip install {module or engine}",
            )
        return HealthCheck(
            name="ocr",
            status=HealthStatus.OK,
            required=False,
            detail=f"OCR engine {engine} available.",
        )

    # -- helper ---------------------------------------------------------

    def _check_binary(
        self,
        *,
        name: str,
        binary: str,
        required: bool,
        remediation: str,
        version_args: Sequence[str] = ("-version",),
    ) -> HealthCheck:
        started = time.perf_counter()
        resolved = shutil.which(binary)
        if resolved is None:
            return HealthCheck(
                name=name,
                status=HealthStatus.FAILED if required else HealthStatus.WARNING,
                required=required,
                detail=f"{binary} not found on PATH.",
                remediation=remediation,
                duration_seconds=time.perf_counter() - started,
            )

        output = _run([resolved, *version_args])
        duration = time.perf_counter() - started
        if output is None:
            return HealthCheck(
                name=name,
                status=HealthStatus.FAILED if required else HealthStatus.WARNING,
                required=required,
                detail=f"{binary} found at {resolved} but does not run.",
                remediation=remediation,
                duration_seconds=duration,
            )

        first_line = output.splitlines()[0].strip() if output.strip() else binary
        return HealthCheck(
            name=name,
            status=HealthStatus.OK,
            required=required,
            detail=first_line,
            version=_extract_version(first_line),
            duration_seconds=duration,
        )


def _run(command: Sequence[str]) -> str | None:
    """Run a command and return its combined output, or ``None`` on any failure.

    Explicit argument list, no shell (§85).
    """
    try:
        completed = subprocess.run(  # fixed argv, shell disabled (SPEC 85)
            list(command),
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 and not completed.stdout:
        return None
    return completed.stdout or completed.stderr


def _http_json(url: str) -> dict | None:
    """GET a JSON document from a local endpoint, or ``None`` if unreachable."""
    try:
        # Fixed http scheme, loopback endpoint from configuration.
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _detected_vram_mb() -> int | None:
    """Return total VRAM in MB, or ``None`` when no NVIDIA GPU is present."""
    output = _run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"]
    )
    if output is None:
        return None
    return _parse_int(output.splitlines()[0] if output.splitlines() else "")


def _parse_int(value: str) -> int | None:
    try:
        return int(float(value.strip()))
    except (TypeError, ValueError):
        return None


def _extract_version(line: str) -> str | None:
    """Pull a version token out of a ``--version`` line."""
    for token in line.split():
        stripped = token.lstrip("v")
        if stripped and stripped[0].isdigit():
            return stripped
    return None


#: Exported for tests that need to stub command execution.
run_command: Callable[[Sequence[str]], str | None] = _run

__all__ = ["HealthService"]
