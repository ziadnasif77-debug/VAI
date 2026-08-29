"""Configuration loading (SPEC section 91).

Reads ``config/*.yaml``, applies environment overrides, and validates the result
into an :class:`~backend.config.schema.AppConfig`. Business logic reads the
resulting object; it never re-reads a file, and it never supplies its own
default for a value the configuration owns.

Environment overrides use ``VAI__<TOP_KEY>__<PATH>``::

    VAI__APPLICATION__API__PORT=9000
    VAI__ANALYSIS__CHUNK_SECONDS=300
    VAI__MODELS__VISION__MODEL=llava:13b

Values are parsed as YAML scalars, so ``true``, ``42`` and ``1.5`` arrive with
the right type and quoted strings stay strings.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError as PydanticValidationError

from backend.config.schema import AppConfig
from backend.core.errors import ConfigurationError, ErrorCode

#: Configuration files and the top-level keys each one is allowed to define.
#: A file that defines an unlisted key is a configuration error, so a section
#: cannot quietly move between files and shadow another.
CONFIG_FILES: Final[dict[str, tuple[str, ...]]] = {
    "application.yaml": ("application",),
    "output.yaml": ("output",),
    "analysis.yaml": ("analysis",),
    "models.yaml": ("models", "cloud", "gpu", "hardware", "fallbacks"),
    "moments.yaml": ("moments",),
    "narrative.yaml": ("narrative",),
    "rendering.yaml": (
        "video",
        "render",
        "youtube_preset",
        "ffmpeg",
        "proxy",
        "thumbnails",
        "remotion",
        "shorts",
    ),
    "effects.yaml": ("effects",),
    "audio.yaml": ("audio",),
    "captions.yaml": ("captions",),
    "qa.yaml": ("qa",),
    "critique.yaml": ("critique",),
    "publishing.yaml": ("publishing",),
    "daily.yaml": ("daily",),
    "interaction.yaml": ("interaction", "presets"),
}

#: Prefix and separator for environment overrides.
ENV_PREFIX: Final[str] = "VAI__"
ENV_SEPARATOR: Final[str] = "__"

_cached_config: AppConfig | None = None
_cached_config_dir: Path | None = None


def default_config_dir() -> Path:
    """Return the repository's ``config/`` directory."""
    # Imported here so backend.config.paths can import the schema without a cycle.
    from backend.config.paths import find_repository_root

    return find_repository_root() / "config"


def load_config(
    config_dir: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Load, merge and validate the configuration.

    Args:
        config_dir: directory holding the YAML files. Defaults to ``config/``.
        env: environment mapping used for ``VAI__`` overrides. Defaults to
            :data:`os.environ`. Tests pass an explicit mapping.
        overrides: nested dict merged last, above the environment. Used by tests
            and by the CLI's explicit flags.

    Raises:
        ConfigurationError: a file is missing, unparseable, defines a key it
            does not own, or the merged result fails validation.
    """
    directory = Path(config_dir) if config_dir is not None else default_config_dir()
    if not directory.is_dir():
        raise ConfigurationError(
            f"Configuration directory not found: {directory}",
            code=ErrorCode.CONFIG_NOT_FOUND,
            details={"config_dir": str(directory)},
        )

    merged: dict[str, Any] = {}
    for filename, allowed_keys in CONFIG_FILES.items():
        path = directory / filename
        if not path.is_file():
            raise ConfigurationError(
                f"Missing configuration file: {path}",
                code=ErrorCode.CONFIG_NOT_FOUND,
                details={"file": str(path), "expected_keys": list(allowed_keys)},
            )
        document = _read_yaml(path)
        unexpected = sorted(set(document) - set(allowed_keys))
        if unexpected:
            raise ConfigurationError(
                f"{filename} defines unexpected top-level key(s): {', '.join(unexpected)}. "
                f"It may only define: {', '.join(allowed_keys)}.",
                code=ErrorCode.CONFIG_INVALID,
                details={"file": str(path), "unexpected_keys": unexpected},
            )
        _deep_merge(merged, document)

    _deep_merge(merged, _env_overrides(os.environ if env is None else env))
    if overrides:
        _deep_merge(merged, dict(overrides))

    try:
        return AppConfig.model_validate(merged)
    except PydanticValidationError as exc:
        raise ConfigurationError(
            "Configuration failed validation:\n" + _format_validation_errors(exc),
            code=ErrorCode.CONFIG_INVALID,
            details={"config_dir": str(directory), "error_count": exc.error_count()},
            cause=exc,
        ) from exc


def get_config(config_dir: Path | None = None) -> AppConfig:
    """Return the process-wide configuration, loading it on first use.

    Reloads when a different ``config_dir`` is requested, so a test that points
    at a fixture directory is never served the production configuration.
    """
    global _cached_config, _cached_config_dir

    directory = Path(config_dir) if config_dir is not None else default_config_dir()
    if _cached_config is None or _cached_config_dir != directory:
        _cached_config = load_config(directory)
        _cached_config_dir = directory
    return _cached_config


def reset_config_cache() -> None:
    """Drop the cached configuration. Used by tests and by a settings reload."""
    global _cached_config, _cached_config_dir
    _cached_config = None
    _cached_config_dir = None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(
            f"Cannot read configuration file {path}: {exc}",
            code=ErrorCode.CONFIG_NOT_FOUND,
            details={"file": str(path)},
            cause=exc,
        ) from exc

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"Malformed YAML in {path}: {exc}",
            code=ErrorCode.CONFIG_INVALID,
            details={"file": str(path)},
            cause=exc,
        ) from exc

    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ConfigurationError(
            f"{path} must contain a YAML mapping at the top level, got "
            f"{type(document).__name__}.",
            code=ErrorCode.CONFIG_INVALID,
            details={"file": str(path)},
        )
    return document


def _deep_merge(base: MutableMapping[str, Any], incoming: Mapping[str, Any]) -> None:
    """Recursively merge ``incoming`` into ``base`` in place.

    Mappings merge key by key; every other value (including lists) replaces
    wholesale. Replacing lists is deliberate: half-overriding an ordered
    preference list such as ``render.encoder_preference`` would be a subtle bug.
    """
    for key, value in incoming.items():
        existing = base.get(key)
        if isinstance(existing, MutableMapping) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            base[key] = dict(value) if isinstance(value, Mapping) else value


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Turn ``VAI__A__B=value`` variables into a nested dict."""
    result: dict[str, Any] = {}
    for name, raw_value in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        path = [part.lower() for part in name[len(ENV_PREFIX) :].split(ENV_SEPARATOR) if part]
        if not path:
            continue
        cursor = result
        for part in path[:-1]:
            nested = cursor.get(part)
            if not isinstance(nested, dict):
                nested = {}
                cursor[part] = nested
            cursor = nested
        cursor[path[-1]] = _parse_env_value(raw_value)
    return result


def _parse_env_value(raw: str) -> Any:
    """Parse an environment value as a YAML scalar, falling back to the string."""
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw
    return raw if parsed is None and raw.strip() != "" else parsed


def _format_validation_errors(exc: PydanticValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        lines.append(f"  - {location or '<root>'}: {error['msg']}")
    return "\n".join(lines)


__all__ = [
    "CONFIG_FILES",
    "ENV_PREFIX",
    "default_config_dir",
    "get_config",
    "load_config",
    "reset_config_cache",
]
