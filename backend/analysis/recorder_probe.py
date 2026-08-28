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

#: Signature classes, matched against what this machine's OCR *actually*
#: emits on an OBS frame -- measured, not imagined. The first draft looked
#: for OBS's Arabic chrome and found nothing: the shipped EasyOCR carries no
#: Arabic, reads that chrome as mangled Latin, and scores the title-bar
#: "OBS 32.1.1" at 0.22. What it reads reliably (0.7-1.0) is the *status*
#: chrome: the FPS readout, CPU percentage, dB meters, "0 hidden",
#: "Options". Any one of those can appear in a game overlay, so a frame is
#: called a recorder only when **two different classes** agree.
RECORDER_SIGNATURES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"OBS[\s\d.]", re.IGNORECASE),
    re.compile(r"\d+\.\d+\s*/\s*\d+\.\d+\s*FPS", re.IGNORECASE),
    re.compile(r"CPU:?\s*\d", re.IGNORECASE),
    re.compile(r"-?\d+(?:\.\d+)?\s*dB\b"),
    re.compile(r"\b\d+\s*hidden\b", re.IGNORECASE),
    re.compile(r"\bOptions\b"),
    re.compile(r"\bStudio Mode\b|\bAudio Mixer\b|\bSources\b|\bScenes\b"),
    re.compile("بدء البث|إيقاف التسجيل|بدء التسجيل|خالط الصوتيات|طور الاستوديو"),
)

#: How many distinct signature classes make the verdict.
_REQUIRED_CLASSES: Final[int] = 2

#: Where the probe looks, in seconds. Dense where recorders live, thinning
#: out. Reaches a full minute because the owner alt-tabs back: OBS was on
#: screen again at 22 seconds of a real recording.
PROBE_OFFSETS: Final[tuple[float, ...]] = (
    0.5, 2.0, 4.0, 6.5, 9.0, 12.0, 15.0, 18.0, 21.0, 24.0, 28.0, 33.0, 40.0, 50.0, 60.0
)

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
                        # Native resolution on purpose: at 960 wide the
                        # recorder's status text OCRed to nothing (3
                        # detections, 0 classes); at source size, 27 and 6.
                        str(frame),
                    ],
                    timeout_seconds=60,
                )
            except Exception:
                continue
            if not frame.is_file():
                continue
            try:
                detections = ocr.read(frame, min_confidence=0.15)
            except Exception:
                continue
            finally:
                frame.unlink(missing_ok=True)
            joined = " ".join(str(item.text) for item in detections)
            classes = sum(1 for pattern in RECORDER_SIGNATURES if pattern.search(joined))
            if classes >= _REQUIRED_CLASSES:
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


__all__ = ["PROBE_OFFSETS", "RECORDER_SIGNATURES", "recorder_spans"]
