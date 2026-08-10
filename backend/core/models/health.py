"""Environment validation models.

Backs the dashboard's system health panel (§58) and the startup check. A missing
GPU or model is reported, never fatal: §52 and §95 require CPU fallback and
graceful degradation.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.core.models.enums import HardwareProfile, HealthStatus
from backend.core.models.project import utcnow


class HealthCheck(BaseModel):
    """Result of validating one external dependency."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Dependency identifier, e.g. 'ffmpeg', 'gpu', 'whisper'.")
    status: HealthStatus
    required: bool = Field(
        description=(
            "Whether the MVP pipeline can run at all without it. Optional "
            "dependencies degrade to a fallback (§95) instead of failing."
        )
    )
    detail: str = Field(description="Human-readable outcome, shown in the UI.")
    version: str | None = None
    remediation: str | None = Field(
        default=None, description="What the user should do when this check is not OK."
    )
    duration_seconds: float | None = None

    @property
    def blocking(self) -> bool:
        """Whether this result prevents the pipeline from running."""
        return self.required and self.status is HealthStatus.FAILED


class HealthReport(BaseModel):
    """Aggregate environment report."""

    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    checked_at: datetime = Field(default_factory=utcnow)
    application_version: str
    hardware_profile: HardwareProfile = Field(
        description="Profile selected from detected hardware (§53)."
    )
    checks: list[HealthCheck] = Field(default_factory=list)

    @property
    def blocking_failures(self) -> list[HealthCheck]:
        return [check for check in self.checks if check.blocking]

    @property
    def warnings(self) -> list[HealthCheck]:
        return [check for check in self.checks if check.status is HealthStatus.WARNING]

    def check(self, name: str) -> HealthCheck | None:
        """Return the named check, or ``None`` if it was not run."""
        for candidate in self.checks:
            if candidate.name == name:
                return candidate
        return None

    @staticmethod
    def aggregate_status(checks: list[HealthCheck]) -> HealthStatus:
        """Worst-case rollup.

        A failed *optional* dependency is a warning, never a failure: §52
        requires a CPU fallback when no GPU is present, so a GPU-less machine
        must still start and run.
        """
        if any(check.blocking for check in checks):
            return HealthStatus.FAILED
        if any(check.status in (HealthStatus.WARNING, HealthStatus.FAILED) for check in checks):
            return HealthStatus.WARNING
        return HealthStatus.OK


__all__ = ["HealthCheck", "HealthReport"]
