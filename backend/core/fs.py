"""Filesystem helpers shared by every layer.

Two concerns drive this module:

* **Windows is the target platform** (spec header) while development happens on
  Linux. Filenames are sanitised against the Windows rule set everywhere so a
  project created on Linux still opens on Windows.
* **Path safety** (section 56): user-supplied paths are resolved and checked
  against an allowed base directory before anything is read or written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

#: Characters Windows forbids in a path component, plus control characters.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Device names Windows reserves regardless of extension.
_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

MAX_FILENAME_LENGTH = 120


def sanitize_filename(name: str, *, fallback: str = "untitled") -> str:
    """Return a filesystem-safe single path component.

    Collapses whitespace, strips characters Windows rejects, avoids reserved
    device names, and trims trailing dots/spaces (which Windows silently drops).
    """
    if not isinstance(name, str):
        raise TypeError(f"Expected a string, got {type(name).__name__}.")

    normalised = unicodedata.normalize("NFKC", name)
    cleaned = _INVALID_FILENAME_CHARS.sub("_", normalised)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    if len(cleaned) > MAX_FILENAME_LENGTH:
        cleaned = cleaned[:MAX_FILENAME_LENGTH].rstrip(". ")

    if not cleaned or set(cleaned) <= {"_"}:
        return fallback

    stem = cleaned.split(".", 1)[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def slugify(value: str, *, fallback: str = "untitled", max_length: int = 60) -> str:
    """Return a lowercase ASCII slug suitable for directory names."""
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    slug = slug[:max_length].strip("-")
    return slug or fallback


def ensure_directory(path: Path) -> Path:
    """Create ``path`` (with parents) if needed and return it."""
    resolved = Path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def is_within(path: Path, base: Path) -> bool:
    """Return whether ``path`` resolves inside ``base``.

    Guards against ``..`` traversal and absolute paths supplied by a user or by
    an AI-produced document.
    """
    try:
        resolved_path = Path(path).resolve()
        resolved_base = Path(base).resolve()
    except OSError:  # pragma: no cover - unreadable path
        return False
    return resolved_path == resolved_base or resolved_base in resolved_path.parents


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    """Write ``content`` to ``path`` atomically.

    A crash mid-write must never leave a half-written ``project.json`` or EDL
    behind (section 51). The temporary file is created in the destination
    directory so the final rename stays on one filesystem.
    """
    destination = Path(path)
    ensure_directory(destination.parent)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding=encoding, newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return destination


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> Path:
    """Serialise ``payload`` as UTF-8 JSON and write it atomically."""
    text = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False, default=str)
    return atomic_write_text(path, text + "\n")


def read_json(path: Path) -> Any:
    """Read UTF-8 JSON from ``path``."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_checksum(path: Path, *, algorithm: str = "sha256", chunk_size: int = 1024 * 1024) -> str:
    """Return a hex digest of the file contents.

    Used to detect duplicate source files (section 12) and as the media
    component of every cache key (section 50). Reads in chunks so a 60-minute
    source does not have to fit in memory.
    """
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def directory_size_bytes(path: Path) -> int:
    """Return the total size of every regular file below ``path``."""
    total = 0
    for entry in Path(path).rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            total += entry.stat().st_size
    return total


__all__ = [
    "MAX_FILENAME_LENGTH",
    "atomic_write_json",
    "atomic_write_text",
    "directory_size_bytes",
    "ensure_directory",
    "file_checksum",
    "is_within",
    "read_json",
    "sanitize_filename",
    "slugify",
]
