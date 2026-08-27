"""Job models and the stage dependency graph.

SPEC sections 46 (job system), 47 (resume), 81 (status/error/retry/duration),
82 (cancellation), 127 (re-edit without re-analysis).

The graph here is the single description of the pipeline's shape. The job
manager, the analysis API's partial/re-analysis modes (§90) and the crash
recovery path all read it instead of restating the stage order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.models.enums import JobStage, JobStatus

#: Direct prerequisites of each stage (§46). A stage may start only once every
#: prerequisite has completed.
STAGE_DEPENDENCIES: dict[JobStage, tuple[JobStage, ...]] = {
    JobStage.IMPORT: (),
    JobStage.PROBE: (JobStage.IMPORT,),
    JobStage.PROXY: (JobStage.PROBE,),
    JobStage.AUDIO: (JobStage.PROBE,),
    JobStage.FRAMES: (JobStage.PROXY,),
    JobStage.TRANSCRIPT: (JobStage.AUDIO,),
    JobStage.AUDIO_EVENTS: (JobStage.AUDIO,),
    JobStage.SCENES: (JobStage.PROXY,),
    JobStage.VISION: (JobStage.FRAMES,),
    JobStage.OCR: (JobStage.FRAMES,),
    JobStage.GAME_EVENTS: (
        JobStage.SCENES,
        JobStage.VISION,
        JobStage.OCR,
        JobStage.AUDIO_EVENTS,
        JobStage.TRANSCRIPT,
    ),
    JobStage.MOMENTS: (JobStage.GAME_EVENTS,),
    JobStage.STORY: (JobStage.MOMENTS,),
    JobStage.EDL: (JobStage.STORY,),
    JobStage.CRITIQUE: (JobStage.EDL,),
    JobStage.RENDER: (JobStage.CRITIQUE,),
    JobStage.QA: (JobStage.RENDER,),
    JobStage.EXPORT: (JobStage.QA,),
    # QA, not EXPORT. The two are parallel deliveries of the same QA-passed
    # render -- a copy to disk and an upload to a destination -- and chaining
    # them forced a local export nobody asked for before every upload. Both
    # stay manual (§51): neither is ever queued without an explicit request.
    JobStage.PUBLISH: (JobStage.QA,),
}

#: Stages that consume the source recording. Expensive, cached, and preserved
#: across re-edits.
ANALYSIS_STAGES: tuple[JobStage, ...] = (
    JobStage.IMPORT,
    JobStage.PROBE,
    JobStage.PROXY,
    JobStage.AUDIO,
    JobStage.FRAMES,
    JobStage.TRANSCRIPT,
    JobStage.AUDIO_EVENTS,
    JobStage.SCENES,
    JobStage.VISION,
    JobStage.OCR,
    JobStage.GAME_EVENTS,
    JobStage.MOMENTS,
)

#: Stages that turn existing analysis into a video. §127 requires these -- and
#: only these -- to re-run when the user changes target duration, mode or the
#: timeline. Re-analysing the source after an edit is a design failure.
EDIT_STAGES: tuple[JobStage, ...] = (
    JobStage.STORY,
    JobStage.EDL,
    JobStage.CRITIQUE,
    JobStage.RENDER,
    JobStage.QA,
)

#: Delivery stages: what happens to a finished, QA-passed render. Kept separate
#: from EDIT_STAGES so that adding a destination (YouTube auto-publish, or any
#: other) extends this tuple and registers a publisher, without touching the
#: analysis or edit pipeline.
DELIVERY_STAGES: tuple[JobStage, ...] = (
    JobStage.EXPORT,
    JobStage.PUBLISH,
)

#: Stages that are never queued automatically. The automatic pipeline stops at
#: QA; delivery is always an explicit user action, which is also what §51
#: requires -- gameplay footage never leaves the machine on its own.
MANUAL_STAGES: frozenset[JobStage] = frozenset(DELIVERY_STAGES)


def stages_in_order() -> tuple[JobStage, ...]:
    """Return every stage in a dependency-respecting order."""
    return tuple(JobStage)


def automatic_stages() -> tuple[JobStage, ...]:
    """Return the stages the pipeline runs without being asked (§46)."""
    return tuple(stage for stage in stages_in_order() if stage not in MANUAL_STAGES)


def dependencies_of(stage: JobStage) -> tuple[JobStage, ...]:
    """Return the direct prerequisites of ``stage``."""
    return STAGE_DEPENDENCIES[stage]


def dependents_of(stage: JobStage) -> tuple[JobStage, ...]:
    """Return the stages that directly depend on ``stage``."""
    return tuple(
        candidate
        for candidate, prerequisites in STAGE_DEPENDENCIES.items()
        if stage in prerequisites
    )


def downstream_of(stage: JobStage) -> tuple[JobStage, ...]:
    """Return every stage transitively invalidated when ``stage`` re-runs."""
    invalidated: set[JobStage] = set()
    frontier = [stage]
    while frontier:
        current = frontier.pop()
        for dependent in dependents_of(current):
            if dependent not in invalidated:
                invalidated.add(dependent)
                frontier.append(dependent)
    return tuple(candidate for candidate in stages_in_order() if candidate in invalidated)


def runnable_stages(
    completed: set[JobStage], *, include_manual: bool = False
) -> tuple[JobStage, ...]:
    """Return stages whose prerequisites are satisfied and which are not done.

    This is what resume uses after a crash: completed work is preserved and the
    pipeline continues from the first stage that can legally run (§47).

    Args:
        completed: stages already finished.
        include_manual: include delivery stages. Off by default so automatic
            recovery never publishes anything on its own.
    """
    return tuple(
        stage
        for stage in stages_in_order()
        if stage not in completed
        and (include_manual or stage not in MANUAL_STAGES)
        and set(dependencies_of(stage)) <= completed
    )


def validate_stage_graph() -> None:
    """Assert the graph is complete and acyclic.

    Called by the job-system tests and at application startup, so a bad edit to
    :data:`STAGE_DEPENDENCIES` fails immediately rather than deadlocking a
    long-running analysis.
    """
    missing = [stage for stage in JobStage if stage not in STAGE_DEPENDENCIES]
    if missing:
        raise ValueError(
            "STAGE_DEPENDENCIES is missing entries for: "
            + ", ".join(stage.value for stage in missing)
        )

    order = {stage: index for index, stage in enumerate(stages_in_order())}
    for stage, prerequisites in STAGE_DEPENDENCIES.items():
        for prerequisite in prerequisites:
            if order[prerequisite] >= order[stage]:
                raise ValueError(
                    f"Stage {stage.value!r} depends on {prerequisite.value!r}, which is "
                    "not declared before it. The JobStage enum order must be a "
                    "topological order of the dependency graph."
                )

    groups = {
        "ANALYSIS_STAGES": set(ANALYSIS_STAGES),
        "EDIT_STAGES": set(EDIT_STAGES),
        "DELIVERY_STAGES": set(DELIVERY_STAGES),
    }
    covered = set().union(*groups.values())
    uncovered = [stage for stage in JobStage if stage not in covered]
    if uncovered:
        raise ValueError(
            "Every stage must belong to exactly one of ANALYSIS_STAGES, EDIT_STAGES or "
            "DELIVERY_STAGES so that §127 re-edit behaviour is well defined. "
            "Unclassified: " + ", ".join(stage.value for stage in uncovered)
        )
    names = sorted(groups)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            overlap = groups[first] & groups[second]
            if overlap:
                raise ValueError(
                    f"Stages appear in both {first} and {second}: "
                    + ", ".join(stage.value for stage in overlap)
                )


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Job(_Base):
    """One pipeline stage execution (§45 ``analysis_jobs``, §81)."""

    id: str
    project_id: str
    media_id: str | None = None
    stage: JobStage
    status: JobStatus = JobStatus.QUEUED

    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    message: str | None = Field(
        default=None, description="Human-readable current step, shown on the analysis screen."
    )

    attempt: int = Field(default=0, ge=0, description="Completed attempts.")
    max_attempts: int = Field(default=3, ge=1)

    error_code: str | None = None
    error_message: str | None = None

    #: Set by a cancellation request; the worker stops at its next checkpoint
    #: and leaves the project consistent (§82).
    cancel_requested: bool = False

    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0)

    payload: dict[str, Any] = Field(
        default_factory=dict, description="Stage inputs, e.g. chunk range or media id."
    )
    result: dict[str, Any] = Field(
        default_factory=dict, description="Stage outputs: artefact paths, counts, cache key."
    )

    @field_validator("created_at", "started_at", "completed_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @property
    def retry_count(self) -> int:
        """Retries performed so far (§81)."""
        return max(self.attempt - 1, 0)

    @property
    def can_retry(self) -> bool:
        """Whether another attempt is permitted."""
        return self.status is JobStatus.FAILED and self.attempt < self.max_attempts


class ProjectStageStatus(_Base):
    """Per-stage rollup used by ``GET /projects/{id}/status`` and the §60 screen."""

    stage: JobStage
    status: JobStatus | None = Field(
        default=None, description="None when the stage has never been queued."
    )
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    error_code: str | None = None
    updated_at: datetime | None = None


__all__ = [
    "ANALYSIS_STAGES",
    "DELIVERY_STAGES",
    "EDIT_STAGES",
    "MANUAL_STAGES",
    "STAGE_DEPENDENCIES",
    "Job",
    "ProjectStageStatus",
    "automatic_stages",
    "dependencies_of",
    "dependents_of",
    "downstream_of",
    "runnable_stages",
    "stages_in_order",
    "validate_stage_graph",
]
