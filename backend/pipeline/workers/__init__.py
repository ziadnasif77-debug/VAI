"""Stage workers and the registry the runner reads (SPEC §46).

One worker per stage, registered here. The registry is the honest statement of
how far the pipeline is built: a stage absent from it has no implementation
yet, and the runner stops there and says so rather than failing a job it was
never able to run.
"""

from backend.core.models.enums import JobStage
from backend.pipeline.workers.base import ProgressReporter, StageWorker, WorkerContext
from backend.pipeline.workers.gaming_workers import GameEventsWorker, OcrWorker
from backend.pipeline.workers.media_workers import (
    AudioWorker,
    FramesWorker,
    ImportWorker,
    ProbeWorker,
    ProxyWorker,
)
from backend.pipeline.workers.moments_worker import MomentsWorker
from backend.pipeline.workers.speech_workers import AudioEventsWorker, TranscriptWorker
from backend.pipeline.workers.vision_workers import SceneWorker, VisionWorker


def default_workers() -> dict[JobStage, StageWorker]:
    """Return the stages that can currently run.

    Phase 2 covers the media engine -- everything needed to turn a recording
    into the artefacts the analysis stages read. Phase 3 adds speech and audio
    understanding, Phase 4 the visual layer, Phase 5 the gaming intelligence,
    Phase 6 the moments. STORY onward arrive with their phases.
    """
    return {
        JobStage.IMPORT: ImportWorker(),
        JobStage.PROBE: ProbeWorker(),
        JobStage.PROXY: ProxyWorker(),
        JobStage.AUDIO: AudioWorker(),
        JobStage.FRAMES: FramesWorker(),
        JobStage.TRANSCRIPT: TranscriptWorker(),
        JobStage.AUDIO_EVENTS: AudioEventsWorker(),
        JobStage.SCENES: SceneWorker(),
        JobStage.VISION: VisionWorker(),
        JobStage.OCR: OcrWorker(),
        JobStage.GAME_EVENTS: GameEventsWorker(),
        JobStage.MOMENTS: MomentsWorker(),
    }


__all__ = [
    "AudioEventsWorker",
    "AudioWorker",
    "FramesWorker",
    "GameEventsWorker",
    "ImportWorker",
    "MomentsWorker",
    "OcrWorker",
    "ProbeWorker",
    "ProgressReporter",
    "ProxyWorker",
    "SceneWorker",
    "StageWorker",
    "TranscriptWorker",
    "VisionWorker",
    "WorkerContext",
    "default_workers",
]
