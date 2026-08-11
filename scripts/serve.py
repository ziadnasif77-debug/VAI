"""Run the application: the API, and the worker that does the work.

    python scripts/serve.py

One command, because two would be one too many for a local tool. The API
answers the interface; the worker consumes the job queue it fills. Separating
them into two processes buys isolation nobody here needs — a render that
crashes the worker would take the API with it either way, since both are the
same application on one machine.

The web interface is a separate process during development, because Vite's dev
server does the reloading:

    npm run dev -w apps/web

A built interface (``npm run build -w apps/web``) is a folder of static files
this server hosts itself, so the finished product is one command again.
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

import uvicorn

from backend.api.app import create_app
from backend.api.dependencies import build_state
from backend.services.worker import JobWorker, recover_stale_jobs


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

    print(f"  API        http://{host}:{port}/api")
    print(f"  Docs       http://{host}:{port}/docs")
    print("  Interface  npm run dev -w apps/web   →  http://127.0.0.1:5173")
    print(f"  Data       {state.paths.data_root}")
    if worker is None:
        print("  Worker     not started (--no-worker): queued jobs will not run")
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
