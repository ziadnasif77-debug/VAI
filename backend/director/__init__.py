"""The Director: an LLM proposes the shape of the story, code executes it.

§64's line, applied one layer up: **AI decides; ordinary code executes.** The
model here never chooses a second of footage. It reads what the pipeline
already found -- moments with types, scores, named events and times -- and
answers one question a scorer cannot: *what is this session about, and in what
order should it be told?*

Everything it returns is typed, validated, and checked against the evidence
before anything acts on it. A blueprint naming a moment that does not exist is
rejected rather than repaired, because a repaired hallucination is a
hallucination nobody can see.
"""

from backend.director.models import Blueprint, BlueprintRejection
from backend.director.service import build_blueprint

__all__ = ["Blueprint", "BlueprintRejection", "build_blueprint"]
