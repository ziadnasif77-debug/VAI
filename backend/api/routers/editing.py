"""Moments, timeline, render and QA endpoints (SPEC §57–§62, §76, §80).

What the editing screens read and write. Four rules shape the shapes here.

**A screen's request is one request.** The moments screen (§61) shows a type, a
score, a confidence, a duration and *why* — so the moments endpoint returns all
of that together. An interface that assembles one row from four round trips
feels slow no matter how fast each one is.

**Explanations travel with decisions (§80).** Every moment carries the sentences
that justify it and the per-dimension breakdown behind its score. The screen
that shows a ranking is the screen where "why this one?" gets asked.

**Editing is non-destructive (§42) and reversible (§78).** The timeline
endpoints move clips and disable them; nothing here deletes footage, and every
operation goes through :mod:`backend.timeline.operations` so the re-flow rule
holds no matter which caller asked.

**The pipeline is not driven from here.** These endpoints queue work and report
on it; running it is the runner's job (§46). A render request returns a job,
not a video.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.dependencies import get_jobs, get_projects, get_state
from backend.core.errors import ErrorCode, ValidationError
from backend.core.models.enums import JobStage, JobStatus, MomentType, QAStatus
from backend.core.models.jobs import Job
from backend.database.repositories.gaming import GameEventRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.qa import QaRepository
from backend.database.repositories.renders import RenderRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.services.job_manager import JobManager
from backend.services.project_manager import ProjectManager
from backend.timeline import operations, validation

router = APIRouter(tags=["editing"])

#: 422. Starlette renamed the constant and deprecated the old spelling, so
#: naming the number once keeps a DeprecationWarning out of the request path.
_UNPROCESSABLE = 422


# ---------------------------------------------------------------------------
# response models
# ---------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MomentView(_Base):
    """One row of the moments screen (§61), with its reasoning (§80)."""

    id: str
    media_id: str
    moment_type: MomentType
    start_seconds: float
    end_seconds: float
    context_start: float
    context_end: float
    duration_seconds: float
    score: float
    confidence: float
    #: The ten §32 dimensions behind the score, so a ranking can be questioned.
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    explanation: list[str] = Field(default_factory=list)
    #: §79: a low-confidence moment is marked rather than hidden.
    needs_review: bool = False
    user_state: str = "auto"


class MomentListResponse(_Base):
    total: int
    returned: int
    by_type: dict[str, int]
    items: list[MomentView]


class ClipView(_Base):
    """One clip of the timeline screen (§62)."""

    id: str
    index: int
    media_id: str
    moment_id: str | None = None
    moment_type: MomentType | None = None
    source_in: float
    source_out: float
    timeline_start: float
    timeline_end: float
    duration_seconds: float
    enabled: bool
    role: str = "body"
    score: float = 0.0


class TimelineResponse(_Base):
    project_id: str
    duration_seconds: float
    clips: list[ClipView]
    captions: int
    effects: int
    #: Whether the edit is currently renderable, and what is wrong if not.
    valid: bool = True
    problems: list[str] = Field(default_factory=list)


class TimelineOperation(_Base):
    """One edit, in the vocabulary §62 uses.

    A command object rather than a verb per endpoint: the timeline screen sends
    the same shape whichever button was pressed, and adding an operation does
    not add a route.
    """

    action: Literal["delete", "restore", "move", "split", "trim"]
    clip_id: str
    #: ``move`` only: the position to move it to.
    to_index: int | None = None
    #: ``split`` only: a position on the timeline, in seconds.
    at_seconds: float | None = None
    #: ``trim`` only: seconds to add to the in and out points.
    start_delta: float = 0.0
    end_delta: float = 0.0


class EventView(_Base):
    """One detected game event (§21, §27)."""

    id: str
    media_id: str
    event_type: str
    start_seconds: float
    end_seconds: float
    confidence: float
    importance: float
    sources: list[str] = Field(default_factory=list)
    #: How many detectors agreed. §26's own measure of how sure the system is,
    #: and the difference between a four-source kill and a lone audio spike.
    agreement: int = 0
    named: bool = False


class EventListResponse(_Base):
    total: int
    by_type: dict[str, int]
    items: list[EventView]


class RenderView(_Base):
    """A finished or attempted render (§45)."""

    id: str
    status: str
    output_path: str | None = None
    duration_seconds: float | None = None
    resolution: int | None = None
    fps: float | None = None
    encoder: str | None = None
    size_bytes: int | None = None
    created_at: str | None = None


class RenderStatusResponse(_Base):
    project_id: str
    #: The render job, when one has been queued.
    job: Job | None = None
    latest: RenderView | None = None
    #: True when the QA stage says this file should not be published (§76).
    blocked_by_qa: bool = False


class QaFindingView(_Base):
    check: str
    category: str
    qa_status: QAStatus
    detail: str = ""
    remedy: str = ""


class QaResponse(_Base):
    project_id: str
    render_id: str | None = None
    qa_status: QAStatus = QAStatus.PASSED
    blocks_export: bool = False
    needs_review: bool = False
    findings: list[QaFindingView] = Field(default_factory=list)


class QueuedResponse(_Base):
    """What an endpoint returns when it starts work rather than doing it."""

    project_id: str
    queued: list[str]
    message: str


# ---------------------------------------------------------------------------
# moments (§61)
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/moments", response_model=MomentListResponse)
def list_moments(
    project_id: str,
    moment_type: MomentType | None = Query(default=None, alias="type"),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
    limit: int = Query(default=200, ge=1, le=1000),
    projects: ProjectManager = Depends(get_projects),
    state=Depends(get_state),
) -> MomentListResponse:
    """The ranked moments, with the reasoning behind each (§61, §80).

    Filtered by type and score because that is what §61's filter row does; the
    counts are of *everything*, not of the filtered page, so the interface can
    show "12 of 87" rather than "12".
    """
    projects.get(project_id)
    repository = MomentRepository(state.database)
    media = MediaRepository(state.database).list_for_project(project_id)

    collected = [
        moment for item in media for moment in repository.list_for_media(item.id)
    ]
    by_type: dict[str, int] = {}
    for moment in collected:
        by_type[moment.moment_type.value] = by_type.get(moment.moment_type.value, 0) + 1

    filtered = [
        moment
        for moment in collected
        if (moment_type is None or moment.moment_type is moment_type)
        and moment.score >= min_score
    ]
    filtered.sort(key=lambda moment: -moment.score)
    page = filtered[:limit]

    return MomentListResponse(
        total=len(collected),
        returned=len(page),
        by_type=dict(sorted(by_type.items())),
        items=[_moment_view(moment) for moment in page],
    )


# ---------------------------------------------------------------------------
# timeline (§62)
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/timeline", response_model=TimelineResponse)
def get_timeline(
    project_id: str,
    projects: ProjectManager = Depends(get_projects),
    state=Depends(get_state),
) -> TimelineResponse:
    projects.get(project_id)
    return _timeline_response(project_id, state)


@router.post("/projects/{project_id}/timeline/operations", response_model=TimelineResponse)
def apply_operation(
    project_id: str,
    operation: TimelineOperation,
    projects: ProjectManager = Depends(get_projects),
    state=Depends(get_state),
) -> TimelineResponse:
    """Apply one edit and return the timeline as it now stands (§62, §78).

    The whole timeline comes back rather than an acknowledgement: every
    operation re-flows the clips after it, so a screen that patched one row
    would immediately be showing the wrong positions for the rest.
    """
    projects.get(project_id)
    repository = TimelineRepository(state.database)
    timeline = repository.load(project_id)
    if timeline.clip(operation.clip_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No clip {operation.clip_id!r} on this timeline.",
        )

    edited = _apply(timeline, operation)
    repository.save_edit(project_id, edited)
    return _timeline_response(project_id, state)


@router.post("/projects/{project_id}/generate-edit", response_model=QueuedResponse)
def generate_edit(
    project_id: str,
    projects: ProjectManager = Depends(get_projects),
    jobs: JobManager = Depends(get_jobs),
) -> QueuedResponse:
    """Rebuild the edit from stored analysis (§127).

    Re-runs STORY, EDL, CRITIQUE and RENDER without touching a frame of the
    source, which is what makes changing the target duration cost seconds
    instead of an hour.
    """
    projects.get(project_id)
    requeued = _requeue(
        jobs,
        project_id,
        (JobStage.STORY, JobStage.EDL, JobStage.CRITIQUE, JobStage.RENDER),
    )
    return QueuedResponse(
        project_id=project_id,
        queued=requeued,
        message="The edit will be rebuilt from stored moments; no re-analysis is needed.",
    )


# ---------------------------------------------------------------------------
# render and QA (§76)
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/render", response_model=QueuedResponse)
def start_render(
    project_id: str,
    projects: ProjectManager = Depends(get_projects),
    jobs: JobManager = Depends(get_jobs),
) -> QueuedResponse:
    """Queue a render of the current timeline, and the QA that follows it."""
    projects.get(project_id)
    requeued = _requeue(jobs, project_id, (JobStage.RENDER, JobStage.QA))
    return QueuedResponse(
        project_id=project_id,
        queued=requeued,
        message="Rendering queued.",
    )


@router.get("/projects/{project_id}/render-status", response_model=RenderStatusResponse)
def render_status(
    project_id: str,
    projects: ProjectManager = Depends(get_projects),
    jobs: JobManager = Depends(get_jobs),
    state=Depends(get_state),
) -> RenderStatusResponse:
    projects.get(project_id)
    job = next(
        (item for item in jobs.list_jobs(project_id) if item.stage is JobStage.RENDER),
        None,
    )
    latest = RenderRepository(state.database).latest(project_id)
    qa_job = next(
        (item for item in jobs.list_jobs(project_id) if item.stage is JobStage.QA), None
    )
    blocked = bool((qa_job.result or {}).get("blocks_export")) if qa_job else False
    return RenderStatusResponse(
        project_id=project_id,
        job=job,
        latest=_render_view(latest),
        blocked_by_qa=blocked,
    )


@router.get("/projects/{project_id}/qa", response_model=QaResponse)
def get_qa(
    project_id: str,
    projects: ProjectManager = Depends(get_projects),
    state=Depends(get_state),
) -> QaResponse:
    """The QA report for the latest render (§76–§79)."""
    projects.get(project_id)
    latest = RenderRepository(state.database).latest(project_id)
    if latest is None:
        return QaResponse(project_id=project_id)

    render_id = latest.get("id")
    rows = QaRepository(state.database).list_for_render(render_id) if render_id else []
    findings = [
        QaFindingView(
            check=row["check_name"],
            category=row["category"],
            qa_status=QAStatus(row["status"]),
            detail=row.get("detail") or "",
            remedy=str((row.get("measured") or {}).get("remedy", "")),
        )
        for row in rows
    ]
    failures = [item for item in findings if item.qa_status is QAStatus.FAILED]
    warnings = [item for item in findings if item.qa_status is QAStatus.WARNING]
    overall = (
        QAStatus.FAILED if failures else QAStatus.WARNING if warnings else QAStatus.PASSED
    )
    return QaResponse(
        project_id=project_id,
        render_id=render_id,
        qa_status=overall,
        blocks_export=bool(failures),
        needs_review=bool(failures or warnings),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/events", response_model=EventListResponse)
def list_events(
    project_id: str,
    limit: int = Query(default=500, ge=1, le=5000),
    projects: ProjectManager = Depends(get_projects),
    state=Depends(get_state),
) -> EventListResponse:
    """Detected game events, for the analysis screen's detail view (§21, §60)."""
    projects.get(project_id)
    repository = GameEventRepository(state.database)
    media = MediaRepository(state.database).list_for_project(project_id)

    collected = [
        (item.id, event)
        for item in media
        for event in repository.list_for_media(item.id)
    ]
    by_type: dict[str, int] = {}
    for _, event in collected:
        name = event.event_type.value
        by_type[name] = by_type.get(name, 0) + 1

    return EventListResponse(
        total=len(collected),
        by_type=dict(sorted(by_type.items())),
        items=[
            EventView(
                id=f"{media_id}@{event.start_seconds:.3f}",
                media_id=media_id,
                event_type=event.event_type.value,
                start_seconds=event.start_seconds,
                end_seconds=event.end_seconds,
                confidence=event.confidence,
                importance=event.importance,
                sources=list(event.sources),
                agreement=event.agreement,
                named=event.is_named,
            )
            for media_id, event in collected[:limit]
        ],
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _moment_view(moment) -> MomentView:
    return MomentView(
        id=str(moment.metadata.get("id") or ""),
        media_id=moment.media_id,
        moment_type=moment.moment_type,
        start_seconds=round(moment.start_seconds, 3),
        end_seconds=round(moment.end_seconds, 3),
        context_start=round(moment.context_start, 3),
        context_end=round(moment.context_end, 3),
        duration_seconds=round(moment.context_duration, 3),
        score=round(moment.score, 4),
        confidence=round(moment.confidence, 4),
        score_breakdown={
            name: round(value, 4) for name, value in moment.score_breakdown.items()
        },
        explanation=list(moment.explanation),
        needs_review=bool(moment.metadata.get("needs_review", False)),
        user_state=str(moment.metadata.get("user_state", "auto")),
    )


def _timeline_response(project_id: str, state) -> TimelineResponse:
    repository = TimelineRepository(state.database)
    timeline = repository.load(project_id)
    durations = {
        item.id: item.metadata.duration_seconds
        for item in MediaRepository(state.database).list_for_project(project_id)
        if item.metadata.duration_seconds
    }
    report = validation.validate(timeline, media_durations=durations)

    return TimelineResponse(
        project_id=project_id,
        duration_seconds=round(timeline.duration, 3),
        clips=[
            ClipView(
                id=clip.id,
                index=clip.clip_index,
                media_id=clip.media_id,
                moment_id=clip.moment_id,
                moment_type=clip.moment_type,
                source_in=round(clip.source_in, 3),
                source_out=round(clip.source_out, 3),
                timeline_start=round(clip.timeline_start, 3),
                timeline_end=round(clip.timeline_end, 3),
                duration_seconds=round(clip.duration, 3),
                enabled=clip.enabled,
                role=clip.role,
                score=clip.score,
            )
            for clip in timeline.video_clips(enabled_only=False)
        ],
        captions=repository.caption_count(project_id),
        effects=repository.effect_count(project_id),
        valid=report.is_valid,
        problems=[str(item) for item in report.errors],
    )


def _apply(timeline, operation: TimelineOperation):
    """Route a §62 command to its operation, translating errors for the API."""
    try:
        if operation.action == "delete":
            return operations.delete(timeline, operation.clip_id)
        if operation.action == "restore":
            return operations.restore(timeline, operation.clip_id)
        if operation.action == "move":
            if operation.to_index is None:
                raise _missing("move", "to_index")
            return operations.move(timeline, operation.clip_id, operation.to_index)
        if operation.action == "split":
            if operation.at_seconds is None:
                raise _missing("split", "at_seconds")
            return operations.split(timeline, operation.clip_id, operation.at_seconds)
        return operations.trim(
            timeline,
            operation.clip_id,
            start_delta=operation.start_delta,
            end_delta=operation.end_delta,
        )
    except ValidationError as error:
        # A refused edit is the user asking for something impossible, not a
        # server fault: 422 with the reason the operation gave.
        raise HTTPException(
            status_code=_UNPROCESSABLE, detail=str(error)
        ) from error


def _missing(action: str, field: str) -> ValidationError:
    return ValidationError(
        f"{action!r} needs {field!r}.",
        code=ErrorCode.INVALID_TIMELINE_OPERATION,
        details={"action": action, "missing": field},
    )


def _requeue(jobs: JobManager, project_id: str, stages: tuple[JobStage, ...]) -> list[str]:
    """Put stages back in the queue, in order, leaving earlier work alone."""
    queued: list[str] = []
    existing = {job.stage: job for job in jobs.list_jobs(project_id)}
    for stage in stages:
        job = existing.get(stage)
        if job is None:
            queued.append(jobs.queue(project_id, stage).stage.value)
        elif job.status is not JobStatus.QUEUED:
            jobs.requeue(job.id)
            queued.append(stage.value)
    return queued


def _render_view(record: dict[str, Any] | None) -> RenderView | None:
    if record is None:
        return None
    return RenderView(
        id=str(record.get("id")),
        status=str(record.get("status", "")),
        output_path=record.get("output_path"),
        duration_seconds=record.get("duration_seconds"),
        resolution=record.get("resolution"),
        fps=record.get("fps"),
        encoder=record.get("encoder"),
        size_bytes=record.get("size_bytes"),
        created_at=record.get("created_at"),
    )


__all__ = ["router"]
