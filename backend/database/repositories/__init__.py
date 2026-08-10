"""Repositories: the only place that knows the §45 table layout."""

from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.projects import ProjectRepository

__all__ = ["JobRepository", "MediaRepository", "ProjectRepository"]
