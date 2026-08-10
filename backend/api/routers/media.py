"""Media endpoints (SPEC section 89)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict

from backend.api.dependencies import get_media, get_projects
from backend.core.models.enums import MediaRole
from backend.core.models.media import Media, MediaImport
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager

router = APIRouter(prefix="/projects/{project_id}/media", tags=["media"])


class MediaListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    items: list[Media]


@router.post("", response_model=Media, status_code=status.HTTP_201_CREATED)
def import_media(
    project_id: str,
    request: MediaImport,
    media: MediaIngestionService = Depends(get_media),
) -> Media:
    """Attach a recording to the project and queue its analysis chain.

    The file is referenced by default; ``copy_into_project`` copies it into the
    project's ``source/`` directory instead. Either way the original is never
    modified (§42).
    """
    return media.import_media(project_id, request)


@router.get("", response_model=MediaListResponse)
def list_media(
    project_id: str,
    role: MediaRole | None = Query(default=None),
    media: MediaIngestionService = Depends(get_media),
    projects: ProjectManager = Depends(get_projects),
) -> MediaListResponse:
    projects.get(project_id)
    items = media.list_media(project_id, role=role)
    return MediaListResponse(total=len(items), items=items)


@router.get("/{media_id}", response_model=Media)
def get_media_item(
    project_id: str,
    media_id: str,
    media: MediaIngestionService = Depends(get_media),
) -> Media:
    return media.get_media(media_id)


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_media(
    project_id: str,
    media_id: str,
    delete_file: bool = Query(
        default=False,
        description=(
            "Delete the file too. Only applies to copies the application made "
            "inside the project; a referenced original is never deleted."
        ),
    ),
    media: MediaIngestionService = Depends(get_media),
) -> None:
    media.remove_media(media_id, delete_file=delete_file)


__all__ = ["router"]
