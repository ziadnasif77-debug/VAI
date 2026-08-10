"""Application wiring for the local API.

One :class:`AppState` holds the configuration, paths, database and services.
It is built once at startup and attached to the FastAPI app, so request
handlers receive ready services instead of constructing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Request

from backend.config.loader import load_config
from backend.config.paths import Paths, build_paths
from backend.config.schema import AppConfig
from backend.core.logging import configure_logging
from backend.core.models.jobs import validate_stage_graph
from backend.core.versions import SCHEMA_VERSION
from backend.database.connection import Database
from backend.database.migrator import migrate, verify_schema_version
from backend.interaction.service import InteractionService
from backend.publishing.base import PublisherRegistry
from backend.publishing.local_file import LocalFilePublisher
from backend.services.health import HealthService
from backend.services.job_manager import JobManager
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager


@dataclass(slots=True)
class AppState:
    """Everything a request handler may need."""

    config: AppConfig
    paths: Paths
    database: Database
    projects: ProjectManager
    media: MediaIngestionService
    jobs: JobManager
    health: HealthService
    interaction: InteractionService
    publishers: PublisherRegistry

    def close(self) -> None:
        self.database.close()


def build_state(
    *,
    config: AppConfig | None = None,
    config_dir: Path | None = None,
    data_root: Path | None = None,
    run_migrations: bool = True,
) -> AppState:
    """Construct the application state.

    Args:
        config: pre-loaded configuration. Loaded from ``config_dir`` when absent.
        config_dir: directory holding the YAML files.
        data_root: overrides where projects and the database live. Tests point
            this at a temporary directory.
        run_migrations: apply pending migrations at startup.
    """
    resolved_config = config if config is not None else load_config(config_dir)
    paths = build_paths(resolved_config, data_root=data_root).create()

    configure_logging(
        paths.logs_dir,
        level=resolved_config.application.logging.level,
        console=resolved_config.application.logging.console,
        json_format=resolved_config.application.logging.json_format,
        max_bytes=resolved_config.application.logging.max_bytes,
        backup_count=resolved_config.application.logging.backup_count,
    )

    # A malformed stage graph would deadlock a long analysis; fail at startup.
    validate_stage_graph()

    database = Database(paths.database_path, resolved_config.application.database)
    if run_migrations:
        migrate(database)
    verify_schema_version(database, SCHEMA_VERSION)

    jobs = JobManager(database, resolved_config)
    publishers = PublisherRegistry()
    publishers.register(
        LocalFilePublisher(
            default_directory=(
                Path(resolved_config.publishing.local_file.default_directory)
                if resolved_config.publishing.local_file.default_directory
                else None
            )
        )
    )

    return AppState(
        config=resolved_config,
        paths=paths,
        database=database,
        projects=ProjectManager(database, paths, resolved_config),
        media=MediaIngestionService(database, paths, resolved_config, jobs),
        jobs=jobs,
        health=HealthService(resolved_config),
        interaction=InteractionService(database, resolved_config),
        publishers=publishers,
    )


def get_state(request: Request) -> AppState:
    """FastAPI dependency returning the application state."""
    return request.app.state.services  # type: ignore[no-any-return]


def get_projects(request: Request) -> ProjectManager:
    return get_state(request).projects


def get_media(request: Request) -> MediaIngestionService:
    return get_state(request).media


def get_jobs(request: Request) -> JobManager:
    return get_state(request).jobs


def get_health(request: Request) -> HealthService:
    return get_state(request).health


def get_interaction(request: Request) -> InteractionService:
    return get_state(request).interaction


__all__ = [
    "AppState",
    "build_state",
    "get_health",
    "get_interaction",
    "get_jobs",
    "get_media",
    "get_projects",
    "get_state",
]
