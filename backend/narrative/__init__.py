"""Narrative: turning ranked moments into a video (SPEC §35-§39).

* :mod:`~backend.narrative.optimizer` chooses which moments fill the requested
  duration. §39 is explicit that this is **an optimisation problem, not simple
  sorting**, and the module exists because a greedy top-N both misses the target
  and produces a monotonous video.
* :mod:`~backend.narrative.story` implements the three §35 modes. Story
  optimises narrative coherence over raw score (§36).
* :mod:`~backend.narrative.hook` picks the opening. §37: **the system must not
  invent narration** -- it selects a moment that exists.
* :mod:`~backend.narrative.pacing` orders for watchability and reports on the
  result rather than silently correcting it.

Nothing here touches video. The output is a plan; Phase 8 turns it into an EDL,
which is what keeps §127's re-edit cheap.
"""

from backend.narrative.hook import HookSelection, choose_hook
from backend.narrative.optimizer import OptimisationResult, optimise
from backend.narrative.pacing import PacingReport, intensity_of
from backend.narrative.story import NarrativePlan, build_plan

__all__ = [
    "HookSelection",
    "NarrativePlan",
    "OptimisationResult",
    "PacingReport",
    "build_plan",
    "choose_hook",
    "intensity_of",
    "optimise",
]
