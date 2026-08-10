"""Analysis cache keys (SPEC sections 48, 49, 127).

Section 48 defines the identity of a cached analysis result::

    video_hash + model_version + analysis_version

Section 127 makes the consequence a hard design requirement: after any edit the
system must regenerate the video **without re-analysing the source from
scratch**. That only holds if cache keys are exact -- too loose and a stale
result reaches the timeline, too strict and every re-edit pays for a full
re-analysis.

Keys are built here and nowhere else. The builder refuses to produce a key for
a model-backed namespace that has not declared its model version, and for a
prompt-backed namespace that has not declared its prompt version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from typing import Any, Final

from backend.core.versions import ANALYSIS_VERSION

KEY_DIGEST_LENGTH: Final[int] = 32


class CacheNamespace(str, Enum):
    """Cacheable analysis artefacts, following the §46 stage chain."""

    PROBE = "probe"
    PROXY = "proxy"
    AUDIO = "audio"
    FRAMES = "frames"
    TRANSCRIPT = "transcript"
    AUDIO_EVENTS = "audio_events"
    SCENES = "scenes"
    VISION = "vision"
    OCR = "ocr"
    HUD = "hud"
    GAME_EVENTS = "game_events"
    MOMENTS = "moments"
    SCORES = "scores"
    NARRATIVE = "narrative"
    EDL = "edl"
    RENDER = "render"


#: Namespaces produced by a model: ``model_version`` is mandatory (§49).
MODEL_BACKED_NAMESPACES: Final[frozenset[CacheNamespace]] = frozenset(
    {
        CacheNamespace.TRANSCRIPT,
        CacheNamespace.VISION,
        CacheNamespace.OCR,
        CacheNamespace.HUD,
        CacheNamespace.GAME_EVENTS,
        CacheNamespace.MOMENTS,
        CacheNamespace.SCORES,
        CacheNamespace.NARRATIVE,
    }
)

#: Namespaces produced by a prompted model call: ``prompt_version`` is also
#: mandatory, because editing a prompt changes the output (§92).
PROMPT_BACKED_NAMESPACES: Final[frozenset[CacheNamespace]] = frozenset(
    {
        CacheNamespace.VISION,
        CacheNamespace.GAME_EVENTS,
        CacheNamespace.MOMENTS,
        CacheNamespace.SCORES,
        CacheNamespace.NARRATIVE,
    }
)


def build_cache_key(
    namespace: CacheNamespace | str,
    *,
    video_hash: str | None = None,
    model_version: str | None = None,
    prompt_id: str | None = None,
    prompt_version: int | str | None = None,
    analysis_version: int = ANALYSIS_VERSION,
    chunk_index: int | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Return a deterministic cache key.

    Args:
        namespace: which artefact kind is being cached.
        video_hash: checksum of the source recording the result derives from.
        model_version: version of the model that produced the result. Required
            for :data:`MODEL_BACKED_NAMESPACES`.
        prompt_id, prompt_version: identify the prompt. Both required for
            :data:`PROMPT_BACKED_NAMESPACES`.
        analysis_version: defaults to the current analysis version.
        chunk_index: for chunked analysis (§7), so each chunk caches separately
            and a failure only invalidates its own chunk.
        params: any other input that changes the result -- sampling interval,
            scoring weights, game profile id, target duration. Serialised
            canonically, so key order and nesting do not affect the key.

    Returns:
        ``"<namespace>:<truncated sha256 hex>"``.

    Raises:
        ValueError: if a required version component is missing.
    """
    resolved = CacheNamespace(namespace)

    if resolved in MODEL_BACKED_NAMESPACES and not model_version:
        raise ValueError(
            f"Cache namespace {resolved.value!r} is model-backed: model_version is "
            "required so that swapping models invalidates the cache (§49)."
        )
    if resolved in PROMPT_BACKED_NAMESPACES and (prompt_id is None or prompt_version is None):
        raise ValueError(
            f"Cache namespace {resolved.value!r} is prompt-backed: prompt_id and "
            "prompt_version are required so that editing a prompt invalidates the "
            "cache (§92)."
        )

    components: dict[str, Any] = {
        "namespace": resolved.value,
        "video_hash": video_hash,
        "model_version": model_version,
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "analysis_version": analysis_version,
        "chunk_index": chunk_index,
        "params": _canonical(params or {}),
    }
    encoded = json.dumps(components, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:KEY_DIGEST_LENGTH]
    return f"{resolved.value}:{digest}"


def _canonical(value: Any) -> Any:
    """Normalise ``value`` so equivalent inputs hash identically."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, float):
        # Avoid float drift producing different keys for equal configurations.
        return round(value, 6)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


__all__ = [
    "KEY_DIGEST_LENGTH",
    "MODEL_BACKED_NAMESPACES",
    "PROMPT_BACKED_NAMESPACES",
    "CacheNamespace",
    "build_cache_key",
]
