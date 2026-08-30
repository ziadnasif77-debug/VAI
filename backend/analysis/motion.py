"""How much the picture moved between one sampled frame and the next.

The ``frames.motion_score`` column has existed since Phase 2 and has never
been written: seventeen thousand rows across this database, none with a
score. The Semantic Timeline weights motion at 0.35 -- the largest term in
the fusion -- so a third of every intensity value has been a constant since
the lane was built, and the percentile normaliser dutifully ranked that
constant at 0.5 everywhere. Nothing failed. The heat was simply flatter than
the session was.

Measured, not inferred: two consecutive sampled frames, both reduced to a
small greyscale grid, and the mean absolute difference between them. Reducing
first is what makes it a *motion* measure rather than a compression-noise
measure -- at full resolution, JPEG artefacts in a static menu score higher
than a slow camera pan.

The score is relative by construction and only ever used through the session's
own percentile ranking, so the absolute scale does not need to mean anything
beyond "more than" and "less than".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger

logger = get_logger("analysis.motion", LogChannel.PIPELINE)

#: The grid each frame is reduced to before differencing. Small enough that
#: encoder noise averages out, large enough that a figure crossing the frame
#: still moves several cells.
GRID: Final[tuple[int, int]] = (32, 18)


def score_pair(previous: Path, current: Path) -> float | None:
    """Mean absolute difference between two frames, 0..1, or ``None``.

    ``None`` where either image is missing or unreadable -- an absent
    measurement, which the caller stores as absent rather than as zero. Zero
    would say "nothing moved", and the lane would believe it.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the app
        return None
    try:
        with Image.open(previous) as first, Image.open(current) as second:
            a = first.convert("L").resize(GRID)
            b = second.convert("L").resize(GRID)
    except OSError:
        return None
    left = a.tobytes()
    right = b.tobytes()
    if len(left) != len(right) or not left:
        return None
    total = sum(abs(x - y) for x, y in zip(left, right, strict=True))
    return total / (len(left) * 255.0)


def score_media(database: Any, media_id: str) -> int:
    """Fill in every missing ``motion_score`` for one recording.

    Idempotent and self-healing: rows that already carry a score are left
    alone, so this costs nothing on a second run and repairs a corpus
    extracted before the column was ever written.

    The first sampled frame has no predecessor and no motion of its own; it
    takes the second frame's score rather than a zero, because "the recording
    began" is not "nothing was happening".
    """
    rows = database.fetch_all(
        "SELECT id, image_path, motion_score FROM frames "
        "WHERE media_id = ? ORDER BY timestamp",
        (media_id,),
    )
    if not rows:
        return 0
    pending = [row for row in rows if row["motion_score"] is None]
    if not pending:
        return 0

    scores: dict[str, float] = {}
    previous: Path | None = None
    for row in rows:
        current = Path(row["image_path"])
        if previous is not None:
            value = score_pair(previous, current)
            if value is not None:
                scores[row["id"]] = value
        previous = current

    if scores and rows[0]["id"] not in scores:
        # The opening frame borrows its neighbour's score (see above).
        second = next((row["id"] for row in rows[1:] if row["id"] in scores), None)
        if second is not None:
            scores[rows[0]["id"]] = scores[second]

    for frame_id, value in scores.items():
        database.execute(
            "UPDATE frames SET motion_score = ? WHERE id = ?", (round(value, 6), frame_id)
        )
    logger.info(
        "Measured frame motion",
        extra={"media_id": media_id, "scored": len(scores), "missing": len(pending)},
    )
    return len(scores)


__all__ = ["GRID", "score_media", "score_pair"]
