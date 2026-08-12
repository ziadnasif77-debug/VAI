"""Project endpoints (SPEC section 89)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.dependencies import get_jobs, get_media, get_projects
from backend.core.models.enums import JobStatus, ProjectStatus, VideoMode
from backend.core.models.jobs import ProjectStageStatus
from backend.core.models.project import Project, ProjectCreate, ProjectUpdate
from backend.services.job_manager import JobManager
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectListResponse(BaseModel):
    """A page of projects for the dashboard (§58)."""

    model_config = ConfigDict(extra="forbid")

    total: int
    items: list[Project]


class ProjectStatusResponse(BaseModel):
    """Live pipeline state for the analysis screen (§60)."""

    model_config = ConfigDict(extra="forbid")

    project: Project
    stages: list[ProjectStageStatus]


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queued_stages: list[str]


class CancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancelled_jobs: int


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(
    request: ProjectCreate,
    projects: ProjectManager = Depends(get_projects),
) -> Project:
    """Create a project (§122 steps 2-6)."""
    return projects.create(request)


@router.get("", response_model=ProjectListResponse)
def list_projects(
    project_status: ProjectStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    projects: ProjectManager = Depends(get_projects),
) -> ProjectListResponse:
    return ProjectListResponse(
        total=projects.count(status=project_status),
        items=projects.list(status=project_status, limit=limit, offset=offset),
    )


@router.get("/{project_id}", response_model=Project)
def get_project(
    project_id: str, projects: ProjectManager = Depends(get_projects)
) -> Project:
    return projects.get(project_id)


@router.patch("/{project_id}", response_model=Project)
def update_project(
    project_id: str,
    changes: ProjectUpdate,
    projects: ProjectManager = Depends(get_projects),
) -> Project:
    """Update a project.

    Changing the target duration or mode invalidates the edit stages only; the
    analysis is preserved and never re-run (§127).
    """
    return projects.update(project_id, changes)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    delete_files: bool = Query(
        default=False,
        description="Also delete the project directory, including imported recordings.",
    ),
    projects: ProjectManager = Depends(get_projects),
) -> None:
    projects.delete(project_id, delete_files=delete_files)


@router.get("/{project_id}/status", response_model=ProjectStatusResponse)
def project_status(
    project_id: str,
    projects: ProjectManager = Depends(get_projects),
    jobs: JobManager = Depends(get_jobs),
) -> ProjectStatusResponse:
    """Per-stage pipeline state (§60, §89)."""
    return ProjectStatusResponse(
        project=projects.get(project_id),
        stages=jobs.project_status(project_id),
    )


@router.post("/{project_id}/analyze", response_model=AnalyzeResponse)
def analyze_project(
    project_id: str,
    projects: ProjectManager = Depends(get_projects),
    jobs: JobManager = Depends(get_jobs),
    media_service: MediaIngestionService = Depends(get_media),
) -> AnalyzeResponse:
    """Queue everything the project still needs (§89, §90).

    Idempotent: queueing returns the job that already covers a stage, so
    pressing Analyze twice does not duplicate work.

    Both chains are queued, not only the project-wide one. Per-media stages are
    queued at import, but `/reanalyze` **deletes** the stages it invalidates --
    and re-running vision or moments is exactly what §90 exists for. Queueing
    only the project-wide half left STORY waiting on a MOMENTS that no longer
    existed: a queue that cannot move, with nothing saying why.
    """
    projects.get(project_id)
    recordings = media_service.list_media(project_id)
    queued = [job for item in recordings for job in jobs.queue_import_chain(project_id, item.id)]
    queued.extend(jobs.queue_project_stages(project_id))
    return AnalyzeResponse(
        queued_stages=sorted({job.stage.value for job in queued if job.status is JobStatus.QUEUED})
    )


@router.post("/{project_id}/cancel", response_model=CancelResponse)
def cancel_project(
    project_id: str,
    projects: ProjectManager = Depends(get_projects),
    jobs: JobManager = Depends(get_jobs),
) -> CancelResponse:
    """Request cancellation without corrupting the project (§82)."""
    projects.get(project_id)
    return CancelResponse(cancelled_jobs=jobs.cancel_project(project_id))


class ModesResponse(BaseModel):
    """Options the import screen offers (§59)."""

    model_config = ConfigDict(extra="forbid")

    modes: list[VideoMode]
    default_mode: VideoMode
    duration_presets_seconds: list[int] = Field(
        description="Target durations offered by the UI (§6)."
    )
    default_duration_seconds: int
    min_duration_seconds: int
    max_duration_seconds: int
    resolutions: list[int]
    fps_options: list[int]


__all__ = ["ModesResponse", "router"]
