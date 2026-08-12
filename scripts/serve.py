"""Run the application: the interface, the API, and the worker.

    python scripts/serve.py

**One command, and everything the application needs is running.** The API
answers the interface; the worker consumes the job queue it fills; and the
built interface is served by the API itself, so there is one process and one
address. Two commands in two terminals is a reasonable thing to ask of a
developer and an unreasonable one to ask of someone who wants to edit a video.

If ``apps/web/dist`` does not exist this serves the API alone and says so.
Build it once with ``npm run build -w apps/web`` -- or run ``VAI.bat``, which
does that for you.

During development the interface is Vite's dev server on port 5173 instead,
because it reloads on save:

    npm run dev -w apps/web
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

try:
    import uvicorn
except ModuleNotFoundError as error:  # pragma: no cover - a setup problem
    # Almost always the wrong interpreter rather than a missing install: a
    # machine with several Pythons on PATH runs whichever is first, and the
    # first is rarely the one the dependencies went into. A raw traceback
    # sends someone to `pip install` when the fix is `py -3.11`.
    print()
    print(f"  This Python has no {error.name!r}:")
    print(f"      {sys.executable}")
    print(f"      Python {sys.version.split()[0]}")
    print()
    print("  You most likely have more than one Python installed and this is")
    print("  not the one the dependencies were installed into. Try:")
    print()
    print("      py -3.11 scripts/serve.py")
    print()
    print("  If that fails too, install the dependencies into this one:")
    print(f'      "{sys.executable}" -m pip install -e ".[dev]"')
    print()
    raise SystemExit(2) from None

from backend.api.app import create_app
from backend.api.dependencies import build_state
from backend.core.logging import LogChannel, get_logger
from backend.services.worker import JobWorker, recover_stale_jobs

logger = get_logger("serve", LogChannel.APPLICATION)


def _owns_port(host: str, port: int) -> bool:
    """Whether what is listening there is *this* application.

    Asked before stopping anything. Restarting our own server on every launch
    is what a person means by "start the app"; terminating whatever happens to
    hold port 8765 is a different and much worse thing to do on someone's
    machine.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    # The shape of our own health response, not merely "something answered".
    return isinstance(body, dict) and "checks" in body and "status" in body


def _pids_on_port(port: int) -> list[int]:
    """Process ids listening on ``port``, via netstat.

    netstat rather than psutil because this has to work when the reason the
    application will not start is a broken install.
    """
    import re
    import subprocess

    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    pids: list[int] = []
    for line in output.splitlines():
        if "LISTENING" not in line:
            continue
        match = re.search(rf":{port}\s", line)
        if not match:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pids.append(int(parts[-1]))
    return sorted(set(pids))


def _stop_existing(host: str, port: int) -> bool:
    """Stop the copy already running, so this launch can take over.

    Returns False when the port belongs to something that is not us, which is
    the case where the right answer is to say so and stop.
    """
    import os
    import signal
    import time

    if not _owns_port(host, port):
        print()
        print(f"  Port {port} on {host} is in use by something that is not this")
        print("  application, so it has been left alone.")
        print(f"  Use another port:   python scripts/serve.py --port {port + 1}")
        print()
        return False

    pids = _pids_on_port(port)
    if not pids:
        print()
        print(f"  Port {port} is in use but the process could not be identified.")
        print(f"  Use another port:   python scripts/serve.py --port {port + 1}")
        print()
        return False

    print(f"  Restarting  stopping the copy already running (pid {pids[0]})", flush=True)
    for pid in pids:
        with contextlib.suppress(OSError, PermissionError):
            os.kill(pid, signal.SIGTERM)

    # Wait for the socket to clear. A port does not free the instant its owner
    # dies, and starting into a half-closed socket fails in a way that reads
    # like the restart itself not working.
    for _ in range(40):
        if not _port_taken(host, port):
            return True
        time.sleep(0.25)

    print()
    print(f"  The copy on port {port} did not stop. Close its window and try again.")
    print()
    return False


def _port_taken(host: str, port: int) -> bool:
    """Whether something is already listening there."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="override the configured host")
    parser.add_argument("--port", type=int, default=None, help="override the configured port")
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="serve the API without running jobs (for debugging a stuck queue)",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="fail if the port is taken instead of restarting what is there",
    )
    arguments = parser.parse_args()

    state = build_state()
    config = state.config
    host = arguments.host or config.application.api.host
    port = arguments.port or config.application.api.port

    if _port_taken(host, port):
        # Checked *before* anything starts. Uvicorn's own failure is a
        # SystemExit raised from inside its event loop, which used to end this
        # process silently and take the worker with it -- the log read "worker
        # started ... API started ... worker stopped", which looks exactly like
        # a completed run. It cost a real analysis before anyone noticed.
        if arguments.keep_existing:
            print()
            print(f"  Port {port} on {host} is already in use.")
            print(f"  Try it:   http://{host}:{port}")
            print()
            state.close()
            return 2
        if not _stop_existing(host, port):
            state.close()
            return 2

    worker: JobWorker | None = None
    if not arguments.no_worker:
        # The worker recovers interrupted jobs itself, on its own thread,
        # before its first poll (§47). Doing it here instead would race the
        # worker for the job it had just claimed.
        worker = JobWorker(config, state.paths)
        worker.start()
    else:
        # Nothing will run, so recovery is this process's only chance to make
        # the queue's state true.
        recover_stale_jobs(state.database, config)

    from backend.api.app import INTERFACE_DIR

    built = (INTERFACE_DIR / "index.html").is_file()
    # `flush` on every line, and the address last so it is the final thing on
    # screen. Without the flush Python buffers stdout while the logging
    # handler writes to stderr unbuffered, so the banner arrives *after* a
    # wall of INFO lines -- or, if the process is still running, not at all.
    # The first version of this printed the address nowhere a person would see
    # it, which is indistinguishable from the application not starting.
    def say(line: str = "") -> None:
        print(line, flush=True)

    say()
    say(f"  API         http://{host}:{port}/api")
    say(f"  Docs        http://{host}:{port}/docs")
    say(f"  Data        {state.paths.data_root}")
    if worker is None:
        say("  Worker      not started (--no-worker): queued jobs will not run")
    say()
    if built:
        say("  " + "=" * 46)
        say(f"     OPEN THIS:   http://{host}:{port}")
        say("  " + "=" * 46)
    else:
        say("  The interface is not built, so this is the API only.")
        say("  Build it with:  npm run build -w apps/web")
        say("  (or use VAI.bat, which builds it for you)")
    say()
    say("  Leave this window open. Close it to stop the application.")
    say()

    app = create_app(state=state)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        if worker is not None:
            worker.stop()
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
