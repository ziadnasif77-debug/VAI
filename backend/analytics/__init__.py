"""What happened after the video was published (V2-P9).

Outcome data, stored against the edit that produced it. No prediction, no
tuning, and no claim of learning: those need data this project does not have
yet, and P10 is the phase allowed to act on it when it does.
"""

from backend.analytics.projection import Projection, project
from backend.analytics.store import Outcome, OutcomeStore
from backend.analytics.youtube import RetentionPoint, Totals, YouTubeAnalytics

__all__ = [
    "Outcome",
    "OutcomeStore",
    "Projection",
    "RetentionPoint",
    "Totals",
    "YouTubeAnalytics",
    "project",
]
