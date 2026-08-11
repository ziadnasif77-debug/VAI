"""Local LLM providers for structured decisions (SPEC §13, §93, §95).

The factory maps ``models.llm.provider`` onto an implementation and never
substitutes one for another. A machine without the model degrades to the
rule-based path (§95): the interaction layer's parser already understands the
common instructions, and losing the model costs the unusual phrasings, not the
feature.
"""

from __future__ import annotations

from ai.llm.fake_provider import FakeLLMProvider
from ai.llm.ollama_provider import OllamaLLMProvider
from ai.providers.base import LLMProvider
from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, ModelError

#: Provider names accepted in ``config/models.yaml``.
LLM_PROVIDERS = ("ollama", "fake")


def create_llm_provider(config: AppConfig) -> LLMProvider:
    """Build the configured language-model provider.

    Raises:
        ModelError: the configured provider name has no implementation.
    """
    llm = config.models.llm
    if llm.provider == "ollama":
        return OllamaLLMProvider(llm, gpu=config.gpu)
    if llm.provider == "fake":
        return FakeLLMProvider()
    raise ModelError(
        f"Unknown LLM provider {llm.provider!r}.",
        code=ErrorCode.PROVIDER_NOT_REGISTERED,
        details={"provider": llm.provider, "supported": list(LLM_PROVIDERS)},
        recoverable=False,
    )


__all__ = [
    "LLM_PROVIDERS",
    "FakeLLMProvider",
    "OllamaLLMProvider",
    "create_llm_provider",
]
