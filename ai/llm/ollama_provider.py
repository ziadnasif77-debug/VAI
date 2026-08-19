"""Structured reasoning via a local Ollama model (SPEC §50, §54, §85, §93, §94).

The only LLM in this pipeline, and it arrives last on purpose. Everything a
model could have decided — which moments matter, how long the video is, what
order the clips go in — is decided by rules that can be read, tested and
argued with. The model's job is narrower and genuinely hard for rules: reading
a sentence a person typed.

Four constraints shape this file, and none is decoration:

**JSON, never prose (§93).** Ollama accepts a JSON schema as its ``format``
parameter, so the schema shipped beside the prompt (§92) is both the constraint
given to the runtime and the contract validated on the way back — one
definition, enforced at both ends.

**Reject, retry, then fail (§94).** A local model asked for JSON will
occasionally answer with something else. That never reaches a caller.

**No tools, no shell (§85).** This provider returns data. It cannot run a
command, read a file, or reach anything but the configured loopback endpoint.
What the application does with the data is the application's decision, made by
code that validated it first.

**Unloading is an HTTP call (§54).** Ollama keeps a model resident for minutes
after the last request, so on an 8 GB card the LLM is still holding memory when
the next stage wants the VLM. ``keep_alive: 0`` is what actually frees it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Final

from ai.providers.base import ModelInfo
from backend.config.schema import GpuConfig, LLMModelConfig
from backend.core.errors import ErrorCode, ModelError, ValidationError
from backend.core.logging import LogChannel, get_logger, log_duration

logger = get_logger("llm.ollama", LogChannel.AI)

#: Attempts before the failure is surfaced (§94). Two retries: a local model
#: that fails a schema twice will not succeed on the fifth, and every caller
#: has a §95 fallback to reach for.
MAX_ATTEMPTS: Final[int] = 3

#: How long Ollama keeps the model resident between requests.
KEEP_ALIVE_ACTIVE: Final[str] = "5m"

_HEALTH_TIMEOUT_SECONDS: Final[int] = 5


class OllamaLLMProvider:
    """Structured completions from a local model."""

    def __init__(self, config: LLMModelConfig, *, gpu: GpuConfig | None = None) -> None:
        self._config = config
        self._gpu = gpu
        self._loaded = False

    # -- provider protocol ----------------------------------------------

    def info(self) -> ModelInfo:
        return ModelInfo(
            name=self._config.model,
            version=self._config.version,
            provider="ollama",
            device="cuda" if self._gpu and self._gpu.enabled else "cpu",
            estimated_vram_mb=self._config.estimated_vram_mb,
        )

    def is_available(self) -> bool:
        """Whether the endpoint answers *and* has this model (§95).

        Both halves matter: a running Ollama without the model pulled fails at
        the first request, minutes into an interaction the user is waiting on.
        """
        tags = self._get("/api/tags", timeout=_HEALTH_TIMEOUT_SECONDS)
        if tags is None:
            return False
        available = {
            str(item.get("name", "")).split(":")[0]
            for item in tags.get("models", [])
            if isinstance(item, dict)
        }
        return self._config.model.split(":")[0] in available

    def load(self) -> None:
        """Ask Ollama to hold the model resident."""
        if self._loaded:
            return
        self._post(
            "/api/generate",
            {"model": self._config.model, "prompt": "", "keep_alive": KEEP_ALIVE_ACTIVE},
            timeout=self._config.timeout_seconds,
        )
        self._loaded = True
        logger.info("Loaded the language model", extra={"model": self._config.model})

    def unload(self) -> None:
        """Free the VRAM now rather than in five minutes (§54).

        Asked for unconditionally, and that is the point. This used to return
        early unless :meth:`load` had been called -- but Ollama loads a model to
        answer whether or not anyone asked it to, and every caller in this
        pipeline reaches for :meth:`complete_json` directly. Measured on a real
        project: the Critic answered in 23 s, called this, and left
        ``qwen2.5:7b-instruct`` resident with 4,528 MB of an 8 GB card still
        held -- through the render that was about to start.

        Never raises. It runs in the ``finally`` of every caller, and a
        transport error here would replace whatever they were actually doing
        with a failure to tidy up.
        """
        try:
            self._post(
                "/api/generate",
                {"model": self._config.model, "prompt": "", "keep_alive": 0},
                timeout=_HEALTH_TIMEOUT_SECONDS,
            )
        except ModelError as error:
            logger.warning(
                "Could not release the language model",
                extra={"model": self._config.model, "reason": str(error)[:160]},
            )
            return
        finally:
            self._loaded = False
        logger.info("Unloaded the language model", extra={"model": self._config.model})

    # -- the one useful call --------------------------------------------

    def complete_json(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        prompt_id: str,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Return a JSON object conforming to ``schema`` (§93, §94).

        Args:
            prompt: the rendered prompt text. Built from a versioned template
                by the caller, so what was asked is reproducible (§92).
            schema: the output schema. Given to the runtime *and* checked on
                return — a model can be told to produce a shape and still not.
            prompt_id: for provenance on whatever this result becomes (§49).
            temperature: overrides the configured default when a caller wants
                a more or less literal reading.

        Raises:
            ModelError: after the retries, or when Ollama cannot be reached.
        """
        payload = {
            "model": self._config.model,
            "prompt": prompt,
            "format": schema,
            "stream": False,
            "keep_alive": KEEP_ALIVE_ACTIVE,
            "options": {
                "temperature": (
                    temperature if temperature is not None else self._config.temperature
                ),
                "num_predict": self._config.max_output_tokens,
                # Configured since §13 and never sent, so every request has
                # run in Ollama's 4,096-token default. Text prompts fit, which
                # is why nothing failed -- but a forty-clip edit review or a
                # long moment list would cross it, and Ollama answers HTTP 400
                # rather than truncating. The vision provider found that out
                # the expensive way.
                "num_ctx": self._config.context_tokens,
            },
        }

        last_error: Exception | None = None
        with log_duration(logger, "Completed a structured request", prompt=prompt_id) as fields:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = self._post(
                        "/api/generate", payload, timeout=self._config.timeout_seconds
                    )
                    # Ollama loaded the model to answer this, whether or not
                    # anyone called `load` first, so from here on the card is
                    # holding it and `unload` has something to release.
                    self._loaded = True
                    parsed = _parse(response.get("response", ""), schema)
                    fields["attempts"] = attempt
                    return parsed
                except (ValidationError, ModelError) as error:
                    last_error = error
                    if attempt < MAX_ATTEMPTS:
                        logger.warning(
                            "The model's answer did not fit the schema; retrying",
                            extra={
                                "attempt": attempt,
                                "prompt": prompt_id,
                                "error": str(error),
                            },
                        )

        raise ModelError(
            f"The language model returned nothing usable after {MAX_ATTEMPTS} attempts.",
            code=ErrorCode.LLM_REQUEST_FAILED,
            details={"model": self._config.model, "prompt_id": prompt_id},
            cause=last_error,
        )

    # -- transport ------------------------------------------------------

    def _endpoint(self, path: str) -> str:
        return f"{self._config.endpoint.rstrip('/')}{path}"

    def _get(self, path: str, *, timeout: int) -> dict[str, Any] | None:
        request = urllib.request.Request(self._endpoint(path), method="GET")
        try:
            # A fixed scheme and a loopback endpoint from configuration (§85).
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def _post(self, path: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(
            self._endpoint(path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ModelError(
                f"Ollama rejected the request: HTTP {error.code}.",
                code=ErrorCode.LLM_REQUEST_FAILED,
                details={"endpoint": path, "model": self._config.model},
                cause=error,
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ModelError(
                f"Cannot reach Ollama at {self._config.endpoint}: {error}",
                code=ErrorCode.MODEL_UNAVAILABLE,
                details={"endpoint": self._config.endpoint},
                cause=error,
            ) from error
        except ValueError as error:
            raise ModelError(
                "Ollama returned a response that is not JSON.",
                code=ErrorCode.LLM_INVALID_JSON,
                details={"endpoint": path},
                cause=error,
            ) from error


def _parse(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Turn the model's answer into a checked object, or reject it (§94).

    The check here is deliberately shallow -- the object is a mapping and has
    the keys the schema requires. Full validation is the caller's Pydantic
    model, which knows what the values *mean*; duplicating that here would give
    two places for the rules to drift apart.
    """
    stripped = text.strip()
    if not stripped:
        raise ValidationError(
            "The model returned an empty response.",
            code=ErrorCode.LLM_INVALID_JSON,
        )
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise ValidationError(
            f"The model's response is not JSON: {error}",
            code=ErrorCode.LLM_INVALID_JSON,
            details={"response": stripped[:400]},
            cause=error,
        ) from error

    if not isinstance(parsed, dict):
        raise ValidationError(
            f"Expected a JSON object, got {type(parsed).__name__}.",
            code=ErrorCode.SCHEMA_VALIDATION_FAILED,
            details={"response": stripped[:400]},
        )

    missing = [key for key in schema.get("required", []) if key not in parsed]
    if missing:
        raise ValidationError(
            f"The model's answer is missing required field(s): {', '.join(missing)}.",
            code=ErrorCode.SCHEMA_VALIDATION_FAILED,
            details={"missing": missing, "received": sorted(parsed)},
        )
    return parsed


__all__ = ["KEEP_ALIVE_ACTIVE", "MAX_ATTEMPTS", "OllamaLLMProvider"]
