"""Stage workers and the registry the runner reads (SPEC §46).

One worker per stage, registered here. The registry is the honest statement of
how far the pipeline is built: a stage absent from it has no implementation
yet, and the runner stops there and says so rather than failing a job it was
never able to run.
"""

from backend.core.models.enums import JobStage
from backend.pipeline.workers.base import ProgressReporter, StageWorker, WorkerContext
from backend.pipeline.workers.media_workers import (
    AudioWorker,
    FramesWorker,
    ImportWorker,
    ProbeWorker,
    ProxyWorker,
)


def default_workers() -> dict[JobStage, StageWorker]:
    """Return the stages that can currently run.

    Phase 2 covers the media engine: everything needed to turn a recording into
    the artefacts the analysis stages read. TRANSCRIPT onward arrive with their
    phases.
    """
    return {
        JobStage.IMPORT: ImportWorker(),
        JobStage.PROBE: ProbeWorker(),
        JobStage.PROXY: ProxyWorker(),
        JobStage.AUDIO: AudioWorker(),
        JobStage.FRAMES: FramesWorker(),
    }


__all__ = [
    "AudioWorker",
    "FramesWorker",
    "ImportWorker",
    "ProbeWorker",
    "ProgressReporter",
    "ProxyWorker",
    "StageWorker",
    "WorkerContext",
    "default_workers",
]
