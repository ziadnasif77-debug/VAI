"""Serving project files to the browser (SPEC §50, §57).

The Preview screen needs to play the finished MP4, and the Moments screen needs
thumbnails. Both mean handing a browser bytes from disk, which is the one place
in a local-first application where "local" stops being a simplification and
starts being a security property.

**Nothing outside the project directory is servable.** Every path is resolved
and checked to be *inside* the project's own directory before a byte is read.
A request is a string from the network even when the network is loopback, and
``../../../etc/passwd`` is a string.

**Range requests are answered properly.** A browser seeking in a twenty-minute
video sends `Range:` and expects `206 Partial Content`. Without it the player
downloads the whole file before it will scrub, which on a multi-gigabyte render
is the difference between an interface that works and one that appears frozen.
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from backend.api.dependencies import get_projects, get_state
from backend.core.logging import LogChannel, get_logger
from backend.database.repositories.renders import RenderRepository
from backend.services.project_manager import ProjectManager

logger = get_logger("api.files", LogChannel.APPLICATION)

router = APIRouter(tags=["files"])

#: Bytes per chunk when streaming a range. Large enough that a long seek is not
#: thousands of reads, small enough not to hold a render in memory (§7).
CHUNK_BYTES: Final[int] = 1 << 20

_RANGE_RE = re.compile(r"bytes=(?P<start>\d*)-(?P<end>\d*)")

#: Status codes as numbers. Starlette renamed several constants and
#: deprecated the old spellings; the numbers are the stable spelling.
_NOT_FOUND: Final[int] = 404
_FORBIDDEN: Final[int] = 403
_GONE: Final[int] = 410
_PARTIAL: Final[int] = 206
_RANGE_NOT_SATISFIABLE: Final[int] = 416


@router.get("/projects/{project_id}/preview")
def preview_render(
    project_id: str,
    request: Request,
    projects: ProjectManager = Depends(get_projects),
    state=Depends(get_state),
):
    """Stream the latest render, seekable (§57's Preview screen)."""
    projects.get(project_id)
    latest = RenderRepository(state.database).latest(project_id)
    if latest is None or not latest.get("output_path"):
        raise HTTPException(
            status_code=_NOT_FOUND,
            detail="This project has not been rendered yet.",
        )

    path = Path(str(latest["output_path"]))
    if not path.is_file():
        # The row says there is a file and there is not: worth a distinct
        # message, because "not rendered" and "the render was deleted" lead to
        # different actions.
        raise HTTPException(
            status_code=_GONE,
            detail="The rendered file is no longer on disk. Re-render the project.",
        )
    return _serve(path, request)


@router.get("/projects/{project_id}/files/{category}/{filename}")
def project_file(
    project_id: str,
    category: str,
    filename: str,
    request: Request,
    projects: ProjectManager = Depends(get_projects),
    state=Depends(get_state),
):
    """Serve one file from a project's own directory.

    ``category`` is a subdirectory of the project (§43): ``renders``,
    ``previews``, ``assets``, ``transcript``. The pair is resolved against the
    project root and checked to be inside it, so a crafted name cannot escape.
    """
    project = projects.get(project_id)
    root = Path(project.project_directory).resolve()
    candidate = (root / category / filename).resolve()

    if not _inside(candidate, root):
        logger.warning(
            "Refused a file request that pointed outside the project",
            extra={"project_id": project_id, "requested": f"{category}/{filename}"},
        )
        raise HTTPException(
            status_code=_FORBIDDEN,
            detail="Only files inside the project directory can be served.",
        )
    if not candidate.is_file():
        raise HTTPException(status_code=_NOT_FOUND, detail="No such file.")
    return _serve(candidate, request)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _inside(candidate: Path, root: Path) -> bool:
    """Whether ``candidate`` is within ``root`` after both are resolved.

    ``is_relative_to`` on resolved paths, rather than a string prefix: on
    Windows a prefix check also matches a sibling directory whose name merely
    starts the same way.
    """
    try:
        return candidate.is_relative_to(root)
    except (OSError, ValueError):  # pragma: no cover - unresolvable path
        return False


def _serve(path: Path, request: Request):
    """A whole file, or the range the browser asked for."""
    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    header = request.headers.get("range")

    if not header:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
        )

    span = _parse_range(header, size)
    if span is None:
        # §RFC 9110: an unsatisfiable range gets 416 and the real length, so the
        # client can ask again correctly rather than guess.
        raise HTTPException(
            status_code=_RANGE_NOT_SATISFIABLE,
            detail="Requested range is outside the file.",
            headers={"Content-Range": f"bytes */{size}"},
        )

    start, end = span
    return StreamingResponse(
        _read_range(path, start, end),
        status_code=_PARTIAL,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """The byte span a ``Range`` header asks for, or ``None`` if unsatisfiable.

    Only a single range is honoured. Multipart ranges are legal and no browser
    video player sends them, so answering the first one is honest and simple.
    """
    match = _RANGE_RE.search(header)
    if match is None or size == 0:
        return None

    raw_start, raw_end = match.group("start"), match.group("end")
    if not raw_start and not raw_end:
        return None
    if not raw_start:
        # `bytes=-500` means the last 500 bytes, not "up to byte 500".
        length = min(int(raw_end), size)
        return size - length, size - 1

    start = int(raw_start)
    end = int(raw_end) if raw_end else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return start, end


def _read_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    """Yield a byte span in chunks, never holding the file in memory (§7)."""
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            block = handle.read(min(CHUNK_BYTES, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


__all__ = ["CHUNK_BYTES", "router"]
