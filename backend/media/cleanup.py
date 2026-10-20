"""The pictures a finished video no longer needs (owner, 2026-10-20).

Every project keeps the frames its analysis sampled -- a base frame every three
seconds, the vision cascade's candidates, the scene keyframes -- and nothing
ever deleted them: 14,913 images across the projects on the development machine.
The owner asked that a video's pictures be removed once it is produced, so that
nothing accumulates.

"Produced" is CRITIC2 finishing: it is the last automatic stage, and the first
that reads the render instead of the recording, so nothing after it needs a
frame of the source. The render it may ask for again reads the recording, not
these pictures.

The frame rows stay in the database. They are the record of what was read and
when, and the reads themselves (OCR, vision, motion) are stored beside them;
only the image files go. A stage that would read the images again -- OCR and
vision on a re-analysis -- must not run over a folder that is now empty and
report an analysis of nothing, so the clearing leaves a marker, and
:func:`require_frames` turns it into a failure that says what to run first.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from backend.core.errors import AnalysisError, ErrorCode
from backend.core.logging import LogChannel, get_logger

logger = get_logger("media.cleanup", LogChannel.PIPELINE)

#: Written into the frames directory when its images were removed. Its presence
#: is what a later stage reads; the FRAMES stage removes it when it extracts
#: again.
CLEARED_MARKER: Final[str] = "CLEARED.json"

#: The render frames CRITIC2 described, under the project's analysis folder.
CRITIC2_FRAMES: Final[str] = "critic2-frames"


def production_image_dirs(paths: Any) -> list[Path]:
    """The folders whose pictures belong to one production."""
    return [Path(paths.frames), Path(paths.previews), Path(paths.analysis) / CRITIC2_FRAMES]


def clear_production_images(paths: Any) -> dict[str, Any]:
    """Delete a project's extracted pictures and say how much went.

    Returns ``{"files": n, "bytes": n, "folders": [...]}``. A folder that does
    not exist is not an error: a project whose vision stage was skipped never
    had candidates.
    """
    files = 0
    size = 0
    cleared: list[str] = []
    for folder in production_image_dirs(paths):
        if not folder.is_dir():
            continue
        for entry in folder.rglob("*"):
            if entry.is_file() and entry.name != CLEARED_MARKER:
                files += 1
                with contextlib.suppress(OSError):
                    size += entry.stat().st_size
        for child in folder.iterdir():
            if child.name == CLEARED_MARKER:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        cleared.append(str(folder))

    frames = Path(paths.frames)
    frames.mkdir(parents=True, exist_ok=True)
    summary = {
        "files": files,
        "bytes": size,
        "folders": cleared,
        "cleared_at": datetime.now(timezone.utc).isoformat(),
    }
    (frames / CLEARED_MARKER).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "Cleared the production's pictures",
        extra={"files": files, "megabytes": round(size / 2**20, 1)},
    )
    return summary


def frames_cleared(paths: Any) -> bool:
    """Whether this project's pictures were removed after production."""
    return (Path(paths.frames) / CLEARED_MARKER).is_file()


def mark_frames_present(paths: Any) -> None:
    """Forget a clearing: the FRAMES stage has extracted the pictures again."""
    (Path(paths.frames) / CLEARED_MARKER).unlink(missing_ok=True)


def require_frames(paths: Any, stage: str) -> None:
    """Fail by name when a stage would read pictures that were cleared.

    Silently reading an empty folder would complete the stage with nothing in
    it, and every stage after would build on that as if it were an analysis.
    """
    if not frames_cleared(paths):
        return
    raise AnalysisError(
        f"The {stage} stage needs this project's frames, and they were removed "
        "after the video was produced. Re-run the FRAMES stage first.",
        code=ErrorCode.FRAME_EXTRACTION_FAILED,
        details={"stage": stage, "marker": str(Path(paths.frames) / CLEARED_MARKER)},
        recoverable=False,
    )


__all__ = [
    "CLEARED_MARKER",
    "clear_production_images",
    "frames_cleared",
    "mark_frames_present",
    "production_image_dirs",
    "require_frames",
]
