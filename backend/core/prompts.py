"""Prompt loading and versioning (SPEC sections 92, 48, 49, 93).

§92 requires prompts to live under ``prompts/{vision,gaming,moments,narrative,
qa}/``, each carrying a version, a purpose, an input schema and an output
schema. This module is what makes that structure load-bearing rather than
decorative.

The version is not documentation. It participates in the cache key (§48), so
editing a prompt has to invalidate exactly the results that prompt produced —
no more, and no less. The failure mode this prevents is quiet: change a prompt,
forget the version, and every cached result from the old wording is served as
though the new one had produced it.

So the registry in :mod:`backend.core.versions` and the version on disk are
both required, and they must agree. Disagreeing is an error at load time, which
is early enough to be cheap.

The output schema lives beside the prompt because they change together: a
prompt that starts asking for a new field and a schema that does not accept it
is a §94 rejection on every call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from backend.config.paths import find_repository_root
from backend.core.errors import ConfigurationError, ErrorCode
from backend.core.versions import PROMPT_VERSIONS

#: Directory name at the repository root, per §92.
PROMPTS_DIRNAME: Final[str] = "prompts"

METADATA_FILENAME: Final[str] = "meta.json"
PROMPT_FILENAME: Final[str] = "prompt.md"


@dataclass(frozen=True, slots=True)
class Prompt:
    """One versioned prompt and the contract around it."""

    id: str
    version: int
    purpose: str
    text: str
    #: What the caller must supply. Documentation for the call site, and the
    #: place to look when a prompt starts needing something new.
    input_schema: dict[str, Any]
    #: What the model must return. Enforced before the result is used (§94),
    #: and handed to the runtime as a structured-output constraint where the
    #: runtime supports one (§93).
    output_schema: dict[str, Any]

    def render(self, **values: Any) -> str:
        """Substitute ``{placeholders}`` in the prompt text.

        Raises:
            ConfigurationError: a placeholder has no value. Sending a prompt
                with a literal ``{game}`` in it wastes a model call and
                produces an answer to a question nobody asked.
        """
        try:
            return self.text.format(**values)
        except KeyError as exc:
            raise ConfigurationError(
                f"Prompt {self.id!r} needs a value for {exc.args[0]!r}.",
                code=ErrorCode.CONFIG_INVALID,
                details={"prompt_id": self.id, "supplied": sorted(values)},
                recoverable=False,
            ) from exc


def prompts_root(root: Path | None = None) -> Path:
    """Return the ``prompts/`` directory."""
    return (Path(root) if root is not None else find_repository_root()) / PROMPTS_DIRNAME


@lru_cache(maxsize=64)
def load_prompt(prompt_id: str, root: Path | None = None) -> Prompt:
    """Load a registered prompt by id, e.g. ``"vision.frame_description"``.

    Cached: prompts are read once per process and never change under a running
    pipeline.

    Raises:
        ConfigurationError: the prompt is unregistered, missing, malformed, or
            its on-disk version disagrees with the registry.
    """
    if prompt_id not in PROMPT_VERSIONS:
        raise ConfigurationError(
            f"Prompt {prompt_id!r} is not registered. Add it to "
            "backend/core/versions.py:PROMPT_VERSIONS before use (§92).",
            code=ErrorCode.CONFIG_INVALID,
            details={"prompt_id": prompt_id, "registered": sorted(PROMPT_VERSIONS)},
            recoverable=False,
        )

    directory = prompts_root(root).joinpath(*prompt_id.split("."))
    metadata_path = directory / METADATA_FILENAME
    text_path = directory / PROMPT_FILENAME
    for path in (metadata_path, text_path):
        if not path.is_file():
            raise ConfigurationError(
                f"Prompt {prompt_id!r} is missing {path.name}.",
                code=ErrorCode.CONFIG_NOT_FOUND,
                details={"prompt_id": prompt_id, "expected": str(path)},
                recoverable=False,
            )

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Prompt {prompt_id!r} has malformed metadata: {exc}",
            code=ErrorCode.CONFIG_INVALID,
            details={"prompt_id": prompt_id, "path": str(metadata_path)},
            cause=exc,
            recoverable=False,
        ) from exc

    registered = PROMPT_VERSIONS[prompt_id]
    on_disk = metadata.get("version")
    if on_disk != registered:
        raise ConfigurationError(
            f"Prompt {prompt_id!r} is version {on_disk} on disk but {registered} in the "
            "registry. A prompt whose version does not move when its wording does "
            "silently serves stale cached results (§48, §92).",
            code=ErrorCode.CONFIG_INVALID,
            details={"prompt_id": prompt_id, "on_disk": on_disk, "registered": registered},
            recoverable=False,
        )

    return Prompt(
        id=prompt_id,
        version=registered,
        purpose=str(metadata.get("purpose", "")),
        text=text_path.read_text(encoding="utf-8").strip(),
        input_schema=dict(metadata.get("input_schema") or {}),
        output_schema=dict(metadata.get("output_schema") or {}),
    )


def available_prompts(root: Path | None = None) -> tuple[str, ...]:
    """Every registered prompt that is actually present on disk."""
    base = prompts_root(root)
    return tuple(
        sorted(
            prompt_id
            for prompt_id in PROMPT_VERSIONS
            if base.joinpath(*prompt_id.split("."), PROMPT_FILENAME).is_file()
        )
    )


def clear_prompt_cache() -> None:
    """Drop the cache. Used by tests that write prompts to a temporary root."""
    load_prompt.cache_clear()


__all__ = [
    "METADATA_FILENAME",
    "PROMPTS_DIRNAME",
    "PROMPT_FILENAME",
    "Prompt",
    "available_prompts",
    "clear_prompt_cache",
    "load_prompt",
    "prompts_root",
]
