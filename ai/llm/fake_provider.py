"""A deterministic LLM for tests (SPEC §113–§119).

Every test that exercises the natural-language path needs a model, and the real
one is a 4 GB download that answers differently each time. Neither property is
wanted in a suite that has to run in twenty minutes and mean the same thing
twice.

So this returns scripted answers, keyed by what the prompt is *for*. It is not
a language model and does not pretend to be one: it exists to let the code
around the model be tested — the retry, the validation, the fallback when the
answer is refused, the wiring from a sentence to a stored change.

The failure modes are scriptable too, because those are the paths worth
testing. A model that always answers correctly would leave §94's reject-retry
and §95's degradation unexercised, and those are exactly the parts that decide
whether a bad answer reaches the user's video.
"""

from __future__ import annotations

from typing import Any, Final

from ai.providers.base import ModelInfo
from backend.core.errors import ErrorCode, ModelError, ValidationError

FAKE_VERSION: Final[str] = "fake-llm-1"


class FakeLLMProvider:
    """Scripted structured completions."""

    def __init__(
        self,
        *,
        responses: dict[str, dict[str, Any]] | None = None,
        default: dict[str, Any] | None = None,
        available: bool = True,
        fail_times: int = 0,
        invalid_times: int = 0,
    ) -> None:
        """
        Args:
            responses: answers by ``prompt_id``, e.g.
                ``{"interaction.instruction": {...}}``.
            default: returned for any prompt id not in ``responses``.
            available: report unavailable, to exercise the §95 path where the
                system carries on without a model.
            fail_times: raise :class:`ModelError` this many times before
                answering -- the "cannot reach the model" path.
            invalid_times: raise :class:`ValidationError` this many times
                before answering -- the "answer did not fit the schema" path
                that §94's retry exists for.
        """
        self._responses = dict(responses or {})
        self._default = default
        self._available = available
        self._fail_times = fail_times
        self._invalid_times = invalid_times
        self.load_count = 0
        self.unload_count = 0
        #: Every call, in order: ``(prompt_id, prompt)``. Tests assert on what
        #: the model was actually asked, which is where prompt bugs show up.
        self.calls: list[tuple[str, str]] = []

    # -- provider protocol ----------------------------------------------

    def info(self) -> ModelInfo:
        return ModelInfo(
            name="fake-llm",
            version=FAKE_VERSION,
            provider="fake",
            device="cpu",
            estimated_vram_mb=0,
        )

    def is_available(self) -> bool:
        return self._available

    def load(self) -> None:
        self.load_count += 1

    def unload(self) -> None:
        self.unload_count += 1

    # -- the one useful call --------------------------------------------

    def complete_json(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        prompt_id: str,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((prompt_id, prompt))

        if self._fail_times > 0:
            self._fail_times -= 1
            raise ModelError(
                "The fake model is pretending Ollama is unreachable.",
                code=ErrorCode.MODEL_UNAVAILABLE,
                details={"prompt_id": prompt_id},
            )
        if self._invalid_times > 0:
            self._invalid_times -= 1
            raise ValidationError(
                "The fake model is pretending to answer with prose.",
                code=ErrorCode.LLM_INVALID_JSON,
                details={"prompt_id": prompt_id},
            )

        answer = self._responses.get(prompt_id, self._default)
        if answer is None:
            raise ModelError(
                f"The fake model has no scripted answer for {prompt_id!r}.",
                code=ErrorCode.LLM_REQUEST_FAILED,
                details={"scripted": sorted(self._responses)},
            )

        missing = [key for key in schema.get("required", []) if key not in answer]
        if missing:
            # A scripted answer that does not fit its own schema is a test bug,
            # and saying so here beats a confusing failure downstream.
            raise ValidationError(
                f"The scripted answer for {prompt_id!r} is missing: {', '.join(missing)}.",
                code=ErrorCode.SCHEMA_VALIDATION_FAILED,
                details={"missing": missing},
            )
        return dict(answer)


__all__ = ["FAKE_VERSION", "FakeLLMProvider"]
