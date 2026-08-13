"""Everything this application touches lives beside it.

The machine this runs on has a 238 GB system drive with 5.7 GB free and a 1.9 TB
data drive with 1.3 TB free. That is not a preference about tidiness; it is the
difference between the application working and the disk filling up mid-render.

So nothing here is left to whatever the machine happens to have on PATH or to
whatever a library picks as its cache. The rule is one line: **if the
application caused it to be written, it is written under the project root.**

What that covers, and why each one is here rather than assumed:

``PATH``
    ``tools/ffmpeg`` and ``tools/node`` go first. FFmpeg was resolving through
    a Chocolatey shim in ``C:\\ProgramData`` and node through ``C:\\Program
    Files``; both work, and both are on the drive that is full.

``TMP`` / ``TEMP``
    FFmpeg writes segment files, Whisper writes audio slices, pip writes wheels.
    The Windows default is under ``AppData\\Local\\Temp``.

``HF_HOME`` / ``TORCH_HOME``
    Whisper's weights are gigabytes and land in the user profile by default.

``PIP_CACHE_DIR`` / ``NPM_CONFIG_CACHE``
    Only touched by an install, but an install is exactly when a full disk
    stops the work.

``OLLAMA_MODELS``
    Only set if the machine has not already chosen; Ollama is a service this
    application talks to rather than owns, and overriding someone else's model
    directory would move models out from under another program.

This module deliberately has no imports beyond the standard library and no
knowledge of the application: the launcher runs it before anything else exists.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The project root: the directory holding `scripts/`.
ROOT = Path(__file__).resolve().parents[1]

#: Bundled binaries, preferred over whatever is on PATH.
TOOLS = ROOT / "tools"

#: Directories the application writes into. Created rather than assumed,
#: because a missing cache directory makes a library fall back to the default
#: -- which is the exact thing this module exists to prevent.
WRITES_INTO = (
    ROOT / ".tmp",
    ROOT / ".cache" / "pip",
    ROOT / ".cache" / "hf",
    ROOT / ".cache" / "torch",
    ROOT / ".cache" / "npm",
)


def tool_directories() -> list[Path]:
    """Bundled tool directories that exist, in the order they should be found."""
    return [path for path in (TOOLS / "ffmpeg", TOOLS / "node") if path.is_dir()]


def environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return ``base`` (the current environment by default), rooted at the project.

    Pure: it returns a new mapping rather than mutating ``os.environ``, so a
    caller can hand it to a subprocess without changing its own process.
    """
    env = dict(os.environ if base is None else base)

    ahead = os.pathsep.join(str(path) for path in tool_directories())
    if ahead:
        env["PATH"] = ahead + os.pathsep + env.get("PATH", "")

    env["TMP"] = env["TEMP"] = str(ROOT / ".tmp")
    env["HF_HOME"] = str(ROOT / ".cache" / "hf")
    env["TORCH_HOME"] = str(ROOT / ".cache" / "torch")
    env["PIP_CACHE_DIR"] = str(ROOT / ".cache" / "pip")
    env["NPM_CONFIG_CACHE"] = str(ROOT / ".cache" / "npm")
    # Remotion downloads a Chromium; the default is the user profile.
    env.setdefault("REMOTION_BROWSER_DOWNLOAD_DIR", str(ROOT / "remotion" / ".browser"))
    env.setdefault("PUPPETEER_CACHE_DIR", str(ROOT / "remotion" / ".browser"))
    # Someone else's models. Only fill the gap, never take it over.
    env.setdefault("OLLAMA_MODELS", str(Path("D:/Models")))
    return env


def prepare() -> dict[str, str]:
    """Create the directories and apply the environment to this process.

    Returns the environment, so a caller that also spawns children can pass the
    same mapping on rather than reading it back out of ``os.environ``.
    """
    for path in WRITES_INTO:
        path.mkdir(parents=True, exist_ok=True)
    env = environment()
    os.environ.update(env)
    return env


def python_executable() -> Path | None:
    """The project's own interpreter, if one has been created.

    A virtual environment inside the project keeps the packages -- torch alone
    is gigabytes -- on the same drive as everything else. Absent, the caller
    falls back to whatever interpreter it can find.
    """
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return candidate
    candidate = ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else None


def report() -> list[str]:
    """Human-readable lines describing where things will be read and written."""
    lines = []
    own = python_executable()
    lines.append(f"python   {own or 'not created (using a system interpreter)'}")
    for path in tool_directories():
        lines.append(f"tools    {path}")
    if not tool_directories():
        lines.append("tools    none bundled; PATH decides")
    lines.append(f"temp     {ROOT / '.tmp'}")
    lines.append(f"caches   {ROOT / '.cache'}")
    lines.append(f"models   {os.environ.get('OLLAMA_MODELS', 'D:/Models')}")
    return lines


__all__ = [
    "ROOT",
    "TOOLS",
    "WRITES_INTO",
    "environment",
    "prepare",
    "python_executable",
    "report",
    "tool_directories",
]
