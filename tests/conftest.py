"""Shared test fixtures.

Every fixture that touches storage points at a ``tmp_path``. No test writes
into the developer's real ``projects/`` tree or database.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.api.dependencies import AppState, build_state
from backend.config.loader import load_config, reset_config_cache
from backend.config.paths import Paths, build_paths, find_repository_root
from backend.config.schema import AppConfig
from backend.core.logging import shutdown_logging
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.services.job_manager import JobManager
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return find_repository_root()


@pytest.fixture(scope="session")
def config_dir(repo_root: Path) -> Path:
    return repo_root / "config"


@pytest.fixture
def config(config_dir: Path) -> Iterator[AppConfig]:
    """The real shipped configuration, loaded fresh."""
    reset_config_cache()
    yield load_config(config_dir)
    reset_config_cache()


@pytest.fixture
def paths(config: AppConfig, tmp_path: Path) -> Paths:
    """Application paths rooted in a temporary directory."""
    return build_paths(config, data_root=tmp_path).create()


@pytest.fixture
def database(paths: Paths, config: AppConfig) -> Iterator[Database]:
    """A migrated database in the temporary data root."""
    db = Database(paths.database_path, config.application.database)
    migrate(db)
    yield db
    db.close()


@pytest.fixture
def project_manager(database: Database, paths: Paths, config: AppConfig) -> ProjectManager:
    return ProjectManager(database, paths, config)


@pytest.fixture
def job_manager(database: Database, config: AppConfig) -> JobManager:
    return JobManager(database, config)


@pytest.fixture
def media_service(
    database: Database, paths: Paths, config: AppConfig, job_manager: JobManager
) -> MediaIngestionService:
    return MediaIngestionService(database, paths, config, job_manager)


@pytest.fixture
def app_state(config: AppConfig, tmp_path: Path) -> Iterator[AppState]:
    """A fully wired application state on temporary storage."""
    state = build_state(config=config, data_root=tmp_path)
    yield state
    state.close()
    shutdown_logging()


@pytest.fixture
def api_client(app_state: AppState) -> Iterator:
    """A ``TestClient`` bound to the temporary application state."""
    from fastapi.testclient import TestClient

    from backend.api.app import create_app

    with TestClient(create_app(state=app_state)) as client:
        yield client


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    """A small file that passes ingestion validation.

    Ingestion checks existence, extension and size; it never decodes. A real
    decodable clip is only needed from Phase 2 onward.
    """
    path = tmp_path / "gameplay.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512)
    return path
