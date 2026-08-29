"""Local-machine services the interface cannot do for itself.

One endpoint so far, and it exists because of a browser rule meeting a
local-first design (§50, §51). A web page cannot learn the real path of a file
on disk -- its file input yields a sandboxed handle -- but this pipeline needs
the path, because it reads a multi-gigabyte recording in place many times and
copies nothing (§42). Pasting paths by hand was the workaround, and the first
real user asked for a picker in the first hour.

The way out is that the API is not a remote server. It runs on the same
machine, in the same desktop session, so it can open the operating system's
own file dialog and hand the chosen path back. The browser never touches the
filesystem; the person picks in a native window; §42 still copies nothing.

The dialog runs in a subprocess, not in this process: Tk and a threaded web
server disagree about who owns the main thread, and a crashed picker must not
take the API with it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from backend.api.dependencies import AppState, get_state
from backend.core.errors import ErrorCode, GamingEditorError
from backend.core.logging import LogChannel, get_logger

logger = get_logger("api.system", LogChannel.APPLICATION)

router = APIRouter(prefix="/system", tags=["system"])

#: One dialog at a time. A second click while one is open must not stack a
#: second window behind the first.
_dialog_lock = threading.Lock()

#: Formats the pipeline accepts, mirrored in the dialog's filter.
_RECORDING_PATTERNS = "*.mkv *.mp4 *.mov *.avi *.ts *.m2ts *.webm"

_PICKER_SCRIPT = """
import tkinter as tk
from tkinter import filedialog
import sys

root = tk.Tk()
root.withdraw()
# In front of the browser that asked for it, or the dialog looks like nothing
# happened -- which is this project's least favourite failure mode.
root.attributes("-topmost", True)
path = filedialog.askopenfilename(
    title="Choose a recording",
    initialdir=sys.argv[1] if len(sys.argv) > 1 else None,
    filetypes=[("Recordings", "{patterns}"), ("All files", "*.*")],
)
root.destroy()
sys.stdout.write(path or "")
""".replace("{patterns}", _RECORDING_PATTERNS)


class PickFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Where the dialog opens. The interface remembers the last directory a
    #: recording was chosen from; the server validates rather than trusts.
    initial_dir: str | None = Field(default=None, max_length=500)


class PickFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``None`` when the person cancelled, which is an answer, not an error.
    path: str | None


def _picker_home(initial_dir: str | None, roots: list[str]) -> str | None:
    """Where the dialog opens: inside the exclusive source, when one is set.

    The owner's rule pins imports to one folder; a picker that opens
    anywhere else invites choosing a path the service will refuse. A
    remembered directory is honoured only when it sits inside an allowed
    root; otherwise the first allowed root is the home. With no roots
    configured the remembered directory passes through untouched. The
    dialog itself can still browse -- the OS owns it; the ingestion
    service remains the wall.
    """
    if not roots:
        return initial_dir
    if initial_dir and Path(initial_dir).is_dir():
        resolved = os.path.normcase(str(Path(initial_dir).resolve()))
        for root in roots:
            allowed = os.path.normcase(str(Path(root).resolve()))
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return initial_dir
    for root in roots:
        if Path(root).is_dir():
            return str(Path(root))
    return None


def _pick_file(initial_dir: str | None) -> str | None:
    """Open the native dialog and wait for the person. Module-level so tests
    can substitute it -- a test suite that pops file dialogs cannot run."""
    directory = None
    if initial_dir:
        candidate = Path(initial_dir)
        if candidate.is_dir():
            directory = str(candidate)

    creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PICKER_SCRIPT, *([directory] if directory else [])],
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as error:
        raise GamingEditorError(
            f"The file dialog could not be opened: {error}",
            code=ErrorCode.FILE_PICKER_UNAVAILABLE,
            recoverable=True,
        ) from error

    if result.returncode != 0:
        # Headless session, missing Tk, remote shell: the dialog cannot exist
        # here. The interface falls back to the paste-a-path box, which is a
        # smaller product rather than a broken one (§95).
        raise GamingEditorError(
            "This machine cannot show a file dialog; paste the path instead.",
            code=ErrorCode.FILE_PICKER_UNAVAILABLE,
            details={"stderr": (result.stderr or "")[:200]},
            recoverable=True,
        )
    chosen = (result.stdout or "").strip()
    return chosen or None


@router.post("/pick-file", response_model=PickFileResponse)
def pick_file(
    request: PickFileRequest, state: AppState = Depends(get_state)
) -> PickFileResponse:
    """Open the operating system's file dialog and return what was chosen.

    Synchronous on purpose: the request lasts as long as the person is
    choosing, and FastAPI runs sync handlers on a worker thread, so the rest
    of the API keeps answering while the dialog is up.
    """
    if not _dialog_lock.acquire(blocking=False):
        raise GamingEditorError(
            "A file dialog is already open. Finish or cancel it first.",
            code=ErrorCode.FILE_PICKER_UNAVAILABLE,
            recoverable=True,
        )
    try:
        chosen = _pick_file(
            _picker_home(
                request.initial_dir,
                list(state.config.application.media_source_roots),
            )
        )
    finally:
        _dialog_lock.release()
    if chosen:
        logger.info("A recording was chosen in the native dialog")
    return PickFileResponse(path=chosen)


__all__ = ["router"]
