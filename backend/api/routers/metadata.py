"""Metadata suggestion endpoint (SPEC §50, §80).

One POST that gathers the stored evidence -- episodes, moments, the STORY
plan, the transcript's language -- and hands it to the pure generator. The
router's own work is exactly the impure part: reading repositories and
grabbing one thumbnail frame with FFmpeg.

A project with no analysis yet is an answerable request, not an error: the
suggestion is then minimal (the project name, no chapters) and the person can
still publish. Only a missing project is a 404. The thumbnail is likewise
best-effort -- a frame grab that fails degrades the suggestion, it does not
fail it, mirroring §79's remedy rule for delivery extras.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.dependencies import AppState, get_state
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.core.models.publishing import VideoMetadata
from backend.database.repositories.gaming import GameEventRepository
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.metadata.generation import detect_transcript_language, suggest
from backend.metadata.thumbnail import render_thumbnail
from backend.moments.formation import Moment

router = APIRouter(tags=["metadata"])

logger = get_logger("api.metadata", LogChannel.APPLICATION)


@router.post("/projects/{project_id}/metadata/suggest", response_model=VideoMetadata)
def suggest_metadata(project_id: str, state: AppState = Depends(get_state)) -> VideoMetadata:
    """Suggest upload metadata from the evidence this project already stores.

    A suggestion, never a publication: the person reviews and edits it in the
    export screen, and only :func:`~backend.api.routers.publishing.publish`
    sends anything anywhere (§51).
    """
    project = state.projects.get(project_id)
    database = state.database

    media_items = MediaRepository(database).list_for_project(project_id)
    moment_repository = MomentRepository(database)
    event_repository = GameEventRepository(database)
    moments: list[Moment] = []
    events_by_media = {}
    for item in media_items:
        moments.extend(moment_repository.list_for_media(item.id))
        events_by_media[item.id] = event_repository.list_for_media(item.id)

    # The STORY result is the video's own structure (§35-§39): its clips are
    # the chapters. One project-wide job (media_id NULL), same as the EDL
    # stage reads it.
    story = JobRepository(database).find(project_id, JobStage.STORY, None)
    clips = story.result.get("clips") if story is not None and story.result else None
    defaults = state.config.publishing.defaults
    if not defaults.generate_chapters:
        clips = None

    segments = TranscriptRepository(database).list_for_project(project_id)
    language = detect_transcript_language(segments)

    metadata = suggest(
        project,
        moments=moments,
        events_by_media=events_by_media,
        story_clips=clips if isinstance(clips, list) else (),
        transcript_language=language,
        min_chapter_seconds=defaults.min_chapter_seconds,
        title_language=defaults.title_language,
    )

    thumbnail = _render_thumbnail(state, project_id, moments, language)
    if thumbnail is not None:
        metadata = metadata.model_copy(update={"thumbnail_path": thumbnail})
    return metadata


def _render_thumbnail(
    state: AppState, project_id: str, moments: list[Moment], language: str | None = None
) -> str | None:
    """The shared recipe, fed from this route's own plumbing."""
    return render_thumbnail(
        database=state.database,
        config=state.config,
        assets_dir=state.paths.project(project_id).assets,
        moments=moments,
        language=language,
    )


__all__ = ["router"]
