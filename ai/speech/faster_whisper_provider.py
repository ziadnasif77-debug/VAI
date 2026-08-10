"""Local speech-to-text via faster-whisper (SPEC sections 14, 52, 54, 95).

§14 wants segments, word timestamps and confidence, produced locally. This is
the one implementation that talks to a real runtime; everything above it sees
only the :class:`~ai.providers.base.SpeechProvider` protocol, so replacing
Whisper with something else is a configuration change (§13).

Three things here are not incidental:

* **The model is loaded once and unloaded explicitly.** An 8 GB card cannot
  hold Whisper large-v3 and a 7B vision model at the same time (§54), so the
  scheduler needs a real release, not a dropped reference.
* **VAD stays on.** Whisper invents text on silence -- and a gameplay recording
  is mostly silence between callouts. A hallucinated "thanks for watching" over
  a quiet stretch becomes a caption in the finished video.
* **CPU cannot do float16.** CTranslate2 rejects it, and the configured
  ``compute_type`` is written for the GPU path. Falling back to CPU therefore
  also means falling back to a compute type CPU supports, or §52's "CPU
  fallback must exist" is only true on paper.
"""

from __future__ import annotations

import gc
import importlib.util
import math
from pathlib import Path
from typing import Any, Final

from ai.providers.base import ModelInfo, TranscriptSegment, TranscriptWord
from backend.config.schema import GpuConfig, SpeechModelConfig
from backend.core.errors import ErrorCode, ModelError
from backend.core.logging import LogChannel, get_logger, log_duration

logger = get_logger("speech.faster_whisper", LogChannel.AI)

#: What CTranslate2 supports on CPU. float16 is GPU-only and raises on load.
_CPU_COMPUTE_TYPES: Final[frozenset[str]] = frozenset({"int8", "float32"})
_CPU_FALLBACK_COMPUTE_TYPE: Final[str] = "int8"


class FasterWhisperProvider:
    """Speech-to-text backed by a local faster-whisper model."""

    def __init__(
        self,
        config: SpeechModelConfig,
        *,
        gpu: GpuConfig | None = None,
        model_root: Path | None = None,
    ) -> None:
        self._config = config
        self._gpu = gpu
        self._model_root = model_root
        self._model: Any | None = None
        self._resolved_device: str | None = None
        self._resolved_compute_type: str | None = None

    # -- identity -------------------------------------------------------

    def info(self) -> ModelInfo:
        """Provenance stored with every transcript (§49)."""
        return ModelInfo(
            name=self._config.model,
            version=self._config.version,
            provider=self._config.provider,
            device=self._resolved_device or self._config.device,
            estimated_vram_mb=self._config.estimated_vram_mb,
        )

    def is_available(self) -> bool:
        """Whether the runtime is importable.

        Checked before the stage starts so a missing package degrades through
        the §95 fallback chain rather than failing mid-analysis.
        """
        return importlib.util.find_spec("faster_whisper") is not None

    # -- lifecycle ------------------------------------------------------

    def load(self) -> None:
        """Load the model. Idempotent."""
        if self._model is not None:
            return
        if not self.is_available():
            raise ModelError(
                "faster-whisper is not installed, so speech transcription is unavailable.",
                code=ErrorCode.MODEL_UNAVAILABLE,
                details={"remediation": "pip install faster-whisper"},
            )

        from faster_whisper import WhisperModel

        device = self._resolve_device()
        compute_type = self._resolve_compute_type(device)
        self._preflight_vram(device)

        with log_duration(
            logger,
            "Loaded speech model",
            model=self._config.model,
            device=device,
            compute_type=compute_type,
        ):
            try:
                self._model = WhisperModel(
                    self._config.model,
                    device=device,
                    compute_type=compute_type,
                    download_root=str(self._model_root) if self._model_root else None,
                )
            except Exception as exc:
                raise ModelError(
                    f"Could not load speech model {self._config.model!r}: {exc}",
                    code=ErrorCode.MODEL_LOAD_FAILED,
                    details={"model": self._config.model, "device": device,
                             "compute_type": compute_type},
                    cause=exc,
                ) from exc

        self._resolved_device = device
        self._resolved_compute_type = compute_type

    def unload(self) -> None:
        """Release the model and its VRAM. Idempotent.

        The collection is deliberate: CTranslate2 frees device memory when the
        model object is finalised, and without forcing that the next stage's
        model load hits an out-of-memory error on a card that is, on paper,
        empty (§54).
        """
        if self._model is None:
            return
        self._model = None
        gc.collect()
        _empty_torch_cache()
        logger.info("Unloaded speech model", extra={"model": self._config.model})

    # -- transcription --------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        start_offset: float = 0.0,
    ) -> tuple[TranscriptSegment, ...]:
        """Transcribe one audio file.

        Args:
            audio_path: an analysis stream, or one chunk of one.
            language: ``None`` detects. Configuration may pin it.
            start_offset: added to every timestamp so a chunk's results land on
                the source timeline (§7). Whisper reports times relative to the
                file it was given; without this, chunk five's speech would be
                attributed to the first ten minutes of the recording.
        """
        source = Path(audio_path)
        if not source.is_file():
            raise ModelError(
                f"Audio file not found for transcription: {source}",
                code=ErrorCode.TRANSCRIPTION_FAILED,
                details={"path": str(source)},
                recoverable=False,
            )

        self.load()
        assert self._model is not None  # load() raises rather than returning None

        try:
            segments, _ = self._model.transcribe(
                str(source),
                language=language or self._config.language,
                beam_size=5,
                word_timestamps=self._config.word_timestamps,
                # Kept on: a gameplay recording is mostly silence between
                # callouts, and Whisper fills silence with invented text.
                vad_filter=self._config.vad_filter,
            )
            return tuple(
                _to_segment(segment, start_offset) for segment in segments
            )
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(
                f"Transcription failed for {source.name}: {exc}",
                code=ErrorCode.TRANSCRIPTION_FAILED,
                details={"path": str(source), "model": self._config.model},
                cause=exc,
            ) from exc

    # -- internals ------------------------------------------------------

    def _resolve_device(self) -> str:
        """Pick the device, honouring ``auto`` and the CPU fallback (§52)."""
        requested = self._config.device
        if requested == "cpu":
            return "cpu"
        if self._gpu is not None and not self._gpu.enabled:
            return "cpu"

        has_cuda = _cuda_device_count() > 0
        if requested == "cuda":
            if not has_cuda:
                if self._gpu is not None and not self._gpu.fallback_to_cpu:
                    raise ModelError(
                        "models.speech.device is 'cuda' but no CUDA device was found.",
                        code=ErrorCode.GPU_NOT_AVAILABLE,
                        details={"model": self._config.model},
                    )
                logger.warning(
                    "CUDA was requested but is unavailable; transcribing on CPU",
                    extra={"model": self._config.model},
                )
                return "cpu"
            return "cuda"
        return "cuda" if has_cuda else "cpu"

    def _resolve_compute_type(self, device: str) -> str:
        """Pick a compute type the device can actually execute."""
        configured = self._config.compute_type
        if device == "cpu" and configured not in _CPU_COMPUTE_TYPES:
            logger.info(
                "Compute type is GPU-only; using the CPU equivalent",
                extra={"configured": configured, "used": _CPU_FALLBACK_COMPUTE_TYPE},
            )
            return _CPU_FALLBACK_COMPUTE_TYPE
        return configured

    def _preflight_vram(self, device: str) -> None:
        """Refuse a load that cannot fit, instead of discovering it as an OOM."""
        if device != "cuda" or self._gpu is None or not self._gpu.preflight_vram_check:
            return
        needed = self._config.estimated_vram_mb
        usable = self._gpu.usable_vram_mb
        if needed and usable and needed > usable:
            raise ModelError(
                f"{self._config.model} needs about {needed} MB of VRAM but only "
                f"{usable} MB is usable.",
                code=ErrorCode.GPU_OUT_OF_MEMORY,
                details={"model": self._config.model, "needed_mb": needed,
                         "usable_mb": usable},
            )


def _to_segment(segment: Any, start_offset: float) -> TranscriptSegment:
    """Convert one faster-whisper segment into the provider-neutral form."""
    words = tuple(
        TranscriptWord(
            word=str(word.word).strip(),
            start=float(word.start) + start_offset,
            end=float(word.end) + start_offset,
            confidence=_optional_float(getattr(word, "probability", None)),
        )
        for word in (getattr(segment, "words", None) or ())
    )
    return TranscriptSegment(
        start=float(segment.start) + start_offset,
        end=float(segment.end) + start_offset,
        text=str(segment.text).strip(),
        language=None,
        confidence=_confidence_from(getattr(segment, "avg_logprob", None)),
        words=words,
    )


def _confidence_from(avg_logprob: float | None) -> float | None:
    """Map Whisper's average log-probability onto a 0-1 confidence.

    ``exp(avg_logprob)`` is the geometric mean token probability, which is the
    honest reading of the number: it is a fluency score, not a correctness
    guarantee, and §14 treats the transcript as supporting evidence rather than
    the intelligence layer.
    """
    if avg_logprob is None:
        return None
    try:
        return max(0.0, min(1.0, math.exp(float(avg_logprob))))
    except (OverflowError, ValueError):  # pragma: no cover - defensive
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _cuda_device_count() -> int:
    """Number of CUDA devices CTranslate2 can see, or zero."""
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # absence of CUDA is the common case, not an error
        return 0


def _empty_torch_cache() -> None:
    """Release cached CUDA blocks if torch is present.

    faster-whisper does not use torch, but other stages do and they share the
    allocator on the same device (§54: ``empty_cache_between_stages``).
    """
    try:
        import torch

        if torch.cuda.is_available():  # pragma: no cover - needs a GPU
            torch.cuda.empty_cache()
    except Exception:  # torch is optional; its absence is not a failure
        return


__all__ = ["FasterWhisperProvider"]
