"""The recording-start probe: frames nobody sampled, read for recorder chrome.

Measured on a real recording (2026-08-28): the owner's video opened on the
OBS window itself -- the game alive only inside OBS's small preview -- and
every sampled detector was structurally blind to it. OCR's first candidate
frame landed at 11.2 s; the vision model's first sample at 4.0 s described
the *preview's* grass and mushroom as gameplay, because a game inside an
application window was not a distinction it had been told matters.

This probe is the deterministic backstop: extract a handful of frames from
the recording's opening at fixed offsets, OCR them, and match the words no
game writes -- OBS's own chrome, in the two languages this machine records
in. A hit at ``t`` marks a dead stretch around ``t``; adjacent stretches
merge. The result feeds the same :class:`StateSpan` stream the screen guard
and the thumbnail already honour, as :attr:`FrameState.DESKTOP`.

The probe covers the classic spot (openings); mid-recording alt-tabs are the
vision model's job, which v3 of its prompt teaches (a `desktop` label with
the inside-a-window rule).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

from backend.analysis.frame_state import StateSpan
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import FrameState

logger = get_logger("analysis.recorder_probe", LogChannel.PIPELINE)

#: Words the recorder writes on itself and no game writes on a frame.
#: Arabic first: this machine's OBS runs in Arabic.
RECORDER_WORDS: Final[re.Pattern[str]] = re.compile(
    "|".join(
        (
            r"\bOBS\b",
            "بدء البث",
            "إيقاف التسجيل",
            "بدء التسجيل",
            "المشاهد",
            "المصادر",
            "خالط الصوتيات",
            "طور الاستوديو",
            r"\bStart Streaming\b",
            r"\bStop Recording\b",
            r"\bStart Recording\b",
            r"\bScenes\b",
            r"\bSources\b",
            r"\bAudio Mixer\b",
            r"\bStudio Mode\b",
        )
    )
)

#: Where the probe looks, in seconds. Dense where recorders live, thinning
#: out; past half a minute the vision model's own samples carry the watch.
PROBE_OFFSETS: Final[tuple[float, ...]] = (0.5, 2.0, 4.0, 6.5, 9.0, 12.0, 16.0, 21.0, 27.0)

#: A hit at ``t`` condemns this much around it; merged when they touch.
_BACK_SECONDS: Final[float] = 2.5
_FORWARD_SECONDS: Final[float] = 3.5


def recorder_spans(
    source: Path,
    *,
    ffmpeg: Any,
    ocr: Any,
    scratch_dir: Path,
    offsets: tuple[float, ...] = PROBE_OFFSETS,
) -> list[StateSpan]:
    """Dead stretches where the recorder's own chrome is on screen.

    Never raises: a probe that cannot run protects nothing and says so in the
    log, because the guard and the thumbnail still have the vision spans.
    """
    if ocr is None:
        return []
    hits: list[float] = []
    try:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        for offset in offsets:
            frame = scratch_dir / f"probe-{offset:05.1f}.jpg"
            try:
                ffmpeg.run(
                    [
                        *ffmpeg.base_arguments(),
                        "-ss",
                        f"{offset:.2f}",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=960:-2",
                        str(frame),
                    ],
                    timeout_seconds=60,
                )
            except Exception:
                continue
            if not frame.is_file():
                continue
            try:
                detections = ocr.read(frame, min_confidence=0.3)
            except Exception:
                continue
            finally:
                frame.unlink(missing_ok=True)
            joined = " ".join(str(item.text) for item in detections)
            if RECORDER_WORDS.search(joined):
                hits.append(offset)
    except Exception:
        logger.exception("The recorder probe could not run; sampled detectors stand alone")
        return []

    if not hits:
        return []
    spans: list[StateSpan] = []
    for offset in hits:
        start = max(0.0, offset - _BACK_SECONDS)
        end = offset + _FORWARD_SECONDS
        if spans and start <= spans[-1].end_seconds:
            spans[-1] = StateSpan(
                FrameState.DESKTOP, spans[-1].start_seconds, max(spans[-1].end_seconds, end)
            )
        else:
            spans.append(StateSpan(FrameState.DESKTOP, start, end))
    logger.info(
        "Recorder chrome found at the opening",
        extra={"hits": len(hits), "dead_until": round(spans[-1].end_seconds, 1)},
    )
    return spans


__all__ = ["PROBE_OFFSETS", "RECORDER_WORDS", "recorder_spans"]
