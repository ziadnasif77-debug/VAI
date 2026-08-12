r"""Start the whole application, under whichever Python can run it.

`VAI.bat` runs this file and nothing else. Batch proved to be the wrong tool
three separate times (LF line endings cmd.exe skips silently, `timeout`
refusing redirected stdin, buffered stdout hiding the address), so every piece
of logic lives here, where it can be tested.

Then a fourth failure taught this file its real lesson. On the machine this
was built for, "py -3.11" and "python" and every other *name* worked in every
environment that could be reconstructed — registry PATH, clean variables, the
lot — and still failed on a real double-click, reporting that no Python had
the dependencies. The environment a name resolves in is simply not knowable
from here. So:

* **Interpreters are found by absolute path first.** ``C:\Program
  Files\Python311\python.exe`` means the same thing in every environment;
  ``python`` does not. Names are the fallback, not the plan.
* **Every probe writes its evidence to .tmp/launch.log** — exit code, stderr,
  the PATH and PYTHON* variables it ran under. "It did not work" can never
  again arrive without the story attached.
* **As a last resort it repairs itself**: if an interpreter exists but lacks
  the dependencies, they are installed into it, live, on screen.
* A failure **holds the window open** until Enter, so the message survives.

This file must run under *any* Python, including one with nothing installed —
standard library only, no imports from the repository.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / ".tmp" / "launch.log"

#: The joint import every candidate must survive. Both names, because a
#: machine was met that had an interpreter with one and not the other.
PROBE = "import uvicorn, fastapi, sys; print(sys.version.split()[0], sys.executable)"

#: Interpreters known to this machine, by absolute path, best first.
KNOWN_EXES = (
    Path(r"C:\Program Files\Python311\python.exe"),
    Path(r"D:\nav\.pyruntime\python.exe"),
)

#: Name-based fallbacks, tried after every absolute path.
NAME_FALLBACKS: tuple[tuple[str, ...], ...] = (
    ("py", "-3.11"),
    ("py", "-3.12"),
    ("python",),
    ("py",),
    (sys.executable,),
)

_log = None


def say(line: str = "") -> None:
    """Print a line, and keep a copy on disk."""
    print(line, flush=True)
    note(line)


def note(line: str = "") -> None:
    """Log-only: evidence that would be noise on screen."""
    if _log is not None:
        with contextlib.suppress(OSError):
            _log.write(line + "\n")
            _log.flush()


def hold_window_open() -> None:
    """Keep the console up so a failure can actually be read."""
    if sys.stdin is not None and sys.stdin.isatty():
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("\n  Press Enter to close this window... ")


def snapshot_environment() -> None:
    """Record what this launch actually ran under.

    The one thing four rounds of guessing never had: the real environment of
    the failing run. Now every run carries its own.
    """
    note(f"launcher: Python {sys.version.split()[0]} at {sys.executable}")
    note(f"cwd: {Path.cwd()}")
    poison = {k: v for k, v in os.environ.items() if k.upper().startswith(("PYTHON", "PY_"))}
    note(f"PYTHON*/PY_* variables: {poison if poison else 'none'}")
    note("PATH:")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            note(f"  {entry}")
    note("")


def discover() -> list[tuple[str, ...]]:
    """Every interpreter worth trying, absolute paths first, deduplicated."""
    found: dict[str, tuple[str, ...]] = {}

    def add(command: tuple[str, ...]) -> None:
        key = " ".join(command).lower()
        found.setdefault(key, command)

    for exe in KNOWN_EXES:
        if exe.is_file():
            add((str(exe),))

    # Whatever the py launcher knows about, as absolute paths.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        listing = subprocess.run(
            ["py", "-0p"], capture_output=True, text=True, timeout=15
        ).stdout
        for match in re.finditer(r"[A-Za-z]:\\[^\r\n*]*python\.exe", listing or ""):
            path = Path(match.group(0).strip())
            if path.is_file():
                add((str(path),))

    # Registered installs, both hives (the py launcher's own sources).
    with contextlib.suppress(ImportError):
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            with contextlib.suppress(OSError), winreg.OpenKey(
                hive, r"Software\Python\PythonCore"
            ) as core:
                for index in range(64):
                    try:
                        version = winreg.EnumKey(core, index)
                    except OSError:
                        break
                    with contextlib.suppress(OSError), winreg.OpenKey(
                        core, rf"{version}\InstallPath"
                    ) as key:
                        install = Path(str(winreg.QueryValue(key, "")))
                        if (install / "python.exe").is_file():
                            add((str(install / "python.exe"),))

    # Common locations the registry can miss.
    local = Path(os.environ.get("LOCALAPPDATA", r"C:\Users") ) / "Programs" / "Python"
    for pattern_root, pattern in (
        (Path(r"C:\Program Files"), "Python3*/python.exe"),
        (Path("C:/"), "Python3*/python.exe"),
        (local, "Python3*/python.exe"),
    ):
        with contextlib.suppress(OSError):
            for exe in sorted(pattern_root.glob(pattern), reverse=True):
                add((str(exe),))

    for name in NAME_FALLBACKS:
        add(name)
    return list(found.values())


def probe(python: tuple[str, ...]) -> str | None:
    """Try the joint import; return a description on success, else ``None``.

    Every outcome is written to the log. This is the function whose silence
    cost four rounds of guessing.
    """
    label = " ".join(python)
    try:
        result = subprocess.run(
            [*python, "-c", PROBE], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        note(f"probe {label} -> timed out after 60s")
        return None
    except (OSError, subprocess.SubprocessError) as error:
        note(f"probe {label} -> {type(error).__name__}: {error}")
        return None
    if result.returncode == 0:
        detail = (result.stdout or "").strip()
        note(f"probe {label} -> OK ({detail})")
        return detail
    reason = (result.stderr or "").strip().splitlines()
    note(f"probe {label} -> exit {result.returncode}: {reason[-1][:160] if reason else '?'}")
    return None


def find_python() -> tuple[str, ...] | None:
    for candidate in discover():
        if probe(candidate) is not None:
            return candidate
    return None


def repair() -> tuple[str, ...] | None:
    """Install the dependencies into the best interpreter available.

    The last resort behind "it must work": an interpreter that exists but
    cannot import the dependencies gets them installed, on screen, and is then
    probed again. Returns the now-working interpreter, or ``None``.
    """
    for candidate in discover():
        try:
            check = subprocess.run(
                [*candidate, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if check.returncode != 0:
            continue

        say(f"  Installing  the dependencies into {' '.join(candidate)}")
        say("              (first time only -- a few minutes)")
        say()
        process = subprocess.Popen(
            [*candidate, "-m", "pip", "install", "-e", "."],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.stdout is not None:
            for line in process.stdout:
                say("  " + line.rstrip("\n"))
        if process.wait() == 0 and probe(candidate) is not None:
            return candidate
        return None
    return None


def build_interface() -> None:
    """Build the web interface when it is missing. Best effort."""
    if (ROOT / "apps" / "web" / "dist" / "index.html").is_file():
        return
    say("  Interface   building (first launch only, about a minute)...")
    try:
        result = subprocess.run(
            "npm run build -w apps/web",
            cwd=str(ROOT),
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        say("  Interface   could not be built; starting the API alone.")
        say("              To see why, run:  npm run build -w apps/web")


def server_environment(python: tuple[str, ...]) -> dict[str, str]:
    """The child's environment, defended.

    PYTHONHOME/PYTHONPATH-style variables can redirect an interpreter away
    from its own libraries, and the interpreter's directory is prepended to
    PATH so its DLLs win over same-named ones from other runtimes earlier on
    the PATH. Both are exactly the kind of machine-specific breakage that
    cannot be reproduced from anywhere else, so they are neutralised rather
    than diagnosed.
    """
    environment = dict(os.environ)
    for key in list(environment):
        upper = key.upper()
        if upper.startswith("PYTHON") and upper != "PYTHONIOENCODING":
            del environment[key]
    first = Path(python[0])
    if first.is_absolute() and first.is_file():
        environment["PATH"] = str(first.parent) + os.pathsep + environment.get("PATH", "")
    return environment


def run_server(python: tuple[str, ...]) -> int:
    """Run serve.py, mirroring everything it prints into the log."""
    command = [*python, str(ROOT / "scripts" / "serve.py"), "--open", *sys.argv[1:]]
    say(f"  Python      {' '.join(python)}")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=server_environment(python),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        say(f"  The server could not be started: {error}")
        return 1
    try:
        if process.stdout is not None:
            for line in process.stdout:
                say(line.rstrip("\n"))
        return process.wait()
    except KeyboardInterrupt:
        # Ctrl+C reaches the child through the shared console; give it a
        # moment to shut down cleanly rather than reporting a failure.
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            process.wait(timeout=10)
        return 0


def doctor() -> int:
    """Probe everything, print the table, start nothing."""
    say("  Interpreter probes:")
    say()
    for candidate in discover():
        detail = probe(candidate)
        verdict = f"OK   {detail}" if detail else "no"
        say(f"    {' '.join(candidate):55s} {verdict}")
    say()
    say(f"  Full detail in  {LOG_FILE}")
    return 0


def main() -> int:
    global _log
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    with contextlib.suppress(OSError):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _log = LOG_FILE.open("w", encoding="utf-8")

    say()
    say("  AI Gaming Video Editor")
    say("  ----------------------")
    say()
    snapshot_environment()

    if "--doctor" in sys.argv[1:]:
        return doctor()

    python = find_python()
    if python is None:
        say("  No interpreter could import the dependencies. Trying to repair...")
        say()
        python = repair()
    if python is None:
        say()
        say("  The application cannot start on this machine, and the repair")
        say("  attempt failed. Every probe and its error is recorded in:")
        say(f"      {LOG_FILE}")
        say()
        say("  Send that file, or run:  VAI.bat --doctor")
        hold_window_open()
        return 1

    build_interface()
    code = run_server(python)
    if code != 0:
        say()
        say("  The application stopped with an error - the lines above say why.")
        say(f"  A full copy of this launch is in  {LOG_FILE}")
        hold_window_open()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
