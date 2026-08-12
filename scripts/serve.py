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
        print()
        print(f"  Port {port} on {host} is already in use.")
        print("  Another copy of this application is probably already running.")
        print(f"  Try it first:   http://{host}:{port}")
        print(f"  Or use another port:   python scripts/serve.py --port {port + 1}")
        print()
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
    print()
    if built:
        print(f"  Open this   http://{host}:{port}")
    else:
        print("  Interface   not built. Run:  npm run build -w apps/web")
        print("              (or use VAI.bat, which builds it for you)")
    print(f"  API         http://{host}:{port}/api")
    print(f"  Docs        http://{host}:{port}/docs")
    print(f"  Data        {state.paths.data_root}")
    if worker is None:
        print("  Worker      not started (--no-worker): queued jobs will not run")
    print()

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
