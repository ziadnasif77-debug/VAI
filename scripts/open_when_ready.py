"""Open the browser once the application is actually answering.

`VAI.bat` runs this alongside the server. A fixed delay before opening the
browser is wrong in both directions: too short on a cold start, where the
health checks probe the GPU, Ollama and the OCR engine before the first
request is served, and wasted seconds on a warm one. Worse, a browser that
opens early shows a connection error, and a connection error looks exactly
like the application having failed to start.

So this polls, and it gives up rather than hanging: if the server does not
answer, the console still has the address and whatever the server said about
why.

Nothing else imports this. It is deliberately dependency-free -- if the reason
the application will not start is a broken install, the thing that reports it
must not need that install to run.
"""

from __future__ import annotations

import socket
import sys
import time
import urllib.request
import webbrowser

HOST = "127.0.0.1"
PORT = 8765
#: Long enough for a cold start on a machine that has to wake a GPU, short
#: enough that a genuine failure is not mistaken for slowness.
TIMEOUT_SECONDS = 90.0
POLL_SECONDS = 0.4


def listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def answering(url: str) -> bool:
    """Whether the application responds, not merely whether the port is open.

    The page itself rather than ``/api/health``: health probes the GPU, the
    OCR engine and Ollama on its first call, which measured 23 seconds on a
    cold start here. Waiting for that would hold the browser closed long after
    the thing it opens was ready.
    """
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else PORT
    page = f"http://{host}:{port}/"

    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if listening(host, port) and answering(page):
            webbrowser.open(f"http://{host}:{port}")
            return 0
        time.sleep(POLL_SECONDS)

    # Silent on purpose: the server's own window is where a person is looking,
    # and a second process shouting into it would only obscure the message that
    # explains what actually went wrong.
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
