"""Sign in to YouTube, or see what the stored sign-in actually covers.

The application already does this from the Export screen, through three API
endpoints and a polling loop in the browser. This is the same device flow with
no server in front of it, and it exists for one reason:

    Re-authorising should not require starting the application, because
    starting the application starts the day's production.

That is a real collision. V2-P9 widened what a new grant asks for to include
`yt-analytics.readonly`, and a refresh token keeps the scopes it was *issued*
with -- so widening the request in code does not widen a grant already on disk.
Reading analytics therefore needs a fresh sign-in, and needing the whole
pipeline awake to do it would mean producing and publishing a video as a side
effect of granting a read permission.

    python scripts/youtube_auth.py              # what the stored grant covers
    python scripts/youtube_auth.py --connect    # sign in again, widening it
    python scripts/youtube_auth.py --disconnect # forget the stored grant

`--connect` prints a URL and a code. Nothing here ever sees a password: the
approval happens on Google's own page, in your browser, and this waits for
Google to say it was approved.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.publishing import build_token_provider, youtube_client
from backend.publishing.google_oauth import ANALYTICS_SCOPE, UPLOAD_SCOPE, DeviceFlow

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
        "--timeout", type=int, default=300, help="seconds to wait for approval"
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
        return _connect(config, paths, tokens, arguments.timeout)
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


def _connect(config, paths, tokens, timeout: int) -> int:
    """The device flow, in a terminal.

    Deliberately noisy about what is happening. A sign-in that prints a code
    and then goes quiet for two minutes is indistinguishable from one that
    has failed, and this one polls at the interval Google asks for.
    """
    client = youtube_client(config, paths.data_root)
    if client is None:
        print("No OAuth client configured.")
        return 2
    client_id, client_secret = client

    before = tokens.granted_scopes()
    flow = DeviceFlow(client_id=client_id, client_secret=client_secret)
    try:
        grant = flow.begin()
    except Exception as error:
        print(f"Google would not start the sign-in: {str(error)[:200]}")
        return 2

    public = grant.public()
    print("\n  Open this page:      " + str(public["verification_url"]))
    print("  Enter this code:     " + str(public["user_code"]))
    print(
        "\n  Approve BOTH permissions on the page -- the second one is the "
        "analytics\n  read that this whole step exists for."
    )
    print("\nWaiting for you to approve", end="", flush=True)

    deadline = time.monotonic() + max(30, timeout)
    interval = max(5, int(getattr(grant, "interval", 5) or 5))
    while time.monotonic() < deadline:
        time.sleep(interval)
        print(".", end="", flush=True)
        try:
            token = flow.poll(grant)
        except Exception as error:
            print(f"\n\nThe sign-in was refused or expired: {str(error)[:200]}")
            return 2
        if token is None:
            continue
        tokens.store.save(token)
        print("\n\nSigned in.\n")
        gained = sorted(tokens.granted_scopes() - before)
        for scope in gained:
            print(f"  gained  {MEANS.get(scope, scope)}")
        if not gained:
            print("  (the same scopes as before -- nothing was widened)")
        print("\nNow:  python scripts/fetch_outcomes.py")
        return 0

    print("\n\nGave up waiting. Nothing was changed; run it again when ready.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
