"""Start the whole application, under whichever Python can run it.

`VAI.bat` runs this file and nothing else. Every previous version of that
batch file carried logic — find an interpreter, wait for the port, open the
browser — and batch proved to be the wrong tool three separate times: LF line
endings that cmd.exe skips silently, `timeout` refusing redirected stdin, and
buffered stdout hiding the one line that said where to go. Logic that cannot
be tested does not survive contact with a real machine, so all of it lives
here now, where it can be.

Two guarantees, because "nothing happened" must never be the report again:

* **Everything printed is also written to ``.tmp/launch.log``.** A console
  window that closes takes its story with it; the log survives the window.
* **A failure holds the window open** until Enter is pressed, so the message
  that explains it can actually be read.

This file must run under *any* Python, including one without the project's
dependencies — finding the right interpreter is its job. Standard library
only, and no imports from the repository.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / ".tmp" / "launch.log"

#: Interpreters to try, most likely first. ``py -3.11`` is where this
#: project's dependencies live on the machine it was built on. Plain
#: ``python`` is tried late: on a machine with several Pythons it is whichever
#: is first on PATH, and that one turned out to be a bare 3.14.
CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("py", "-3.11"),
    ("py", "-3.12"),
    ("py", "-3.10"),
    ("python",),
    ("py",),
    (sys.executable,),
)

_log = None


def say(line: str = "") -> None:
    """Print a line, and keep a copy on disk."""
    print(line, flush=True)
    if _log is not None:
        with contextlib.suppress(OSError):
            _log.write(line + "\n")
            _log.flush()


def hold_window_open() -> None:
    """Keep the console up so a failure can actually be read."""
    if sys.stdin is not None and sys.stdin.isatty():
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("\n  Press Enter to close this window... ")


def runnable(python: tuple[str, ...]) -> bool:
    """Whether this interpreter exists and has the dependencies."""
    try:
        probe = subprocess.run(
            [*python, "-c", "import uvicorn, fastapi"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def find_python() -> tuple[str, ...] | None:
    for candidate in CANDIDATES:
        if runnable(candidate):
            return candidate
    return None


def build_interface() -> None:
    """Build the web interface when it is missing. Best effort.

    A failed build is reported and the launch continues: the API alone is
    degraded, not broken, and the server prints its own message about it.
    """
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


def run_server(python: tuple[str, ...]) -> int:
    """Run serve.py, mirroring everything it prints into the log."""
    command = [*python, str(ROOT / "scripts" / "serve.py"), "--open", *sys.argv[1:]]
    say(f"  Python      {' '.join(python)}")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
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

    python = find_python()
    if python is None:
        say("  No Python on this machine has the dependencies installed,")
        say("  so the application cannot start.")
        say()
        say("  Install them with:")
        say('      py -3.11 -m pip install -e ".[dev]"')
        say()
        say(f"  A copy of this message is in  {LOG_FILE}")
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
