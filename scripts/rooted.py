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
    Whisper's weights are gigabytes and land in the user profile by default --
    unless the machine has already moved them off the system drive, in which
    case they are left where they are. Re-rooting a cache that is already
    obeying the rule costs a multi-gigabyte re-download and buys nothing.

``PIP_CACHE_DIR`` / ``NPM_CONFIG_CACHE``
    Only touched by an install, but an install is exactly when a full disk
    stops the work.

``OLLAMA_MODELS``
    **Read, reported, never written.** Ollama is a shared runtime this
    application talks to rather than owns, and several projects on this
    machine use the same models — a 6 GB vision model held once serves all of
    them. Choosing its storage is not a consumer's decision to make.

    This module used to fill the variable in when it was empty, defaulting to
    ``D:/Models``. That default cost **36.4 GB**: the machine's own setting is
    ``F:\\Models``, a launch that did not inherit it wrote ``D:/Models``
    instead, and Ollama duly downloaded every model a second time. Both stores
    still held the same five models months later. An empty variable is the
    machine's business, and Ollama's own default is the right answer to it.

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


def shared_model_root() -> Path:
    """Where model weights live for **every** project on this machine.

    Model weights are shared infrastructure. One 4.4 GB Whisper checkpoint
    serves every project that transcribes; a project-local copy is those
    gigabytes again for nothing, which is why the same mistake with Ollama's
    store cost 36 GB across two drives.

    The application reads this from ``application.directories.models``, and so
    does this module — because two sources of truth for one directory is how
    the launcher and the application end up pointing at different stores. The
    environment override wins first (the loader honours it too), then the
    configured value, then ``models/`` inside the repository, which is what a
    fresh clone on an unknown machine should do.

    This is **not** the Ollama store and the two must never be confused:
    Ollama's lives wherever ``OLLAMA_MODELS`` says, and is never set here.
    This is the Hugging Face / faster-whisper side — the weights this
    application downloads for itself.
    """
    override = os.environ.get("VAI__APPLICATION__DIRECTORIES__MODELS", "").strip()
    for value in (override, _configured_models_dir()):
        if value:
            candidate = Path(value).expanduser()
            if candidate.is_absolute():
                return candidate
    return ROOT / "models"


def _configured_models_dir() -> str:
    """``application.directories.models`` from the YAML, read without a parser.

    This module deliberately imports nothing beyond the standard library —
    the launcher runs it before the virtual environment exists, so PyYAML is
    not available and cannot be. One key is worth a five-line scan; anything
    more would be a parser, and a parser belongs in the loader.
    """
    path = ROOT / "config" / "application.yaml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("directories:"):
            inside = True
            continue
        if inside:
            if stripped and not line.startswith((" ", "\t")):
                break  # left the block
            if stripped.startswith("models:"):
                return stripped.split(":", 1)[1].split("#", 1)[0].strip().strip("\"'")
    return ""


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
    env["PIP_CACHE_DIR"] = str(ROOT / ".cache" / "pip")
    env["NPM_CONFIG_CACHE"] = str(ROOT / ".cache" / "npm")
    # Model caches are gigabytes and slow to refill, so an existing setting is
    # kept when it already points off the system drive. This machine had
    # HF_HOME on D: before any of this existed; overriding it would have
    # re-downloaded Whisper's weights to say the same thing twice.
    #
    # The fallback is the *shared* model root rather than a folder inside this
    # repository. Weights are shared infrastructure: one 4.4 GB Whisper
    # checkpoint serves every project on the machine that transcribes, and a
    # project-local copy is those gigabytes again for nothing.
    _keep_or_root(env, "HF_HOME", shared_model_root())
    _keep_or_root(env, "TORCH_HOME", shared_model_root() / "torch")
    # Remotion downloads a Chromium; the default is the user profile.
    env.setdefault("REMOTION_BROWSER_DOWNLOAD_DIR", str(ROOT / "remotion" / ".browser"))
    env.setdefault("PUPPETEER_CACHE_DIR", str(ROOT / "remotion" / ".browser"))
    # Shared models, on a runtime this application does not own. Not even the
    # gap is ours to fill: see the note on OLLAMA_MODELS above.
    return env


def _keep_or_root(env: dict[str, str], name: str, fallback: Path) -> None:
    """Keep ``name`` if it already points off the system drive, else root it.

    The rule is "nothing lands on the system drive", not "everything lands
    here". A cache someone has already moved to another disk is already
    obeying it, and moving it again costs a re-download to no end.
    """
    current = env.get(name, "").strip()
    if current and not _on_system_drive(Path(current)):
        return
    env[name] = str(fallback)


def _on_system_drive(path: Path) -> bool:
    """Whether ``path`` sits on the drive Windows boots from."""
    system = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\")
    try:
        return path.resolve().drive.upper() == system.drive.upper()
    except (OSError, ValueError):
        return True  # unreadable: assume the worst and root it


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
    # Reported so a reader can see which shared store this machine is using,
    # and reported as unset when it is — an invented default here is what
    # duplicated 36 GB of models onto a second drive.
    lines.append(
        f"models   {os.environ.get('OLLAMA_MODELS') or 'unset (Ollama decides)'}  [shared]"
    )
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
