"""Black and frozen stretches of the *source*, found before they are edited in.

QA has always run ``blackdetect`` and ``freezedetect`` on the finished render
and reported what it found -- three warnings per video on a real session,
every one tracing back to seconds the recording itself spent black or
motionless. Reporting after the render is honesty; cutting before it is §36.
This module runs the exact same detectors, with the exact same thresholds,
over the recording once, and hands the guard the same span type the recorder
probe speaks -- so the machinery that already excises OBS chrome excises
these too.

The decode of a half-hour file costs minutes, so the verdict is cached next
to the project's assets, keyed by the file's size and mtime: a re-render
re-reads a JSON, not the video.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.analysis.frame_state import StateSpan
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import FrameState

logger = get_logger("analysis.source_dead", LogChannel.PIPELINE)

#: Padding around a detected run. blackdetect reports the fully-black span;
#: the fade into and out of it is half-dead too, and a cut placed exactly on
#: the first black frame still shows the dip.
_PAD_SECONDS = 0.3


def dead_source_spans(
    source: Path,
    *,
    ffmpeg: Any,
    config: Any,
    cache_dir: Path,
    media_id: str,
    duration_seconds: float | None,
) -> list[StateSpan]:
    """Black and frozen runs of ``source``, as guard-consumable spans.

    Black runs come back as ``TRANSITION`` (a black screen is what a
    transition shows) and frozen runs as ``PAUSE`` (a motionless frame is
    what a pause shows); the guard only asks whether a state can carry a
    highlight, and neither can. Failures are an empty list and a log line --
    the guard simply knows less, exactly as when the probe finds nothing.
    """
    try:
        stat = source.stat()
        signature = f"{stat.st_size}:{int(stat.st_mtime)}"
        cache = cache_dir / f"{media_id}.json"
        if cache.is_file():
            stored = json.loads(cache.read_text(encoding="utf-8"))
            if stored.get("signature") == signature:
                return _to_spans(stored)

        from backend.qa.technical import decode

        measured = decode(
            source, ffmpeg, config.qa, total_seconds=duration_seconds or 0.0
        )
        if not measured.decoded:
            logger.warning(
                "Source decode failed; black/frozen stretches stay unknown",
                extra={"media_id": media_id, "error": measured.error},
            )
            return []
        payload = {
            "signature": signature,
            "black": [[round(b, 3), round(e, 3)] for b, e in measured.black_runs],
            "freeze": [[round(b, 3), round(e, 3)] for b, e in measured.freeze_runs],
        }
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(cache)
        spans = _to_spans(payload)
        if spans:
            logger.info(
                "Dead source stretches measured",
                extra={
                    "media_id": media_id,
                    "black": len(payload["black"]),
                    "frozen": len(payload["freeze"]),
                },
            )
        return spans
    except Exception:
        logger.exception("Source dead-span probe unavailable")
        return []


def _to_spans(payload: dict) -> list[StateSpan]:
    spans: list[StateSpan] = []
    for begin, end in payload.get("black", []):
        spans.append(
            StateSpan(
                state=FrameState.TRANSITION,
                start_seconds=max(0.0, float(begin) - _PAD_SECONDS),
                end_seconds=float(end) + _PAD_SECONDS,
            )
        )
    for begin, end in payload.get("freeze", []):
        spans.append(
            StateSpan(
                state=FrameState.PAUSE,
                start_seconds=max(0.0, float(begin) - _PAD_SECONDS),
                end_seconds=float(end) + _PAD_SECONDS,
            )
        )
    return spans


__all__ = ["dead_source_spans"]
