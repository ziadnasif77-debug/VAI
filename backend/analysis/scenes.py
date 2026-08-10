"""Scene detection (SPEC sections 17, 55, 7, 82).

§17: shot boundaries, visual changes, screen-state changes, menu and gameplay
transitions — and one constraint that matters more than any of them:

    **Scene boundaries are supporting information, not automatic edit points.**

That is a design rule, not a caveat. A scene change is where the *picture*
changed, which is a fact about pixels. Where a *clip* should start is a fact
about the moment, and the two coincide far less often than a naive editor
assumes: a kill happens mid-shot, and a menu-to-gameplay transition is a
boundary nobody wants to cut on. Nothing here returns anything a timeline can
use directly.

Detection runs over the **proxy** (§55): at 720p30 that is a quarter of the
source's pixels, for boundaries that do not get more accurate at higher
resolution — and the configured downscale halves it again.

Two products, both wanted:

* **boundaries** — the scenes themselves, for the UI and for §21;
* **change scores** — how much the picture actually moved at each boundary,
  measured rather than assumed, which is what lets the §16 cascade rank
  candidate regions before any model has run.

The video is walked in slices rather than in one call, because a two-hour proxy
is minutes of work and §82 requires a worker to stop at a checkpoint instead of
being killed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from backend.config.schema import SceneAnalysisConfig
from backend.core.errors import AnalysisError, ErrorCode
from backend.core.logging import LogChannel, get_logger, log_duration

logger = get_logger("analysis.scenes", LogChannel.PIPELINE)

#: How much video each pass processes before reporting progress and checking
#: for cancellation. Thirty seconds keeps the check cheap on an eight-hour
#: source while bounding how long a cancel takes to be noticed.
SLICE_SECONDS: Final[float] = 30.0

#: The ContentDetector metric that answers "how different was this frame".
_CHANGE_METRIC: Final[str] = "content_val"


@dataclass(frozen=True, slots=True)
class Scene:
    """One shot, as the detector saw it."""

    index: int
    start_seconds: float
    end_seconds: float
    #: Measured picture change at this scene's opening boundary. ``None`` for
    #: the first scene, which has no boundary before it — and ``None`` rather
    #: than zero, because "no boundary" and "a boundary with no change" are
    #: different things.
    change_score: float | None = None

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def midpoint(self) -> float:
        """A representative instant, used as the scene's keyframe (§17, §56)."""
        return (self.start_seconds + self.end_seconds) / 2.0

    def contains(self, timestamp: float) -> bool:
        return self.start_seconds <= timestamp < self.end_seconds


@dataclass(frozen=True, slots=True)
class SceneResult:
    """Every scene of one file, and the settings that found them."""

    scenes: tuple[Scene, ...]
    duration_seconds: float
    detector: str
    threshold: float

    def __len__(self) -> int:
        return len(self.scenes)

    def __iter__(self):
        return iter(self.scenes)

    def __getitem__(self, index: int) -> Scene:
        return self.scenes[index]

    @property
    def boundaries(self) -> tuple[float, ...]:
        """Scene start times after the first — the *changes* themselves."""
        return tuple(scene.start_seconds for scene in self.scenes[1:])

    def scene_at(self, timestamp: float) -> Scene | None:
        return next((scene for scene in self.scenes if scene.contains(timestamp)), None)

    def keyframe_times(self) -> tuple[float, ...]:
        """One representative instant per scene (§17 → §16's keyframe step)."""
        return tuple(scene.midpoint for scene in self.scenes)

    def summary(self) -> dict[str, Any]:
        if not self.scenes:
            return {"scenes": 0, "duration_seconds": round(self.duration_seconds, 3)}
        durations = [scene.duration for scene in self.scenes]
        return {
            "scenes": len(self.scenes),
            "duration_seconds": round(self.duration_seconds, 3),
            "shortest_seconds": round(min(durations), 3),
            "longest_seconds": round(max(durations), 3),
            "mean_seconds": round(sum(durations) / len(durations), 3),
        }


def detect_scenes(
    path: Path,
    config: SceneAnalysisConfig,
    *,
    duration_seconds: float | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SceneResult:
    """Find shot boundaries in ``path``.

    Args:
        path: the proxy, normally. Any decodable video works.
        config: ``analysis.scenes`` — detector, threshold, minimum scene length
            and downscale factor.
        duration_seconds: for progress reporting and for closing the final
            scene. Read from the file when omitted.
        on_progress: ``(fraction, message)``.
        should_cancel: polled between slices (§82).

    Raises:
        AnalysisError: PySceneDetect is missing, or the file cannot be decoded.
    """
    source = Path(path)
    if not source.is_file():
        raise AnalysisError(
            f"Cannot detect scenes: {source} does not exist.",
            code=ErrorCode.SCENE_DETECTION_FAILED,
            details={"path": str(source)},
            recoverable=False,
        )

    try:
        from scenedetect import SceneManager, StatsManager, open_video
    except ImportError as exc:  # pragma: no cover - reported by doctor.py
        raise AnalysisError(
            "PySceneDetect is not installed, so scene detection is unavailable.",
            code=ErrorCode.SCENE_DETECTION_FAILED,
            details={"remediation": "pip install scenedetect"},
            cause=exc,
        ) from exc

    with log_duration(
        logger,
        "Detected scenes",
        path=str(source),
        detector=config.detector,
        threshold=config.threshold,
    ) as fields:
        try:
            video = open_video(str(source))
            total = duration_seconds or _seconds(video.duration) or 0.0

            stats = StatsManager()
            manager = SceneManager(stats_manager=stats)
            # Fixed, not automatic: §53 scales this with the hardware profile,
            # and a detector that silently picks its own downscale makes scene
            # counts differ between machines.
            manager.auto_downscale = False
            manager.downscale = max(int(config.downscale_factor), 1)
            detector = _build_detector(config)
            manager.add_detector(detector)

            _walk(manager, video, total, on_progress=on_progress, should_cancel=should_cancel)
            scene_list = manager.get_scene_list()
        except AnalysisError:
            raise
        except Exception as exc:
            raise AnalysisError(
                f"Scene detection failed for {source.name}: {exc}",
                code=ErrorCode.SCENE_DETECTION_FAILED,
                details={"path": str(source), "detector": config.detector},
                cause=exc,
            ) from exc

        scenes = _to_scenes(scene_list, total, stats)
        fields["scenes"] = len(scenes)

    result = SceneResult(
        scenes=scenes,
        duration_seconds=total,
        detector=config.detector,
        threshold=config.threshold,
    )
    if on_progress is not None:
        on_progress(1.0, f"{len(scenes)} scenes")
    return result


def scene_change_regions(
    result: SceneResult, *, pre_roll: float = 0.0, post_roll: float = 0.0
) -> list[tuple[float, float]]:
    """Turn boundaries into regions the §16 cascade can nominate.

    A boundary is an instant; a candidate is a span. The roll is applied here
    rather than by each caller, so "scene change" means the same thing wherever
    it is used as a trigger.
    """
    return [
        (max(boundary - pre_roll, 0.0), min(boundary + post_roll, result.duration_seconds))
        for boundary in result.boundaries
    ]


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _walk(
    manager,
    video,
    total: float,
    *,
    on_progress: Callable[[float, str], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> None:
    """Process the video in slices, reporting and checking between each.

    ``detect_scenes`` resumes from the stream's current position, so calling it
    repeatedly walks the file once — no frame is decoded twice, and the
    detector's state carries across the calls.
    """
    while True:
        if should_cancel is not None and should_cancel():
            from backend.media.ffmpeg import CancelledError

            raise CancelledError(details={"stage": "scenes"})

        processed = manager.detect_scenes(video, duration=SLICE_SECONDS, show_progress=False)
        if not processed:
            break
        if on_progress is not None and total > 0:
            position = _seconds(video.position) or 0.0
            on_progress(min(position / total, 1.0), "Detecting scenes")


def _build_detector(config: SceneAnalysisConfig):
    """Construct the configured detector.

    ``min_scene_len`` is handed over as a timecode string so PySceneDetect
    converts it against the video's own frame rate; computing frames ourselves
    would mean guessing that rate.
    """
    from scenedetect import AdaptiveDetector, ContentDetector, ThresholdDetector

    minimum = f"{max(config.min_scene_seconds, 0.0):.3f}s"
    if config.detector == "content":
        return ContentDetector(threshold=config.threshold, min_scene_len=minimum)
    if config.detector == "adaptive":
        return AdaptiveDetector(min_scene_len=minimum)
    return ThresholdDetector(threshold=config.threshold, min_scene_len=minimum)


def _to_scenes(scene_list, duration: float, stats) -> tuple[Scene, ...]:
    """Convert PySceneDetect's timecode pairs into our own model.

    A file with no detected boundary becomes one scene covering the whole
    recording, not zero scenes: a single continuous shot is a legitimate
    result, and returning nothing would make every consumer special-case it.
    """
    if not scene_list:
        return (Scene(index=0, start_seconds=0.0, end_seconds=duration),) if duration > 0 else ()

    scenes: list[Scene] = []
    for start, end in scene_list:
        start_seconds = float(_seconds(start) or 0.0)
        end_seconds = float(_seconds(end) or 0.0)
        if duration > 0:
            end_seconds = min(end_seconds, duration)
        if end_seconds <= start_seconds:
            continue
        scenes.append(
            Scene(
                index=len(scenes),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                change_score=None if not scenes else _change_score(stats, start),
            )
        )
    return tuple(scenes)


def _change_score(stats, boundary) -> float | None:
    """Read the measured frame difference at a boundary.

    The detector records what it saw; storing the configured threshold instead
    would mean every boundary in the database claimed the same magnitude, and
    the cascade could not rank them.
    """
    with contextlib.suppress(Exception):
        values = stats.get_metrics(boundary.frame_num, [_CHANGE_METRIC])
        if values and values[0] is not None:
            return float(values[0])
    return None


def _seconds(timecode) -> float | None:
    """Read a FrameTimecode as seconds across PySceneDetect versions."""
    if timecode is None:
        return None
    value = getattr(timecode, "seconds", None)
    if value is not None:
        return float(value)
    with contextlib.suppress(Exception):  # pragma: no cover - pre-0.7 API
        return float(timecode.get_seconds())
    return None


__all__ = [
    "SLICE_SECONDS",
    "Scene",
    "SceneResult",
    "detect_scenes",
    "scene_change_regions",
]
