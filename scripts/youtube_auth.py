"""Sign in to YouTube, or see what the stored sign-in actually covers.

The application signs in from the Export screen, through three API endpoints
and a polling loop in the browser. This exists beside that for two reasons, and
the second one was a surprise.

**Re-authorising should not require starting the application**, because
starting the application starts the day's production. Granting a read
permission would otherwise produce and publish a video as a side effect.

**And the flow the application uses cannot grant it.** V2-P9 widened what a new
grant asks for to include `yt-analytics.readonly` and shipped that, without
checking whether the device flow supports the scope. It does not: measured
against Google's own endpoint with this client id, `youtube` alone is accepted
and anything containing `yt-analytics.readonly` comes back `invalid_scope`. So
the change did not merely fail to gain the read permission -- it broke sign-in
altogether, because the same constant is what the device flow asks with. The
device flow now asks for what it can have, and this script uses the loopback
flow, which has no such restriction.

    python scripts/youtube_auth.py              # what the stored grant covers
    python scripts/youtube_auth.py --connect    # sign in again, widening it
    python scripts/youtube_auth.py --disconnect # forget the stored grant

`--connect` opens a browser. It has to: Google's device flow -- a code typed on
another screen, which is what this project used -- is restricted to a scope
list that excludes YouTube Analytics. Measured against Google's own endpoint
with this client, `yt-analytics.readonly` comes back `invalid_scope` there,
alone or in company.

Nothing here ever sees a password. The approval happens on Google's own page,
and the only thing that returns to this machine is a one-time code, on a local
port that answers exactly one request and then stops.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.publishing import build_token_provider, youtube_client
from backend.publishing.google_oauth import (
    ANALYTICS_SCOPE,
    UPLOAD_SCOPE,
    LoopbackFlow,
)

#: The local port the authorisation code comes back on.
#:
#: Fixed rather than free-chosen. A free port means a different consent address
#: every attempt, and an address that changes between attempts is one nobody
#: can come back to -- which is exactly how the first two attempts here were
#: lost. Loopback redirects accept any port for an installed-app client, so
#: this costs nothing and buys an address that stays true.
DEFAULT_PORT: int = 8971

#: What each scope buys, in the words of what it stops working without.
MEANS = {
    UPLOAD_SCOPE: "upload a video, set its thumbnail, add it to a playlist",
    ANALYTICS_SCOPE: "read this channel's own analytics (V2-P9)",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true", help="sign in again")
    parser.add_argument(
        "--disconnect", action="store_true", help="forget the stored grant"
    )
    parser.add_argument(
        "--timeout", type=int, default=600, help="seconds to wait for approval"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="the local port the code comes back on; fixed so the consent "
        "address is the same every attempt",
    )
    arguments = parser.parse_args()

    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    tokens = build_token_provider(config, paths.data_root)
    if tokens is None:
        print(
            "YouTube is not configured on this machine: no client id, or no "
            "secret file.\nSet publishing.youtube.client_id and "
            "client_secret_file first."
        )
        return 2

    if arguments.disconnect:
        tokens.store.clear()
        print("The stored grant was forgotten.")
        print(
            "Google still has the permission until you remove it at\n"
            "  https://myaccount.google.com/permissions"
        )
        return 0

    if arguments.connect:
        return _connect(config, paths, tokens, arguments.timeout, arguments.port)
    return _status(tokens)


def _status(tokens) -> int:
    """What the stored grant covers, and what it does not."""
    if not tokens.is_authorised():
        print("Not signed in.\n\n  python scripts/youtube_auth.py --connect")
        return 1

    granted = tokens.granted_scopes()
    print("Signed in.\n")
    for scope, means in MEANS.items():
        mark = "yes" if scope in granted else "NO "
        print(f"  [{mark}] {means}")
        print(f"        {scope}")
    extra = sorted(granted - set(MEANS))
    for scope in extra:
        print(f"  [yes] {scope}")

    if ANALYTICS_SCOPE not in granted:
        print(
            "\nThis grant predates the analytics scope. Uploading is "
            "unaffected;\nnothing can be read until it is granted:\n"
            "\n  python scripts/youtube_auth.py --connect\n"
            "\nSigning in again replaces the stored grant with a wider one. "
            "The old\none keeps working until it does."
        )
        return 1
    print("\nEverything this project asks for is granted.")
    return 0


def _connect(config, paths, tokens, timeout: int, port: int = DEFAULT_PORT) -> int:
    """Sign in through the browser, because analytics cannot come any other way.

    The device flow -- a code typed on another screen -- is what this project
    used, and Google restricts it to a scope list that excludes YouTube
    Analytics. Measured against Google's own endpoint with this client:
    `yt-analytics.readonly` comes back `invalid_scope` there, alone or in
    company. A read permission is not obtainable that way however the request
    is phrased, so this opens a browser instead.

    Nothing here sees a password. The approval happens on Google's own page,
    and the only thing that returns to this machine is a one-time code, on a
    local port that answers exactly one request and then stops.
    """
    client = youtube_client(config, paths.data_root)
    if client is None:
        print("No OAuth client configured.")
        return 2
    client_id, client_secret = client

    before = tokens.granted_scopes()
    flow = LoopbackFlow(client_id=client_id, client_secret=client_secret)

    print("\nA browser tab is opening for you to approve two permissions:\n")
    for means in MEANS.values():
        print(f"  - {means}")

    def show(url: str) -> None:
        # Printed before the wait, and flushed. The first version printed it
        # afterwards, which is exactly when it stops being any use: a person
        # whose browser did not open sat looking at nothing for fifteen
        # minutes and was then handed the address they had needed at the start.
        print("\nIf no tab opened, open this by hand:\n", flush=True)
        print(f"  {url}\n", flush=True)
        print(f"Waiting up to {max(30, timeout)}s.", flush=True)

    try:
        token = flow.authorise(
            timeout_seconds=max(30, timeout), on_url=show, port=port
        )
    except Exception as error:
        print(f"The sign-in did not complete: {str(error)[:220]}")
        if getattr(flow, "url", ""):
            print(f"\nOpen this by hand and try again:\n  {flow.url}")
        return 2

    tokens.store.save(token)
    print("Signed in.\n")
    gained = sorted(tokens.granted_scopes() - before)
    for scope in gained:
        print(f"  gained  {MEANS.get(scope, scope)}")
    if not gained:
        print("  (the same scopes as before -- nothing was widened)")
    print("\nNow:  python scripts/fetch_outcomes.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
