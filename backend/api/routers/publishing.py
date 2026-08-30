"""Delivery endpoints — connect YouTube, publish a render (§50, §51, §57).

The auth flow is Google's device grant, split across requests the way the flow
itself is split across time: ``start`` yields a short code the person types at
google.com/device, and each ``poll`` makes exactly one token request. Holding
one HTTP request open for the minutes a person takes to type a code is how a
UI freezes, so the UI polls instead, on the same cadence Google allows.

One pending grant at a time, held in process memory. This is a localhost app
with one person in front of it; a second ``start`` simply replaces the first,
which is also what the person means when they press the button again.

Publishing itself is a job, not a request handler: a 1.14 GiB upload on a
residential line is minutes to hours, and the job queue already owns progress,
cancellation and history (§45). The endpoint's whole work is writing the
person's explicit instruction into the payload — §51 in mechanical form.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from backend.api.dependencies import AppState, get_state
from backend.core.errors import ErrorCode
from backend.core.models.enums import JobStage, PublishTarget
from backend.core.models.publishing import VideoMetadata
from backend.publishing import build_token_provider, youtube_client
from backend.publishing.base import PublishError
from backend.publishing.google_oauth import DeviceFlow, DeviceGrant

router = APIRouter(tags=["publishing"])

#: The one pending device grant, with the flow that started it. Module state
#: on purpose: the grant is worthless without this process's client secret,
#: and a restart simply means pressing "connect" again.
_pending: dict[str, Any] = {}


class TargetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: PublishTarget
    #: Registered at all -- a client pair exists for it.
    available: bool
    #: Ready to publish right now -- for YouTube, a granted token.
    connected: bool


class TargetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[TargetStatus]


class AuthStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verification_url: str
    user_code: str
    expires_in_seconds: int


class AuthPollResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``authorized`` | ``pending`` | ``none`` (no flow was started).
    status: str


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: PublishTarget = PublishTarget.LOCAL_FILE
    metadata: VideoMetadata = Field(default_factory=VideoMetadata)
    destination: str | None = None
    render_id: str | None = None


class QueuedPublish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    target: PublishTarget


@router.get("/publishing/targets", response_model=TargetsResponse)
def targets(state: AppState = Depends(get_state)) -> TargetsResponse:
    """What this build can deliver to, and whether each is ready now."""
    tokens = build_token_provider(state.config, state.paths.data_root)
    return TargetsResponse(
        targets=[
            TargetStatus(target=PublishTarget.LOCAL_FILE, available=True, connected=True),
            TargetStatus(
                target=PublishTarget.YOUTUBE,
                available=youtube_client(state.config, state.paths.data_root) is not None,
                connected=bool(tokens and tokens.is_authorised()),
            ),
        ]
    )


@router.post("/publishing/youtube/auth/start", response_model=AuthStartResponse)
def auth_start(state: AppState = Depends(get_state)) -> AuthStartResponse:
    """Begin the device grant and hand back the code to show the person."""
    client = youtube_client(state.config, state.paths.data_root)
    if client is None:
        raise PublishError(
            "No YouTube OAuth client is configured. Set publishing.youtube "
            "client_id and client_secret_file first.",
            code=ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED,
            details={"target": "youtube"},
            recoverable=False,
        )
    client_id, client_secret = client
    flow = DeviceFlow(client_id=client_id, client_secret=client_secret)
    grant = flow.begin()
    _pending.clear()
    _pending.update({"flow": flow, "grant": grant})
    public = grant.public()
    return AuthStartResponse(
        verification_url=public["verification_url"],
        user_code=public["user_code"],
        expires_in_seconds=public["expires_in_seconds"],
    )


@router.post("/publishing/youtube/auth/poll", response_model=AuthPollResponse)
def auth_poll(state: AppState = Depends(get_state)) -> AuthPollResponse:
    """One poll of the pending grant. The UI calls this every few seconds."""
    flow: DeviceFlow | None = _pending.get("flow")
    grant: DeviceGrant | None = _pending.get("grant")
    if flow is None or grant is None:
        return AuthPollResponse(status="none")

    try:
        token = flow.poll(grant)
    except PublishError:
        # Denied or expired: the grant is dead either way. Clear it so the
        # next start begins clean, and let the typed error reach the UI.
        _pending.clear()
        raise
    if token is None:
        return AuthPollResponse(status="pending")

    tokens = build_token_provider(state.config, state.paths.data_root)
    if tokens is None:  # the client vanished mid-flow; nothing to store into
        _pending.clear()
        raise PublishError(
            "The OAuth client configuration disappeared during sign-in.",
            code=ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED,
            details={"target": "youtube"},
        )
    tokens.store.save(token)
    _pending.clear()
    return AuthPollResponse(status="authorized")


@router.delete("/publishing/youtube/auth")
def auth_disconnect(state: AppState = Depends(get_state)) -> dict[str, bool]:
    """Forget the stored grant. Revocation at Google's side is the person's."""
    tokens = build_token_provider(state.config, state.paths.data_root)
    if tokens is not None:
        tokens.store.clear()
    _pending.clear()
    return {"disconnected": True}


class QueuedShorts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str


@router.post("/projects/{project_id}/shorts", response_model=QueuedShorts)
def shorts(project_id: str, state: AppState = Depends(get_state)) -> QueuedShorts:
    """Queue vertical cuts of the strongest moments (§51: asked for, never assumed)."""
    state.projects.get(project_id)
    existing = next(
        (job for job in state.jobs.list_jobs(project_id) if job.stage is JobStage.SHORTS),
        None,
    )
    job = (
        state.jobs.queue(project_id, JobStage.SHORTS)
        if existing is None
        else state.jobs.requeue(existing.id)
    )
    return QueuedShorts(job_id=job.id)


@router.post("/projects/{project_id}/publish", response_model=QueuedPublish)
def publish(
    project_id: str,
    body: PublishBody,
    state: AppState = Depends(get_state),
) -> QueuedPublish:
    """Queue the delivery the person just confirmed (§51).

    The instruction rides in the job payload verbatim; the worker honours the
    QA verdict and the registry, and the job history is the publication
    history (§81).
    """
    state.projects.get(project_id)

    existing = next(
        (job for job in state.jobs.list_jobs(project_id) if job.stage is JobStage.PUBLISH),
        None,
    )
    payload = {
        "target": body.target.value,
        "metadata": body.metadata.model_dump(mode="json"),
        "destination": body.destination,
        "render_id": body.render_id,
        # Somebody pressed publish. The worker refuses a publication that
        # cannot name who asked for it.
        "authorised_by": "human",
    }
    if existing is None:
        job = state.jobs.queue(project_id, JobStage.PUBLISH, payload=payload)
    else:
        # A new instruction replaces the old payload before the requeue --
        # publishing twice with yesterday's title is nobody's intent.
        job = state.jobs.requeue(existing.id, payload=payload)
    return QueuedPublish(job_id=job.id, target=body.target)


__all__ = ["router"]
