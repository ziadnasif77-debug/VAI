"""Identifier generation.

Identifiers are opaque strings with a type prefix, so a stray id in a log line,
an event record or an EDL is immediately attributable to an entity kind.
"""

from __future__ import annotations

import re
import uuid
from typing import Final

#: Entity prefixes for the §45 core entities. Adding an entity means adding it
#: here, not inventing a prefix at the call site.
PREFIXES: Final[dict[str, str]] = {
    "project": "proj",
    "media": "media",
    "media_track": "track",
    "job": "job",
    "scene": "scene",
    "frame": "frame",
    "audio_event": "aev",
    "transcript_segment": "seg",
    "game_event": "evt",
    "moment": "mom",
    "timeline_clip": "clip",
    "timeline_effect": "fx",
    "caption": "cap",
    "music": "mus",
    "render": "rnd",
    "qa_result": "qa",
}

_ID_RE = re.compile(r"^[a-z_]+-[0-9a-f]{12}$")


def new_id(entity: str) -> str:
    """Return a new identifier for ``entity``, e.g. ``evt-3f9a12c4b8d0``.

    Raises:
        KeyError: if ``entity`` has no registered prefix.
    """
    try:
        prefix = PREFIXES[entity]
    except KeyError as exc:
        raise KeyError(
            f"Unknown entity type {entity!r}. Register its prefix in backend/core/ids.py."
        ) from exc
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def is_valid_id(value: object) -> bool:
    """Return whether ``value`` has the shape produced by :func:`new_id`."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def sequence_id(prefix: str, index: int, *, width: int = 3) -> str:
    """Return a stable ordinal identifier such as ``evt-001``.

    Used for artefacts whose order is meaningful and which are regenerated
    deterministically, so re-running a stage on unchanged input produces
    byte-identical files and the cache stays valid (§48).
    """
    if index < 0:
        raise ValueError("Sequence index must be non-negative.")
    return f"{prefix}-{index:0{width}d}"


__all__ = ["PREFIXES", "is_valid_id", "new_id", "sequence_id"]
