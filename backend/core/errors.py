"""Typed error codes and the exception hierarchy.

SPEC sections 81 (every stage reports status/error/retry), 94 (invalid AI output
is rejected, retried or falls back) and 95 (graceful degradation).

Every failure crossing a component boundary carries an :class:`ErrorCode`, so
logs, the API, the retry policy and the fallback strategy all agree on what went
wrong without parsing message strings.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Stable, machine-readable failure identifiers."""

    # -- configuration / environment ------------------------------------
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
    CONFIG_INVALID = "CONFIG_INVALID"
    ENVIRONMENT_INVALID = "ENVIRONMENT_INVALID"
    FFMPEG_NOT_FOUND = "FFMPEG_NOT_FOUND"
    FFPROBE_NOT_FOUND = "FFPROBE_NOT_FOUND"

    # -- media ----------------------------------------------------------
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
    MEDIA_CORRUPTED = "MEDIA_CORRUPTED"
    MEDIA_ALREADY_IMPORTED = "MEDIA_ALREADY_IMPORTED"
    UNSUPPORTED_CONTAINER = "UNSUPPORTED_CONTAINER"
    UNSUPPORTED_CODEC = "UNSUPPORTED_CODEC"
    MEDIA_PROBE_FAILED = "MEDIA_PROBE_FAILED"
    PROXY_GENERATION_FAILED = "PROXY_GENERATION_FAILED"
    AUDIO_EXTRACTION_FAILED = "AUDIO_EXTRACTION_FAILED"
    FRAME_EXTRACTION_FAILED = "FRAME_EXTRACTION_FAILED"

    # -- AI models / GPU (§52, §54) -------------------------------------
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    PROVIDER_NOT_REGISTERED = "PROVIDER_NOT_REGISTERED"
    FILE_PICKER_UNAVAILABLE = "FILE_PICKER_UNAVAILABLE"
    GPU_NOT_AVAILABLE = "GPU_NOT_AVAILABLE"
    GPU_OUT_OF_MEMORY = "GPU_OUT_OF_MEMORY"

    # -- analysis stages (§46) ------------------------------------------
    CHUNK_PROCESSING_FAILED = "CHUNK_PROCESSING_FAILED"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
    AUDIO_ANALYSIS_FAILED = "AUDIO_ANALYSIS_FAILED"
    SCENE_DETECTION_FAILED = "SCENE_DETECTION_FAILED"
    VISION_FAILED = "VISION_FAILED"
    OCR_FAILED = "OCR_FAILED"
    HUD_DETECTION_FAILED = "HUD_DETECTION_FAILED"

    # -- gaming intelligence (§21, §27, §28) ----------------------------
    EVENT_DETECTION_FAILED = "EVENT_DETECTION_FAILED"
    EVENT_CORRELATION_FAILED = "EVENT_CORRELATION_FAILED"
    MOMENT_DETECTION_FAILED = "MOMENT_DETECTION_FAILED"
    MOMENT_SCORING_FAILED = "MOMENT_SCORING_FAILED"
    GAME_PROFILE_NOT_FOUND = "GAME_PROFILE_NOT_FOUND"
    GAME_PROFILE_INVALID = "GAME_PROFILE_INVALID"

    # -- narrative / timeline (§36-§40) ---------------------------------
    NARRATIVE_FAILED = "NARRATIVE_FAILED"
    DURATION_TARGET_UNREACHABLE = "DURATION_TARGET_UNREACHABLE"
    INVALID_TARGET_DURATION = "INVALID_TARGET_DURATION"
    INVALID_EDL = "INVALID_EDL"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_TIMELINE_OPERATION = "INVALID_TIMELINE_OPERATION"

    # -- AI output validation (§93, §94) --------------------------------
    LLM_REQUEST_FAILED = "LLM_REQUEST_FAILED"
    LLM_INVALID_JSON = "LLM_INVALID_JSON"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    BUSINESS_VALIDATION_FAILED = "BUSINESS_VALIDATION_FAILED"

    # -- rendering / QA (§65-§67, §76) ----------------------------------
    RENDER_FAILED = "RENDER_FAILED"
    REMOTION_FAILED = "REMOTION_FAILED"
    ENCODING_FAILED = "ENCODING_FAILED"
    AUDIO_MIX_FAILED = "AUDIO_MIX_FAILED"
    QA_FAILED = "QA_FAILED"

    # -- delivery -------------------------------------------------------
    # Publication is a separate layer from rendering so that adding a
    # destination later does not disturb the pipeline.
    PUBLISH_FAILED = "PUBLISH_FAILED"
    PUBLISH_TARGET_NOT_CONFIGURED = "PUBLISH_TARGET_NOT_CONFIGURED"
    PUBLISH_AUTH_FAILED = "PUBLISH_AUTH_FAILED"
    PUBLISH_QUOTA_EXCEEDED = "PUBLISH_QUOTA_EXCEEDED"

    # -- persistence ----------------------------------------------------
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    PROJECT_ALREADY_EXISTS = "PROJECT_ALREADY_EXISTS"
    DATABASE_ERROR = "DATABASE_ERROR"
    MIGRATION_FAILED = "MIGRATION_FAILED"
    STORAGE_ERROR = "STORAGE_ERROR"
    DISK_SPACE_EXHAUSTED = "DISK_SPACE_EXHAUSTED"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"

    # -- jobs (§46, §81, §82) -------------------------------------------
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_DEPENDENCY_FAILED = "JOB_DEPENDENCY_FAILED"
    WORKER_FAILED = "WORKER_FAILED"

    # -- fallback -------------------------------------------------------
    INTERNAL_ERROR = "INTERNAL_ERROR"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Codes the retry policy may re-run (§81, §94). Anything absent is terminal:
#: retrying a corrupted recording or an unsupported container only burns time.
RECOVERABLE_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.GPU_OUT_OF_MEMORY,
        ErrorCode.MODEL_LOAD_FAILED,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.LLM_REQUEST_FAILED,
        ErrorCode.LLM_INVALID_JSON,
        ErrorCode.SCHEMA_VALIDATION_FAILED,
        ErrorCode.WORKER_FAILED,
        ErrorCode.DATABASE_ERROR,
        ErrorCode.CHUNK_PROCESSING_FAILED,
        ErrorCode.PROXY_GENERATION_FAILED,
        ErrorCode.AUDIO_EXTRACTION_FAILED,
        ErrorCode.FRAME_EXTRACTION_FAILED,
        ErrorCode.TRANSCRIPTION_FAILED,
        ErrorCode.AUDIO_ANALYSIS_FAILED,
        ErrorCode.VISION_FAILED,
        ErrorCode.OCR_FAILED,
        ErrorCode.RENDER_FAILED,
        ErrorCode.REMOTION_FAILED,
        ErrorCode.ENCODING_FAILED,
        # A failed upload is usually transient (network, rate limit); a missing
        # credential or an unconfigured target is not, and is absent here.
        ErrorCode.PUBLISH_FAILED,
        ErrorCode.PUBLISH_QUOTA_EXCEEDED,
    }
)

_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.PROJECT_NOT_FOUND: 404,
    ErrorCode.MEDIA_NOT_FOUND: 404,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.MODEL_NOT_FOUND: 404,
    ErrorCode.CONFIG_NOT_FOUND: 404,
    ErrorCode.GAME_PROFILE_NOT_FOUND: 404,
    ErrorCode.PROJECT_ALREADY_EXISTS: 409,
    ErrorCode.MEDIA_ALREADY_IMPORTED: 409,
    ErrorCode.JOB_CANCELLED: 409,
    ErrorCode.INVALID_TARGET_DURATION: 422,
    ErrorCode.INVALID_TIMESTAMP: 422,
    ErrorCode.INVALID_EDL: 422,
    ErrorCode.INVALID_TIMELINE_OPERATION: 422,
    ErrorCode.SCHEMA_VALIDATION_FAILED: 422,
    ErrorCode.BUSINESS_VALIDATION_FAILED: 422,
    ErrorCode.UNSUPPORTED_CONTAINER: 422,
    ErrorCode.UNSUPPORTED_CODEC: 422,
    ErrorCode.MEDIA_CORRUPTED: 422,
    ErrorCode.GAME_PROFILE_INVALID: 422,
    ErrorCode.DURATION_TARGET_UNREACHABLE: 422,
    ErrorCode.PATH_NOT_ALLOWED: 400,
    ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED: 501,
    ErrorCode.PUBLISH_AUTH_FAILED: 401,
    ErrorCode.PUBLISH_QUOTA_EXCEEDED: 429,
    ErrorCode.CONFIG_INVALID: 500,
    ErrorCode.DISK_SPACE_EXHAUSTED: 507,
    ErrorCode.ENVIRONMENT_INVALID: 503,
    ErrorCode.FFMPEG_NOT_FOUND: 503,
    ErrorCode.FFPROBE_NOT_FOUND: 503,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.PROVIDER_NOT_REGISTERED: 503,
    ErrorCode.FILE_PICKER_UNAVAILABLE: 501,
    ErrorCode.GPU_NOT_AVAILABLE: 503,
    ErrorCode.GPU_OUT_OF_MEMORY: 503,
}


def is_recoverable(code: ErrorCode) -> bool:
    """Return whether ``code`` may be retried (§81)."""
    return code in RECOVERABLE_ERROR_CODES


def http_status_for(code: ErrorCode) -> int:
    """Return the HTTP status the local API should use for ``code``."""
    return _HTTP_STATUS.get(code, 500)


class GamingEditorError(Exception):
    """Base class for every failure raised by application code.

    Attributes:
        code: the typed :class:`ErrorCode`.
        message: human-readable description.
        details: structured context (paths, ids, ffmpeg stderr excerpts). Goes
            into logs and API responses, so it must never carry secrets.
        recoverable: whether the retry policy may re-run the operation.
            Defaults to the code's classification; can be narrowed per instance
            when a recoverable code has exhausted its attempts.
    """

    default_code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
        recoverable: bool | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.recoverable = is_recoverable(self.code) if recoverable is None else recoverable
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        """Serialise for API responses and structured logs."""
        return {
            "error_code": self.code.value,
            "message": self.message,
            "details": self.details,
            "recoverable": self.recoverable,
        }

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code.value!r}, message={self.message!r})"


class ConfigurationError(GamingEditorError):
    """Configuration missing, malformed or semantically invalid."""

    default_code = ErrorCode.CONFIG_INVALID


class DependencyError(GamingEditorError):
    """A required external dependency is missing or unusable."""

    default_code = ErrorCode.ENVIRONMENT_INVALID


class MediaError(GamingEditorError):
    """Source media is missing, unreadable or unsupported."""

    default_code = ErrorCode.MEDIA_NOT_FOUND


class ModelError(GamingEditorError):
    """An AI model could not be found, loaded or executed."""

    default_code = ErrorCode.MODEL_LOAD_FAILED


class ResourceError(GamingEditorError):
    """A hardware resource (VRAM, RAM, disk) was exhausted (§83, §84)."""

    default_code = ErrorCode.GPU_OUT_OF_MEMORY


class AnalysisError(GamingEditorError):
    """An analysis stage failed (§46)."""

    default_code = ErrorCode.INTERNAL_ERROR


class GamingIntelligenceError(GamingEditorError):
    """Event, correlation or moment detection failed (§21-§32)."""

    default_code = ErrorCode.EVENT_DETECTION_FAILED


class NarrativeError(GamingEditorError):
    """Story construction or duration optimisation failed (§36-§39)."""

    default_code = ErrorCode.NARRATIVE_FAILED


class ValidationError(GamingEditorError):
    """Data failed schema or business validation (§93, §94)."""

    default_code = ErrorCode.SCHEMA_VALIDATION_FAILED


class RenderError(GamingEditorError):
    """Rendering failed (§65-§67)."""

    default_code = ErrorCode.RENDER_FAILED


class QAError(GamingEditorError):
    """The QA engine rejected a rendered file (§76)."""

    default_code = ErrorCode.QA_FAILED


class StorageError(GamingEditorError):
    """Database or filesystem persistence failed."""

    default_code = ErrorCode.STORAGE_ERROR


class NotFoundError(GamingEditorError):
    """A requested entity does not exist."""

    default_code = ErrorCode.PROJECT_NOT_FOUND


class JobError(GamingEditorError):
    """A job could not be scheduled, executed or resumed (§46, §47)."""

    default_code = ErrorCode.WORKER_FAILED


__all__ = [
    "RECOVERABLE_ERROR_CODES",
    "AnalysisError",
    "ConfigurationError",
    "DependencyError",
    "ErrorCode",
    "GamingEditorError",
    "GamingIntelligenceError",
    "JobError",
    "MediaError",
    "ModelError",
    "NarrativeError",
    "NotFoundError",
    "QAError",
    "RenderError",
    "ResourceError",
    "StorageError",
    "ValidationError",
    "http_status_for",
    "is_recoverable",
]
