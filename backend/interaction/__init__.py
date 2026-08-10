"""Interaction layer -- editing instructions, questions and commands.

Sits *above* the pipeline and is entirely optional. With no instructions the
default preset takes a recording to a finished video; instructions refine that
result rather than enabling it.

    USER
      ├── video ──────────────────────────► pipeline
      └── instructions / questions / commands
                    │
            interaction layer
                    │
            ┌───────┼────────┐
          intent   Q&A    commands
            │       │        │
            ▼       ▼        ▼
       editing   answers   EDL edits
        intent   (from      (no
           │      stored     re-analysis)
           ▼      data)
      core pipeline (STORY onward)

Boundaries this package keeps (§14, §22):

* Nothing in ``analysis``, ``gaming``, ``moments``, ``narrative``, ``timeline``,
  ``rendering`` or ``qa`` imports this package. The pipeline consumes a resolved
  :class:`~backend.interaction.models.EditingIntent` and knows nothing else.
* No chat logic lives inside a renderer, a model wrapper or FFmpeg code.
* Answering a question never re-runs analysis; changing the edit never
  invalidates it either.
* The product is a video editor with a control interface, not a chatbot.
"""

from typing import TYPE_CHECKING, Any

from backend.interaction.intent import IntentResolver
from backend.interaction.knowledge import VideoKnowledgeBase
from backend.interaction.models import (
    Answer,
    EditCommand,
    EditingIntent,
    IntentDelta,
    InteractionResult,
    InteractionType,
)
from backend.interaction.parser import classify, parse_command, parse_instruction
from backend.interaction.qa import QuestionAnswering

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from backend.interaction.service import InteractionService


def __getattr__(name: str) -> Any:
    """Load the service on first use (PEP 562).

    Importing it eagerly would mean that touching the *vocabulary* -- the
    ``EditingIntent`` the docstring above says the pipeline consumes -- loads
    the whole application layer with it. That closed a real cycle: the effects
    library reads this package's models, the timeline repository reads the
    effects models, and the service reads the timeline repository. It surfaced
    only from entry points that happened to import in the wrong order, which is
    the worst way for an import cycle to surface.

    Laziness here is not a workaround for that cycle; it is the package
    matching what it already claims about itself.
    """
    if name == "InteractionService":
        from backend.interaction.service import InteractionService

        return InteractionService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "Answer",
    "EditCommand",
    "EditingIntent",
    "IntentDelta",
    "IntentResolver",
    "InteractionResult",
    "InteractionService",
    "InteractionType",
    "QuestionAnswering",
    "VideoKnowledgeBase",
    "classify",
    "parse_command",
    "parse_instruction",
]
