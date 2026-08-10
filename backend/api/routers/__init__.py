"""HTTP routers. Thin: validate, call a service, shape the response."""

from backend.api.routers import health, interaction, jobs, media, projects

__all__ = ["health", "interaction", "jobs", "media", "projects"]
