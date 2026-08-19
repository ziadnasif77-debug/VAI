"""The Critic (Phase E): the first thing in this pipeline that reviews its own work.

Every judgement upstream is made from evidence about the *source*. The scorer
read events, the optimiser read durations, the Director read a list of moments.
None of them ever saw the assembled edit, which is the only object a viewer
ever sees -- and a defect that lives in the assembly (a clip that opens on a
loading screen, three identical clips in a row, a video that stops rather than
ends) is invisible to all of them by construction.

    evidence.gather -> service.review -> revision.apply

with the same rule the Director is held to at every step: name a clip that
exists, ask for something the timeline can already do, and lose the argument to
§39 when the two disagree.
"""

from backend.critic.evidence import ClipEvidence, EditEvidence, gather
from backend.critic.models import Action, Critique, CritiqueRejection, Note
from backend.critic.revision import Revision, apply
from backend.critic.service import review

__all__ = [
    "Action",
    "ClipEvidence",
    "Critique",
    "CritiqueRejection",
    "EditEvidence",
    "Note",
    "Revision",
    "apply",
    "gather",
    "review",
]
