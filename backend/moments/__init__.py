"""Moments: candidate clips, ranked with their reasoning (SPEC §28-§34).

The pipeline's centre of gravity. Everything before this describes a recording;
everything after it edits one.

* :mod:`~backend.moments.formation` groups correlated events into story
  fragments (§28).
* :mod:`~backend.moments.context` gives each one an adaptive viewing span that
  snaps to real boundaries rather than a fixed roll (§29).
* :mod:`~backend.moments.dead_time` marks what could be cut, and what must not
  be (§30).
* :mod:`~backend.moments.repetition` keeps the strongest example rather than the
  first, and measures type saturation (§31, §33).
* :mod:`~backend.moments.scoring` produces a score with its working shown (§32).

§33 governs all of it: **the highest score is not necessarily the best clip.**
Nothing here selects anything -- it ranks candidates and explains them, and the
narrative stage decides what goes in the video.
"""

from backend.moments.context import ExpansionSources, expand
from backend.moments.dead_time import DeadSegment, dead_time_ratio, detect_dead_time
from backend.moments.formation import Moment, form_moments
from backend.moments.repetition import (
    detect_repetition,
    saturation_penalties,
    variety_report,
)
from backend.moments.scoring import DIMENSIONS, ScoringContext, score_moments

__all__ = [
    "DIMENSIONS",
    "DeadSegment",
    "ExpansionSources",
    "Moment",
    "ScoringContext",
    "dead_time_ratio",
    "detect_dead_time",
    "detect_repetition",
    "expand",
    "form_moments",
    "saturation_penalties",
    "score_moments",
    "variety_report",
]
