"""Structured logging.

SPEC section 81 requires every pipeline stage to report status, error,
retry count, duration and logs. Every record therefore carries::

    timestamp  level  component  job_id  project_id  message  duration  error_code

``project_id`` and ``job_id`` are ambient: they are set once per unit of work
with :func:`log_context` and attach themselves to every record emitted inside
that scope, including records from libraries we call.

Five channel files are produced, one per architectural layer::

    logs/application.log  logs/pipeline.log  logs/ai.log
    logs/rendering.log    logs/qa.log
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.handlers
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any

ROOT_LOGGER_NAME = "vai"

#: Record attributes that :class:`logging.LogRecord` always defines; anything
#: else supplied through ``extra`` is treated as structured context.
_RESERVED_RECORD_KEYS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)

_project_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vai_project_id", default=None
)
_job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vai_job_id", default=None
)


class LogChannel(str, Enum):
    """Destination log file for a component."""

    APPLICATION = "application"
    PIPELINE = "pipeline"
    AI = "ai"
    RENDERING = "rendering"
    QA = "qa"

    @property
    def filename(self) -> str:
        return f"{self.value}.log"


class ContextFilter(logging.Filter):
    """Inject the ambient project/job identifiers into every record.

    Attached to handlers rather than to loggers: a filter on a logger only sees
    records logged directly to it, not records propagating up from children,
    and every one of our records comes from a child logger.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "project_id", None) is None:
            record.project_id = _project_id_var.get()
        if getattr(record, "job_id", None) is None:
            record.job_id = _job_id_var.get()
        if getattr(record, "component", None) is None:
            record.component = _component_from_name(record.name)
        return True


class SafeExtraLogger(logging.Logger):
    """A logger whose ``extra`` keys can never crash the caller.

    :meth:`logging.Logger.makeRecord` raises ``KeyError`` when ``extra``
    contains a key that already exists on the record -- ``name``, ``module``,
    ``filename`` and friends. Those are ordinary words in this domain ("name"
    of a project, "module" of a pipeline), and a logging call must never take
    down the request it was describing. Colliding keys are renamed instead.
    """

    def makeRecord(  # noqa: N802 - overriding a stdlib method name
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Mapping[str, Any] | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        record = super().makeRecord(name, level, fn, lno, msg, args, exc_info, func, None, sinfo)
        if extra:
            for key, value in extra.items():
                target = key if key not in record.__dict__ and key != "asctime" else f"ctx_{key}"
                record.__dict__[target] = value
        return record


def _component_from_name(logger_name: str) -> str:
    """Derive the ``component`` field from a dotted logger name."""
    parts = logger_name.split(".")
    if parts and parts[0] == ROOT_LOGGER_NAME:
        parts = parts[1:]
    if parts and parts[0] in {channel.value for channel in LogChannel}:
        parts = parts[1:]
    return ".".join(parts) if parts else logger_name


class JsonFormatter(logging.Formatter):
    """Render records as single-line JSON with the section 60 field set."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.format_timestamp(record),
            "level": record.levelname,
            "component": getattr(record, "component", None) or _component_from_name(record.name),
            "job_id": getattr(record, "job_id", None),
            "project_id": getattr(record, "project_id", None),
            "message": record.getMessage(),
            "duration": getattr(record, "duration", None),
            "error_code": _error_code_value(getattr(record, "error_code", None)),
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_KEYS and key not in payload
        }
        if extras:
            payload["context"] = _jsonify(extras)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def format_timestamp(record: logging.LogRecord) -> str:
        base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        return f"{base}.{int(record.msecs):03d}Z"


class ConsoleFormatter(logging.Formatter):
    """Compact human-readable format for the terminal."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        component = getattr(record, "component", None) or _component_from_name(record.name)
        bits = [f"{stamp} {record.levelname:<8} {component:<28} {record.getMessage()}"]
        project_id = getattr(record, "project_id", None)
        job_id = getattr(record, "job_id", None)
        error_code = _error_code_value(getattr(record, "error_code", None))
        duration = getattr(record, "duration", None)
        suffix = []
        if project_id:
            suffix.append(f"project={project_id}")
        if job_id:
            suffix.append(f"job={job_id}")
        if duration is not None:
            suffix.append(f"duration={duration:.3f}s")
        if error_code:
            suffix.append(f"error={error_code}")
        if suffix:
            bits.append("  (" + " ".join(suffix) + ")")
        if record.exc_info:
            bits.append("\n" + self.formatException(record.exc_info))
        return "".join(bits)


def _error_code_value(value: Any) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, Enum) else str(value)


def _jsonify(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def configure_logging(
    log_directory: Path,
    *,
    level: str | int = "INFO",
    console: bool = True,
    json_format: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Install the channel handlers. Safe to call repeatedly (idempotent).

    Args:
        log_directory: created if missing; one file per :class:`LogChannel`.
        level: minimum level for the ``vai`` logger tree.
        console: also emit to stderr in human-readable form.
        json_format: write JSON lines to the files (section 60 requires
            structured logs; plain text is offered only for local debugging).
    """
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)

    numeric_level = logging.getLevelName(level) if isinstance(level, str) else level
    if not isinstance(numeric_level, int):
        raise ValueError(f"Unknown log level: {level!r}")

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(numeric_level)
    root.propagate = False
    _reset_handlers(root)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ConsoleFormatter())
        console_handler.setLevel(numeric_level)
        console_handler.addFilter(ContextFilter())
        root.addHandler(console_handler)

    file_formatter: logging.Formatter = (
        JsonFormatter() if json_format else ConsoleFormatter()
    )
    for channel in LogChannel:
        channel_logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.{channel.value}")
        channel_logger.setLevel(numeric_level)
        channel_logger.propagate = True  # console output comes from the root logger
        _reset_handlers(channel_logger)
        handler = logging.handlers.RotatingFileHandler(
            directory / channel.filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(file_formatter)
        handler.setLevel(numeric_level)
        handler.addFilter(ContextFilter())
        channel_logger.addHandler(handler)


def _reset_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        with contextlib.suppress(Exception):  # handler may already be closed
            handler.close()


def shutdown_logging() -> None:
    """Close every file handler. Used by tests and on clean shutdown."""
    for name in [ROOT_LOGGER_NAME] + [
        f"{ROOT_LOGGER_NAME}.{channel.value}" for channel in LogChannel
    ]:
        _reset_handlers(logging.getLogger(name))


def get_logger(
    component: str, channel: LogChannel | str = LogChannel.APPLICATION
) -> logging.Logger:
    """Return the logger for ``component`` writing to ``channel``.

    Args:
        component: dotted component name, e.g. ``"media.ingest"``. It is
            reported verbatim in the ``component`` log field.
        channel: which of the five log files receives the records.

    The logger is a :class:`SafeExtraLogger`. The logger class is swapped only
    for the duration of the call so third-party libraries keep the stdlib
    default.
    """
    resolved = LogChannel(channel) if not isinstance(channel, LogChannel) else channel
    full_name = f"{ROOT_LOGGER_NAME}.{resolved.value}.{component}"

    existing = logging.Logger.manager.loggerDict.get(full_name)
    if isinstance(existing, logging.Logger):
        return existing

    previous_class = logging.getLoggerClass()
    logging.setLoggerClass(SafeExtraLogger)
    try:
        return logging.getLogger(full_name)
    finally:
        logging.setLoggerClass(previous_class)


@contextmanager
def log_context(
    *, project_id: str | None = None, job_id: str | None = None
) -> Iterator[None]:
    """Bind ``project_id`` / ``job_id`` to every record emitted in the block."""
    project_token = _project_id_var.set(project_id) if project_id is not None else None
    job_token = _job_id_var.set(job_id) if job_id is not None else None
    try:
        yield
    finally:
        if job_token is not None:
            _job_id_var.reset(job_token)
        if project_token is not None:
            _project_id_var.reset(project_token)


def current_log_context() -> dict[str, str | None]:
    """Return the ambient identifiers (useful when constructing job records)."""
    return {"project_id": _project_id_var.get(), "job_id": _job_id_var.get()}


@contextmanager
def log_duration(
    logger: logging.Logger,
    message: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    """Log ``message`` on completion with the elapsed wall-clock ``duration``.

    Yields a mutable dict so the caller can attach results discovered inside the
    block (frame counts, output sizes...). On failure the same duration is
    logged at ERROR with the failure's :class:`~backend.core.errors.ErrorCode`
    where available -- section 81 requires every stage failure to be reported.
    """
    # Local import keeps the core package free of import cycles.
    from backend.core.errors import GamingEditorError

    started = time.perf_counter()
    collected: dict[str, Any] = {}
    try:
        yield collected
    except GamingEditorError as exc:
        logger.error(
            message,
            extra={
                "duration": time.perf_counter() - started,
                "error_code": exc.code,
                **fields,
                **collected,
            },
            exc_info=True,
        )
        raise
    except Exception:
        logger.error(
            message,
            extra={
                "duration": time.perf_counter() - started,
                "error_code": "INTERNAL_ERROR",
                **fields,
                **collected,
            },
            exc_info=True,
        )
        raise
    else:
        logger.log(
            level,
            message,
            extra={"duration": time.perf_counter() - started, **fields, **collected},
        )


__all__ = [
    "ConsoleFormatter",
    "ContextFilter",
    "JsonFormatter",
    "LogChannel",
    "SafeExtraLogger",
    "configure_logging",
    "current_log_context",
    "get_logger",
    "log_context",
    "log_duration",
    "shutdown_logging",
]
