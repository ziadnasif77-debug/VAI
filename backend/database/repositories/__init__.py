"""Repositories: the only place that knows the §45 table layout."""

from backend.database.repositories.audio_events import AudioEventRepository
from backend.database.repositories.frames import FrameRepository, FrameRow
from backend.database.repositories.gaming import GameEventRepository, OcrRepository
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository, MediaTrackRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.projects import ProjectRepository
from backend.database.repositories.qa import QaRepository
from backend.database.repositories.renders import RenderRepository
from backend.database.repositories.scenes import SceneRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.database.repositories.vision import StoredObservation, VisionRepository

__all__ = [
    "AudioEventRepository",
    "FrameRepository",
    "FrameRow",
    "GameEventRepository",
    "JobRepository",
    "MediaRepository",
    "MediaTrackRepository",
    "MomentRepository",
    "OcrRepository",
    "ProjectRepository",
    "QaRepository",
    "RenderRepository",
    "SceneRepository",
    "StoredObservation",
    "TimelineRepository",
    "TranscriptRepository",
    "VisionRepository",
]
