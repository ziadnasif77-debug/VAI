"""Health and system information endpoints (SPEC section 58)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from backend.api.dependencies import AppState, get_health, get_state
from backend.core.models.enums import HardwareProfile, PublishTarget, VideoMode
from backend.core.models.health import HealthReport
from backend.core.versions import APPLICATION_VERSION
from backend.services.health import HealthService

router = APIRouter(tags=["system"])


class CapabilitiesResponse(BaseModel):
    """What this installation can do, for the import screen (§59)."""

    model_config = ConfigDict(extra="forbid")

    application_version: str
    hardware_profile: HardwareProfile
    modes: list[VideoMode]
    default_mode: VideoMode
    duration_presets_seconds: list[int]
    default_duration_seconds: int
    min_duration_seconds: int
    max_duration_seconds: int
    resolutions: list[int]
    fps_options: list[int]
    vision_enabled: bool
    remotion_enabled: bool
    publish_targets: list[PublishTarget]


class IdentityResponse(BaseModel):
    """Proof that this is *this* application, and nothing more."""

    model_config = ConfigDict(extra="forbid")

    application: str
    version: str


@router.get("/identity", response_model=IdentityResponse)
def identity() -> IdentityResponse:
    """Say who is listening on this port. Costs nothing, deliberately.

    The launcher restarts its own server on every start and must never stop a
    process that merely happens to hold port 8765. It used to ask ``/health``,
    which probes FFmpeg, the GPU and Ollama -- warm that is under a second,
    cold it is twenty-plus, and *cold* is exactly the state a server is in when
    someone relaunches straight after starting it. The three-second probe timed
    out, the launcher concluded the port belonged to a stranger, and refused to
    restart the application it had itself just started.

    Identity is not health. This endpoint answers from constants.
    """
    return IdentityResponse(application="vai", version=APPLICATION_VERSION)


@router.get("/health", response_model=HealthReport)
def health(service: HealthService = Depends(get_health)) -> HealthReport:
    """Validate the environment.

    A missing GPU or model is a warning, not a failure: the pipeline falls back
    to CPU and degrades gracefully (§52, §95).
    """
    return service.report()


@router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities(state: AppState = Depends(get_state)) -> CapabilitiesResponse:
    config = state.config
    policy = config.duration_policy
    profile = state.health.hardware_profile()
    return CapabilitiesResponse(
        application_version=APPLICATION_VERSION,
        hardware_profile=profile,
        modes=list(config.output.modes),
        default_mode=config.output.default_mode,
        duration_presets_seconds=list(policy.presets_seconds),
        default_duration_seconds=policy.default_seconds,
        min_duration_seconds=policy.min_seconds,
        max_duration_seconds=policy.max_seconds,
        resolutions=list(config.video.supported_resolutions),
        fps_options=list(config.video.supported_fps),
        # The vision model is disabled on the LOW profile, so a GPU-less machine
        # reports honestly instead of promising analysis it cannot deliver (§53).
        vision_enabled=(
            config.analysis.vision.enabled
            and config.hardware.profiles[profile].vision_enabled
        ),
        remotion_enabled=config.remotion.enabled,
        publish_targets=list(state.publishers.available()),
    )


__all__ = ["router"]
