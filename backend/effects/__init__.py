"""Effects engine (SPEC §68, §69, §70).

An independent module between the timeline and the renderers. It answers one
question -- *which effect belongs where* -- and answers it deterministically
from the moments that were selected and the editing intent that was resolved.

    moments (selected)  +  editing intent
                  │
            EffectPlanner            backend/effects
                  │
             EffectPlan
           ┌──────┴───────┐
           ▼              ▼
      FFmpeg pass    Remotion pass
   (footage itself)   (overlay layer)

Boundaries:

* The library is configuration (``config/effects.yaml``). Adding an effect is a
  config entry plus a renderer implementation -- the planner does not change.
* Effects are declarative data on the timeline. Nothing here calls a renderer,
  builds a filter graph or knows what FFmpeg is.
* No effect is ever applied globally (§69). Each one is earned by a specific
  moment and survives three budgets: its own rate limit, a per-moment cap, and
  a per-video density ceiling.
* Which renderer realises an effect is a property of the effect, not a decision
  the planner makes: effects that alter the gameplay footage are FFmpeg's,
  effects drawn on top of it are Remotion's.
"""

from backend.effects.library import EffectDefinition, EffectLibrary
from backend.effects.models import EffectCandidate, EffectInstance, EffectPlan
from backend.effects.planner import EffectPlanner, PlannedMoment

__all__ = [
    "EffectCandidate",
    "EffectDefinition",
    "EffectInstance",
    "EffectLibrary",
    "EffectPlan",
    "EffectPlanner",
    "PlannedMoment",
]
