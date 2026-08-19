"""Frame understanding via a local Ollama VLM (SPEC §15, §50, §54, §93, §94).

The model runs on the user's machine (§50) and returns JSON, never prose (§93).
Ollama accepts a JSON schema as its ``format`` parameter, so the schema that
ships beside the prompt (§92) is both the constraint given to the runtime and
the contract validated on the way back — one definition, enforced twice.

Two details here are not incidental:

* **Unloading is an HTTP call, not a dropped reference.** Ollama keeps a model
  resident for five minutes after the last request. On an 8 GB card that means
  the vision model is still holding 7 GB when the next stage tries to load, and
  §54's "one model at a time" quietly stops being true. A request with
  ``keep_alive: 0`` is what actually frees it.
* **An invalid response is rejected, retried, and only then surfaced** (§94).
  A local model asked for JSON will occasionally produce something else; the
  pipeline must never carry that forward.

Only candidate keyframes reach this provider. The cascade in
:mod:`backend.analysis.candidates` decides which, and the ceiling it enforces
is what keeps analysis time predictable (§15, §16).
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Final

from ai.providers.base import ModelInfo, VisionObservation
from backend.config.schema import GpuConfig, VisionModelConfig
from backend.core.errors import ErrorCode, ModelError, ValidationError
from backend.core.logging import LogChannel, get_logger, log_duration
from backend.core.prompts import load_prompt

logger = get_logger("vision.ollama", LogChannel.AI)

#: Attempts per batch before the failure is surfaced (§94: reject → retry →
#: fallback). Two retries, because a local model that fails a schema twice is
#: not going to succeed on the fifth try and the caller has a §95 fallback.
MAX_ATTEMPTS: Final[int] = 3

#: How long Ollama keeps the model resident after a request. Zero on the
#: unload call; the default otherwise, so the frames of one stage do not pay a
#: reload each.
KEEP_ALIVE_ACTIVE: Final[str] = "5m"

_HEALTH_TIMEOUT_SECONDS: Final[int] = 5


class OllamaVisionProvider:
    """Vision analysis backed by a local Ollama model."""

    def __init__(
        self,
        config: VisionModelConfig,
        *,
        gpu: GpuConfig | None = None,
        game: str = "an unidentified game",
    ) -> None:
        self._config = config
        self._gpu = gpu
        self._game = game
        self._loaded = False

    # -- identity -------------------------------------------------------

    def info(self) -> ModelInfo:
        """Provenance stored with every observation (§49)."""
        return ModelInfo(
            name=self._config.model,
            version=self._config.version,
            provider=self._config.provider,
            quantization=getattr(self._config, "quantization", None),
            device="cuda" if self._gpu and self._gpu.enabled else "cpu",
            estimated_vram_mb=self._config.estimated_vram_mb,
        )

    def is_available(self) -> bool:
        """Whether Ollama is reachable *and* has the configured model.

        Both halves matter: a running server without the model produces a
        confusing mid-stage failure, and §95 wants the absence known before the
        stage starts.
        """
        tags = self._get("/api/tags", timeout=_HEALTH_TIMEOUT_SECONDS)
        if tags is None:
            return False
        installed = {
            str(entry.get("name", ""))
            for entry in tags.get("models", [])
            if isinstance(entry, dict)
        }
        if self._config.model in installed:
            return True
        # Ollama reports "qwen2.5vl:7b"; a config that omits the tag still means
        # the same model.
        base = self._config.model.split(":")[0]
        return any(name.split(":")[0] == base for name in installed)

    # -- lifecycle ------------------------------------------------------

    def load(self) -> None:
        """Ask Ollama to bring the model into memory. Idempotent.

        Explicit rather than implicit so the cost lands in this stage's own
        duration (§81) instead of being charged to the first frame.
        """
        if self._loaded:
            return
        self._preflight_vram()
        with log_duration(logger, "Loaded vision model", model=self._config.model):
            self._post(
                "/api/generate",
                {"model": self._config.model, "keep_alive": KEEP_ALIVE_ACTIVE},
                timeout=self._config.timeout_seconds,
            )
        self._loaded = True

    def unload(self) -> None:
        """Release the model's VRAM. Idempotent.

        ``keep_alive: 0`` is the whole point. Without it Ollama holds the model
        for five more minutes and the next stage meets an out-of-memory error
        on a card that is, on paper, free (§54).
        """
        # Unconditional, and the reason is the same one the language provider
        # learned: Ollama loads a model to answer whether or not `load` was
        # called, and a caller that reaches straight for `describe` -- as any
        # test or script does -- leaves 5.2 GB of an 8 GB card held. The early
        # return this replaces made that invisible.
        self._loaded = False
        try:
            self._post(
                "/api/generate",
                {"model": self._config.model, "keep_alive": 0},
                timeout=_HEALTH_TIMEOUT_SECONDS,
            )
        except ModelError:  # unloading must never fail a completed stage
            logger.warning("Could not unload the vision model", extra={"model": self._config.model})
        else:
            logger.info("Unloaded vision model", extra={"model": self._config.model})

    # -- analysis -------------------------------------------------------

    def describe(
        self,
        frame_paths: tuple[Path, ...],
        timestamps: tuple[float, ...],
        *,
        prompt_id: str = "vision.frame_description",
    ) -> tuple[VisionObservation, ...]:
        """Describe one small batch of frames.

        Batches are small by design: only candidate keyframes reach the model,
        never every sampled frame (§16).

        Raises:
            ModelError: the request failed, or the response failed validation
                on every attempt.
        """
        if not frame_paths:
            return ()
        if len(frame_paths) != len(timestamps):
            raise ValidationError(
                "Every frame needs its timestamp: an observation without one cannot be "
                "placed on the timeline.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"frames": len(frame_paths), "timestamps": len(timestamps)},
                recoverable=False,
            )

        prompt = load_prompt(prompt_id)
        payload = {
            "model": self._config.model,
            "prompt": prompt.render(
                game=self._game,
                frame_count=len(frame_paths),
                timestamps=", ".join(_clock(value) for value in timestamps),
            ),
            "images": [_encode(path) for path in frame_paths],
            # §93: the schema constrains the runtime, not just the validator.
            "format": prompt.output_schema,
            "stream": False,
            "keep_alive": KEEP_ALIVE_ACTIVE,
            "options": {
                "temperature": self._config.temperature,
                "num_predict": self._config.max_output_tokens,
                # Not optional, and the reason is images. Ollama defaults to a
                # 4,096-token context and *rejects* anything larger with HTTP
                # 400 rather than truncating it. One 1080p frame is roughly
                # 1,400 tokens, so a batch of four measured 5,712 and every
                # request failed all three attempts in 1.7 s -- which reads in
                # the logs as "the vision model returned no usable result",
                # a sentence about the model that was never about the model.
                "num_ctx": self._config.context_tokens,
            },
        }

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._post(
                    "/api/generate", payload, timeout=self._config.timeout_seconds
                )
                # Ollama loaded the model to answer this, whether or not
                # anyone called `load` first, so the card is holding it now.
                self._loaded = True
                return _to_observations(response.get("response", ""), timestamps)
            except (ValidationError, ModelError) as exc:
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    logger.warning(
                        "Vision response rejected; retrying",
                        extra={
                            "attempt": attempt,
                            "model": self._config.model,
                            "error": str(exc),
                        },
                    )

        raise ModelError(
            f"The vision model returned no usable result after {MAX_ATTEMPTS} attempts.",
            code=ErrorCode.VISION_FAILED,
            details={"model": self._config.model, "frames": len(frame_paths)},
            cause=last_error,
        )

    # -- transport ------------------------------------------------------

    def _endpoint(self, path: str) -> str:
        return f"{self._config.endpoint.rstrip('/')}{path}"

    def _get(self, path: str, *, timeout: int) -> dict[str, Any] | None:
        request = urllib.request.Request(self._endpoint(path), method="GET")
        try:
            # Fixed scheme and a loopback endpoint from configuration (§85).
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
        except urllib.error.HTTPError as exc:
            raise ModelError(
                f"Ollama rejected the request: HTTP {exc.code}.",
                code=ErrorCode.VISION_FAILED,
                details={"endpoint": path, "model": self._config.model},
                cause=exc,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelError(
                f"Cannot reach Ollama at {self._config.endpoint}: {exc}",
                code=ErrorCode.MODEL_UNAVAILABLE,
                details={"endpoint": self._config.endpoint},
                cause=exc,
            ) from exc
        except ValueError as exc:
            raise ModelError(
                "Ollama returned a response that is not JSON.",
                code=ErrorCode.LLM_INVALID_JSON,
                details={"endpoint": path},
                cause=exc,
            ) from exc

    def _preflight_vram(self) -> None:
        """Refuse a model that cannot fit, rather than meeting it as an OOM."""
        if self._gpu is None or not self._gpu.preflight_vram_check or not self._gpu.enabled:
            return
        needed = self._config.estimated_vram_mb
        usable = self._gpu.usable_vram_mb
        if needed and usable and needed > usable:
            raise ModelError(
                f"{self._config.model} needs about {needed} MB of VRAM but only "
                f"{usable} MB is usable.",
                code=ErrorCode.GPU_OUT_OF_MEMORY,
                details={"model": self._config.model, "needed_mb": needed, "usable_mb": usable},
            )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _to_observations(
    raw: str, timestamps: tuple[float, ...]
) -> tuple[VisionObservation, ...]:
    """Validate a model response and pair each description with its timestamp.

    Raises:
        ValidationError: the response is not JSON, is not the expected shape,
            or describes a different number of frames than were sent. §94:
            rejected, never carried forward — an observation attached to the
            wrong second is worse than no observation, because it will be
            believed.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError(
            "The vision model did not return JSON.",
            code=ErrorCode.LLM_INVALID_JSON,
            details={"excerpt": str(raw)[:400]},
            cause=exc,
        ) from exc

    frames = payload.get("frames") if isinstance(payload, dict) else None
    if not isinstance(frames, list) or not frames:
        raise ValidationError(
            "The vision response has no 'frames' array.",
            code=ErrorCode.SCHEMA_VALIDATION_FAILED,
            details={"excerpt": str(raw)[:400]},
        )
    if len(frames) != len(timestamps):
        raise ValidationError(
            f"The model described {len(frames)} frames but {len(timestamps)} were sent; "
            "the descriptions cannot be matched to timestamps.",
            code=ErrorCode.SCHEMA_VALIDATION_FAILED,
            details={"described": len(frames), "sent": len(timestamps)},
        )

    observations: list[VisionObservation] = []
    for timestamp, frame in zip(timestamps, frames, strict=True):
        if not isinstance(frame, dict):
            raise ValidationError(
                "A frame description is not an object.",
                code=ErrorCode.SCHEMA_VALIDATION_FAILED,
                details={"timestamp": timestamp},
            )
        description = str(frame.get("description", "")).strip()
        if not description:
            raise ValidationError(
                "A frame description is empty.",
                code=ErrorCode.SCHEMA_VALIDATION_FAILED,
                details={"timestamp": timestamp},
            )
        observations.append(
            VisionObservation(
                timestamp=timestamp,
                description=description,
                labels=tuple(
                    str(label).strip().lower()
                    for label in (frame.get("labels") or [])
                    if str(label).strip()
                ),
                confidence=_confidence(frame.get("confidence")),
                hud=dict(frame.get("hud") or {}) if isinstance(frame.get("hud"), dict) else {},
            )
        )
    return tuple(observations)


def _confidence(value: Any) -> float:
    """Clamp the model's self-reported confidence into 0-1.

    Models do return 1.5 and -0.2. Clamping rather than rejecting: the
    description is still usable, and the number is a hint either way.
    """
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _encode(path: Path) -> str:
    """Base64-encode one frame for the request body."""
    try:
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise ModelError(
            f"Cannot read frame {path}: {exc}",
            code=ErrorCode.VISION_FAILED,
            details={"path": str(path)},
            cause=exc,
            recoverable=False,
        ) from exc


def _clock(seconds: float) -> str:
    """Format a source timestamp for the prompt, so the model sees real times."""
    total = max(float(seconds), 0.0)
    hours, remainder = divmod(total, 3600)
    minutes, rest = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{rest:06.3f}"


__all__ = ["KEEP_ALIVE_ACTIVE", "MAX_ATTEMPTS", "OllamaVisionProvider"]
