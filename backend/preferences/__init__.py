"""Preferences (Phase F): what the editor has learned about the person using it.

Everything else in this pipeline starts each project from the same defaults.
That is right for a first project and wrong for a tenth: someone who has typed
"make it faster" into every project so far should not have to type it into the
eleventh.

    learn(database) -> Preferences -> as_delta -> applied over the preset

Read only. No table, no writer, no migration -- §4's intent log already keeps
every instruction with its words and its provenance, and this reads it across
projects instead of within one.
"""

from backend.preferences.learning import (
    LEARNABLE,
    LEARNABLE_LISTS,
    MIN_PROJECTS,
    as_delta,
    learn,
)
from backend.preferences.models import Preference, Preferences

__all__ = [
    "LEARNABLE",
    "LEARNABLE_LISTS",
    "MIN_PROJECTS",
    "Preference",
    "Preferences",
    "as_delta",
    "learn",
]
