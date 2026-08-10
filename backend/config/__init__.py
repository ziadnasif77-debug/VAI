"""Centralised configuration (SPEC section 91).

``config/*.yaml`` at the repository root is the only source of tunable values.
This package turns those files into validated, typed objects; business logic
reads the objects and never re-reads a file or invents a default.
"""

from backend.config.loader import (
    CONFIG_FILES,
    default_config_dir,
    get_config,
    load_config,
    reset_config_cache,
)
from backend.config.paths import (
    PROJECT_MANIFEST_FILENAME,
    PROJECT_SUBDIRECTORIES,
    Paths,
    ProjectPaths,
    build_paths,
    find_repository_root,
    project_paths,
)
from backend.config.schema import AppConfig

__all__ = [
    "CONFIG_FILES",
    "PROJECT_MANIFEST_FILENAME",
    "PROJECT_SUBDIRECTORIES",
    "AppConfig",
    "Paths",
    "ProjectPaths",
    "build_paths",
    "default_config_dir",
    "find_repository_root",
    "get_config",
    "load_config",
    "project_paths",
    "reset_config_cache",
]
