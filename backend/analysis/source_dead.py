"""Black and frozen stretches of the *source*, found before they are edited in.

QA has always run ``blackdetect`` and ``freezedetect`` on the finished render
and reported what it found -- three warnings per video on a real session,
every one tracing back to seconds the recording itself spent black or
motionless. Reporting after the render is honesty; cutting before it is §36.
This module runs the same detectors with the same thresholds over the
recording and hands the guard the span type the recorder probe speaks, so the
machinery that already excises OBS chrome excises these too.

Windowed, not sequential, for two measured reasons. A real OBS recording
carried a mid-file corruption: a straight decode consumed 832 seconds of
video, drained the audio to the end, and exited 0 -- every detector blind past
the damage, silently. Seeked windows land beyond a bad packet and keep
working, exactly like QA's own source cross-checks. And only the seconds the
plan is about to use need decoding at all: the guard asks about the planned
clips, this probes those windows, and an incremental cache keyed by the
file's signature remembers every window ever probed -- a re-render decodes
nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from backend.analysis.frame_state import StateSpan
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import FrameState

logger = get_logger("analysis.source_dead", LogChannel.PIPELINE)

#: Padding around a detected run. blackdetect reports the fully-black span;
#: the fade into and out of it is half-dead too, and a cut placed exactly on
#: the first black frame still shows the dip.
_PAD_SECONDS: Final[float] = 0.3
#: Extra source decoded either side of a requested window, so a run that
#: straddles the window's edge is still seen whole.
_WINDOW_PAD_SECONDS: Final[float] = 3.0
#: Pieces shorter than this are not worth a seek.
_MIN_PIECE_SECONDS: Final[float] = 2.0
#: Runs from adjacent windows closer than this merge into one.
_MERGE_GAP_SECONDS: Final[float] = 1.0


def dead_source_spans(
    source: Path,
    *,
    ffmpeg: Any,
    config: Any,
    cache_dir: Path,
    media_id: str,
    windows: list[tuple[float, float]],
    duration_seconds: float | None,
) -> list[StateSpan]:
    """Black and frozen runs inside ``windows``, as guard-consumable spans.

    Black comes back as ``TRANSITION`` (a black screen is what a transition
    shows) and frozen as ``PAUSE`` (a motionless frame is what a pause
    shows); the guard only asks whether a state can carry a highlight, and
    neither can. Failures are an empty answer and a log line -- the guard
    simply knows less, exactly as when the probe finds nothing.
    """
    if not windows:
        return []
    try:
        stat = source.stat()
        signature = f"{stat.st_size}:{int(stat.st_mtime)}"
        cache = cache_dir / f"{media_id}.json"
        stored = _load(cache, signature)

        ceiling = duration_seconds if duration_seconds else None
        padded = [
            (max(0.0, start - _WINDOW_PAD_SECONDS), end + _WINDOW_PAD_SECONDS)
            for start, end in windows
            if end > start
        ]
        if ceiling:
            padded = [(start, min(end, ceiling)) for start, end in padded]
        wanted = _merged(padded)
        missing = _difference(wanted, [tuple(w) for w in stored["probed"]])
        decoded_any = False
        for piece_start, piece_end in missing:
            if piece_end - piece_start < _MIN_PIECE_SECONDS:
                continue
            runs = _detect_window(
                source, ffmpeg, config, piece_start, piece_end - piece_start
            )
            if runs is None:
                logger.warning(
                    "A source window would not decode; its stretches stay unknown",
                    extra={"media_id": media_id, "window": [piece_start, piece_end]},
                )
                continue
            black, freeze = runs
            stored["black"] = _merged([*map(tuple, stored["black"]), *black])
            stored["freeze"] = _merged([*map(tuple, stored["freeze"]), *freeze])
            stored["probed"] = _merged([*map(tuple, stored["probed"]), (piece_start, piece_end)])
            decoded_any = True

        if decoded_any or not cache.is_file():
            cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "signature": signature,
                **{
                    k: [list(v) for v in stored[k]]
                    for k in ("probed", "black", "freeze")
                },
            }
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(cache)

        spans = [
            *_to_spans(stored["black"], FrameState.TRANSITION),
            *_to_spans(stored["freeze"], FrameState.PAUSE),
        ]
        if spans and decoded_any:
            logger.info(
                "Dead source stretches measured",
                extra={
                    "media_id": media_id,
                    "black": len(stored["black"]),
                    "frozen": len(stored["freeze"]),
                },
            )
        return spans
    except Exception:
        logger.exception("Source dead-span probe unavailable")
        return []


def _detect_window(
    source: Path, ffmpeg: Any, config: Any, start: float, seconds: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]] | None:
    """One seeked, video-only pass: (black runs, freeze runs), absolute times.

    ``None`` when the window would not decode. Timestamps inside a seeked
    decode start at zero, so every parsed run is offset by the seek.
    """
    from backend.qa.technical import _noise, _parse_black, _parse_freeze

    thresholds = config.qa.technical.thresholds
    argv = [
        *ffmpeg.base_arguments(loglevel="info"),
        *ffmpeg.input_arguments(source, start=start, duration=seconds),
        "-an",
        "-vf",
        (
            f"blackdetect=d=0.5:pix_th=0.10,"
            f"freezedetect=n={_noise(config.qa)}:"
            f"d={max(0.5, thresholds.max_frozen_run_seconds):.2f}"
        ),
        "-f",
        "null",
        "-",
    ]
    result = ffmpeg.run(argv, check=False)
    if not result.ok:
        return None
    black = [
        (start + begin, start + end) for begin, end in _parse_black(result.stderr)
    ]
    # freezedetect pairs are (start, DURATION); one still running at the
    # window's edge is closed against the window and merges with its other
    # half from the neighbouring window.
    freeze = [
        (start + begin, start + begin + length)
        for begin, length in _parse_freeze(result.stderr, seconds)
    ]
    return black, freeze


def _load(cache: Path, signature: str) -> dict:
    if cache.is_file():
        try:
            stored = json.loads(cache.read_text(encoding="utf-8"))
            if stored.get("signature") == signature and "probed" in stored:
                return stored
        except (OSError, ValueError):
            pass
    return {"signature": signature, "probed": [], "black": [], "freeze": []}


def _merged(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted((float(s), float(e)) for s, e in intervals):
        if merged and start <= merged[-1][1] + _MERGE_GAP_SECONDS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _difference(
    wanted: list[tuple[float, float]], probed: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """The parts of ``wanted`` not yet covered by ``probed``."""
    pieces = list(wanted)
    for taken_start, taken_end in probed:
        next_pieces: list[tuple[float, float]] = []
        for start, end in pieces:
            if taken_end <= start or taken_start >= end:
                next_pieces.append((start, end))
                continue
            if start < taken_start:
                next_pieces.append((start, taken_start))
            if taken_end < end:
                next_pieces.append((taken_end, end))
        pieces = next_pieces
    return pieces


def _to_spans(runs: list, state: FrameState) -> list[StateSpan]:
    return [
        StateSpan(
            state=state,
            start_seconds=max(0.0, float(begin) - _PAD_SECONDS),
            end_seconds=float(end) + _PAD_SECONDS,
        )
        for begin, end in runs
    ]


__all__ = ["dead_source_spans"]
