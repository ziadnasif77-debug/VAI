"""Version identifiers recorded with every analysis result.

SPEC sections 48 (caching), 49 (model versioning), 92 (prompt architecture).

Every AI-generated analysis stores ``model_name``, ``model_version``,
``prompt_version`` and ``analysis_version``. Together with the video hash these
decide whether an existing result can be reused (§48) and make a wrong result
traceable to the exact code, prompt and model that produced it (§49).
"""

from __future__ import annotations

from typing import Final

#: Semantic version of the application as a whole.
APPLICATION_VERSION: Final[str] = "0.1.0"

#: Bumped when analysis *logic* changes in a way that invalidates stored
#: results: sampling strategy, event correlation rules, scoring maths.
#: Participates in every cache key (§48).
ANALYSIS_VERSION: Final[int] = 1

#: Bumped for every database migration. Must equal the highest migration number
#: in backend/database/migrations.
SCHEMA_VERSION: Final[int] = 3

#: Version of the on-disk ``project.json`` manifest format (§43).
PROJECT_MANIFEST_VERSION: Final[int] = 1

#: Registry of production prompts (§92). Each prompt directory under
#: ``prompts/`` registers its id here; using an unregistered prompt raises,
#: because an unversioned prompt silently poisons the analysis cache.
#:
#: The id is the directory path under ``prompts/``, dotted. Bump the version
#: here *and* in the prompt's ``meta.json`` whenever the wording changes —
#: :func:`backend.core.prompts.load_prompt` refuses to load them if the two
#: disagree, which is how a forgotten bump is caught before it serves stale
#: results.
PROMPT_VERSIONS: Final[dict[str, int]] = {
    "vision.frame_description": 1,
    # The transcript is the only source that already contains the story in
    # words. Without this pass a 41-minute recording with 658 seconds of
    # speech produced two moment types in total.
    "analysis.narration": 1,
    # v2 of both: the model no longer sets the video's length. Ollama enforces
    # a schema as a grammar, so `minimum: 600` meant a model asked for "30
    # seconds" could not emit 30 -- it emitted 3000, and the person was told
    # their 30-second request had become a 50-minute video. Asked for "25
    # minutes" it produced 2500. Durations are arithmetic, which the rule
    # parser does exactly and this model does not; there was nothing to gain.
    "interaction.instruction": 2,
    # v3: trim, split and move. The timeline could do all three since Phase 8
    # and the chat could not ask for any of them.
    "interaction.command": 3,
    "interaction.question": 1,
    # The Director (Phase C): structure, never content. It is shown the
    # moments the pipeline found and answers with an order and a role for
    # each -- by index, so a name it invented cannot be matched back to
    # footage by guessing. A blueprint naming a moment that does not exist is
    # rejected rather than repaired.
    "narrative.blueprint": 1,
    "critique.edit_review": 1,
}


def prompt_version(prompt_id: str) -> int:
    """Return the registered version of ``prompt_id``.

    Raises:
        KeyError: if the prompt is not registered.
    """
    try:
        return PROMPT_VERSIONS[prompt_id]
    except KeyError as exc:
        raise KeyError(
            f"Unknown prompt id {prompt_id!r}. Register it in "
            "backend/core/versions.py:PROMPT_VERSIONS before use (§92)."
        ) from exc


def version_manifest() -> dict[str, object]:
    """Return the full version set stored on projects and analysis artefacts."""
    return {
        "application_version": APPLICATION_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "schema_version": SCHEMA_VERSION,
        "project_manifest_version": PROJECT_MANIFEST_VERSION,
        "prompt_versions": dict(PROMPT_VERSIONS),
    }
