"""Job endpoints (SPEC sections 46, 81, 90)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from backend.api.dependencies import get_jobs, get_projects
from backend.core.models.enums import JobStage, JobStatus
from backend.core.models.jobs import Job
from backend.services.job_manager import JobManager
from backend.services.project_manager import ProjectManager

router = APIRouter(tags=["jobs"])


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    items: list[Job]


class InvalidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invalidated_stages: list[str]


@router.get("/projects/{project_id}/jobs", response_model=JobListResponse)
def list_jobs(
    project_id: str,
    job_status: JobStatus | None = Query(default=None, alias="status"),
    projects: ProjectManager = Depends(get_projects),
    jobs: JobManager = Depends(get_jobs),
) -> JobListResponse:
    projects.get(project_id)
    items = jobs.list_jobs(project_id, status=job_status)
    return JobListResponse(total=len(items), items=items)


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str, jobs: JobManager = Depends(get_jobs)) -> Job:
    return jobs.get_job(job_id)


@router.post("/projects/{project_id}/reanalyze", response_model=InvalidateResponse)
def reanalyze(
    project_id: str,
    stage: JobStage = Query(description="Stage to re-run, together with everything after it."),
    projects: ProjectManager = Depends(get_projects),
) -> InvalidateResponse:
    """Re-run one analysis stage and everything downstream of it (§90).

    Stages before it keep their results, so replacing the vision model does not
    force a new transcription.
    """
    projects.get(project_id)
    affected = projects.invalidate_from(project_id, stage)
    return InvalidateResponse(
        invalidated_stages=sorted(item.value for item in affected)
    )


__all__ = ["router"]
