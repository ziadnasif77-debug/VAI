"""The gaming-intelligence stages: OCR and GAME_EVENTS.

SPEC sections 21-27 — the product differentiator, and the part §23 constrains
hardest:

    **The application must not require a game profile.**

Both stages therefore run identically with or without one. A profile changes
*what* OCR reads (three declared boxes instead of the whole frame) and *how*
text is interpreted (this game's wording instead of the words that mean the
same thing everywhere), and it changes neither stage's ability to produce
timestamped events.

OCR reads the candidate keyframes the §16 cascade already chose, for the same
reason the vision model does: reading every sampled frame of a two-hour
recording is an afternoon of work to find the four minutes that mattered.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ai.ocr import create_ocr_provider
from ai.providers.base import OcrProvider, TextDetection
from backend.analysis import perception
from backend.analysis.narration import observations_from_narration, read_incidents
from backend.analysis.reactions import ReactionCandidate
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, ReactionType
from backend.core.models.media import Media
from backend.database.repositories.audio_events import AudioEventRepository
from backend.database.repositories.frames import FrameRepository
from backend.database.repositories.gaming import GameEventRepository, OcrRepository
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.scenes import SceneRepository
from backend.database.repositories.transcript import TranscriptRepository
from backend.database.repositories.vision import VisionRepository
from backend.gaming import events as detectors
from backend.gaming.correlation import correlate
from backend.gaming.detection import detect_game
from backend.gaming.fusion import GENERIC_RULES
from backend.gaming.ocr import CROP_DIRNAME, FrameText, read_frames
from backend.gaming.profiles import (
    UNSPECIFIED_GAMES,
    GameProfile,
    ProfileResolution,
    load_profile,
)
from backend.pipeline.reuse import record_success, try_reuse
from backend.pipeline.workers.base import WorkerContext
from backend.pipeline.workers.vision_workers import CANDIDATE_LEVEL

logger = get_logger("pipeline.workers.gaming", LogChannel.PIPELINE)


class OcrWorker:
    """OCR -- read on-screen text off the candidate frames (§25).

    Region-restricted when a profile declares regions; a reduced-resolution
    full-frame read otherwise (§23). Every detection carries the timestamp of
    the frame it came from, which §25 requires and without which none of it
    could become an event.
    """

    stage = JobStage.OCR

    def __init__(self, provider: OcrProvider | None = None) -> None:
        self._provider = provider

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        config = context.config.analysis.ocr
        reused = try_reuse(context, self.stage)
        if reused is not None:
            context.report(1.0, "Reused matching analysis from another project")
            return reused
        if not config.enabled:
            context.report(1.0, "OCR disabled")
            return {"skipped": True, "reason": "ocr disabled in configuration"}

        resolution = _profile_for(context)
        frames = _candidate_frames(context, media)

        # The HUD is read here rather than in its own stage because this stage
        # already opens these frames, and decoding them twice is exactly the
        # waste §15 and §16 exist to prevent. It runs before the OCR engine is
        # checked, so a machine with no OCR still gets HUD events (§95).
        hud_readings = _read_hud(resolution.profile, frames)

        provider = self._provider or create_ocr_provider(context.config)
        if provider is None or not provider.is_available():
            # §95: OCR failing degrades to vision and audio. It does not fail
            # the analysis, and it never invents text.
            context.report(1.0, "No usable OCR engine")
            logger.warning(
                "No usable OCR engine; continuing without on-screen text",
                extra={"media_id": media.id, "engine": config.engine},
            )
            return {
                "skipped": True,
                "reason": "no usable OCR engine",
                "detections": 0,
                "hud_readings": hud_readings,
            }

        if not frames:
            context.report(1.0, "No candidate frames")
            return {"skipped": True, "reason": "no candidate frames", "detections": 0}

        work_dir = context.paths.frames / media.id / CROP_DIRNAME
        try:
            provider.load()
            results = read_frames(
                frames,
                provider,
                config,
                resolution.profile,
                work_dir=work_dir,
                should_cancel=context.should_cancel,
                on_progress=context.report,
            )
        finally:
            provider.unload()
            # §84: the crops are an intermediate, and there is one per region
            # per frame.
            shutil.rmtree(work_dir, ignore_errors=True)

        detections = [item for frame in results for item in frame.detections]
        repository = OcrRepository(context.database)
        with context.database.transaction():
            stored = repository.replace_for_media(
                context.project_id,
                media.id,
                detections,
                game_profile=resolution.id,
                engine=provider.info().provider,
            )

        record_success(context, self.stage)
        context.report(1.0, f"{stored} text detections")
        return {
            "detections": stored,
            "frames_read": len(frames),
            "frames_with_text": len(results),
            "engine": provider.info().provider,
            "mode": "regions" if resolution.profile.has_ocr_regions else "full_frame",
            # §81: the next stage reads this rather than re-deriving it, and a
            # reading is small enough to travel in a job result.
            "hud_readings": hud_readings,
            **resolution.profile.summary(),
        }


class GameEventsWorker:
    """GAME_EVENTS -- turn every detector's evidence into events (§21, §26, §27).

    This is the stage where the pipeline stops describing a recording and starts
    saying what happened in it. It reads only what earlier stages stored, so it
    is cheap, deterministic and re-runnable — changing a correlation window and
    re-running costs seconds, not another pass over the video (§127).
    """

    stage = JobStage.GAME_EVENTS

    def __init__(self, llm_provider: Any = None) -> None:
        """
        Args:
            llm_provider: the model that reads the transcript for incidents
                (§19). Injected the way every other model in the pipeline is,
                so a test proves the wiring without depending on what happens
                to be installed. Built lazily when absent.
        """
        self._llm = llm_provider

    def run(self, context: WorkerContext) -> dict[str, Any]:
        media = context.require_media()
        analysis = context.config.analysis
        ocr_frames = _ocr_frames(context, media.id)
        resolution = _profile_for(context, ocr_frames)

        vision = VisionRepository(context.database).list_for_media(media.id)
        audio = AudioEventRepository(context.database).list_for_media(media.id)
        scenes = SceneRepository(context.database).list_for_media(media.id)
        reactions = _reactions_from(audio)
        narration = _narration_observations(context, media.id, self._llm)

        observations = detectors.detect(
            vision=vision,
            ocr_frames=ocr_frames,
            audio=audio,
            reactions=reactions,
            scenes=scenes,
            hud_readings=_stored_hud_readings(context, media.id),
            narration=narration,
            profile=resolution.profile,
            vision_min_confidence=analysis.vision.min_confidence,
            scene_min_change=analysis.scenes.threshold,
        )
        events = correlate(
            observations,
            window_seconds=analysis.reactions.correlation_window_seconds,
            game_profile=resolution.id,
            min_confidence=analysis.hud.min_confidence,
            # A profile that knows the game names instants the generic table
            # cannot, and is consulted first for exactly that reason (§22).
            fusion_rules=(*resolution.profile.fusion(), *GENERIC_RULES),
        )

        repository = GameEventRepository(context.database)
        with context.database.transaction():
            stored = repository.replace_for_media(context.project_id, media.id, events)

        context.report(1.0, f"{stored} game events")
        return {
            "events": stored,
            "observations": len(observations),
            "by_type": repository.counts_by_type(media.id),
            "named_events": sum(1 for event in events if event.is_named),
            "multi_source_events": sum(1 for event in events if event.agreement > 1),
            # Phase 0.5: the numbers that say whether perception improved or
            # the output merely changed. A ratio on its own misleads -- when
            # nineteen false defeats were removed the unknown ratio *rose*,
            # which was the pipeline getting more honest, not worse.
            **perception.report(
                events,
                observations,
                duration_seconds=media.metadata.duration_seconds or 0.0,
                vision_observations=len(vision),
                candidate_frames=len(ocr_frames),
            ),
            # §23's claim, made checkable: this ran with or without a profile,
            # and the result says which.
            "game_profile": resolution.id,
            "profile_requested": resolution.requested,
            "profile_exact": resolution.exact,
            "inputs": {
                "vision": len(vision),
                "audio": len(audio),
                "scenes": len(scenes),
                "ocr_frames": len(ocr_frames),
                "reactions": len(reactions),
                "narration": len(narration),
                "hud_readings": len(_stored_hud_readings(context, media.id)),
            },
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_hud(
    profile: GameProfile, frames: Sequence[tuple[float, Path]]
) -> list[dict[str, Any]]:
    """Read every declared HUD indicator off the candidate frames (§24).

    Returns plain dictionaries so the readings can travel in a job result and
    survive a restart. A profile that declares no HUD does no work and opens no
    files, which is the unknown-game path (§23).
    """
    if not profile.hud or not frames:
        return []

    from backend.gaming.hud import read_frame

    readings: list[dict[str, Any]] = []
    for timestamp, path in frames:
        try:
            image = _load_image(Path(path))
        except Exception as error:  # a corrupt frame is not a failed analysis
            logger.warning(
                "Could not open a frame for HUD reading",
                extra={"path": str(path), "error": str(error)[:200]},
            )
            continue
        for reading in read_frame(image, profile, timestamp_seconds=float(timestamp)):
            readings.append(
                {
                    "indicator": reading.indicator,
                    "timestamp_seconds": reading.timestamp_seconds,
                    "value": reading.value,
                    "quality": reading.quality.value,
                    "confidence": reading.confidence,
                }
            )
    logger.info("Read the HUD", extra={"readings": len(readings), "frames": len(frames)})
    return readings


def _load_image(path: Path) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as handle:
        return np.asarray(handle.convert("RGB"))


def _stored_hud_readings(context: WorkerContext, media_id: str) -> list[Any]:
    """The HUD readings the OCR stage recorded, as reading objects (§81)."""
    from backend.gaming.hud import HudReading, ReadingQuality

    job = JobRepository(context.database).find(
        context.project_id, JobStage.OCR, media_id=media_id
    )
    rows = (job.result or {}).get("hud_readings") if job else None
    if not rows:
        return []
    return [
        HudReading(
            indicator=str(row["indicator"]),
            timestamp_seconds=float(row["timestamp_seconds"]),
            value=None if row.get("value") is None else float(row["value"]),
            quality=ReadingQuality(row.get("quality", "uncertain")),
            confidence=float(row.get("confidence", 0.0)),
        )
        for row in rows
    ]


def _profile_for(
    context: WorkerContext, ocr_frames: Sequence[FrameText] = ()
) -> ProfileResolution:
    """Resolve the project's game profile, falling back to generic (§23).

    When the project says ``auto`` — which every real project has said — the
    game is looked for in what OCR already read (Phase 0.3). A recognised game
    is written to ``projects.detected_game``, a column that has existed since
    the schema was written with nothing to fill it, and the profile is used
    for this run. Nothing recognised means the generic path, unchanged.

    A game the user named is never overruled: recognition only fills a gap.
    """
    row = context.database.fetch_one(
        "SELECT game, detected_game FROM projects WHERE id = ?", (context.project_id,)
    )
    game = str(row["game"]) if row is not None and row["game"] else "auto"
    if game.strip().lower() not in UNSPECIFIED_GAMES:
        return load_profile(game, context.profiles_dir)

    known = str(row["detected_game"]) if row is not None and row["detected_game"] else ""
    if not known and ocr_frames:
        guess = detect_game(
            (
                detection.text
                for frame in ocr_frames
                for detection in frame.detections
            ),
            context.profiles_dir,
        )
        if guess.recognised:
            known = str(guess.game)
            context.database.execute(
                "UPDATE projects SET detected_game = ? WHERE id = ?",
                (known, context.project_id),
            )
            logger.info(
                "Recognised the game from the screen",
                extra={"project_id": context.project_id, **guess.summary()},
            )

    resolved = load_profile(known or game, context.profiles_dir)
    # Detected, not declared: the resolution is exact for the profile it
    # loaded, and §49 wants "detected with the generic profile" and "detected
    # with the Grounded profile" to stay different claims about the same event.
    return resolved


def _candidate_frames(context: WorkerContext, media: Media) -> list[tuple[float, Path]]:
    """The frames OCR reads: the cascade's keyframes, or the base pass.

    Candidate keyframes are the right target — they are where something was
    already thought to be happening. Falling back to the base pass keeps OCR
    working when the vision stage was skipped, which §95 allows.
    """
    repository = FrameRepository(context.database)
    rows = repository.list_for_media(media.id, level=CANDIDATE_LEVEL)
    if not rows:
        rows = repository.list_for_media(media.id, level="base")
    return [
        (row.timestamp, Path(row.image_path))
        for row in rows
        if Path(row.image_path).is_file()
    ]


def _ocr_frames(context: WorkerContext, media_id: str):
    """Rebuild per-frame text groupings from stored detections."""
    from backend.gaming.ocr import FrameText

    detections = OcrRepository(context.database).list_for_media(media_id)
    grouped: dict[float, list[TextDetection]] = {}
    for detection in detections:
        grouped.setdefault(detection.timestamp, []).append(detection)
    # The frame path is not carried back: these detections came from the
    # database, and their crop was deleted when the OCR stage finished.
    return [
        FrameText(timestamp=timestamp, frame_path=Path(), detections=tuple(items))
        for timestamp, items in sorted(grouped.items())
    ]


def _narration_observations(
    context: WorkerContext, media_id: str, provider: Any = None
) -> list:
    """Read the transcript for incidents, or return nothing (§19, §95).

    Nothing here is required. Without a model, or without speech, the other
    detectors carry the analysis exactly as they did before this existed -- and
    a failure is logged rather than raised, because a recording with no
    narration is ordinary and a broken model should not lose the whole stage.
    """
    if not context.config.analysis.narration.enabled:
        return []
    segments = TranscriptRepository(context.database).list_for_media(media_id)
    if not segments:
        return []
    try:
        incidents = read_incidents(segments, config=context.config, provider=provider)
    except Exception as error:  # pragma: no cover - defensive (§95)
        logger.warning(
            "Could not read the narration; continuing without it",
            extra={"error": str(error)[:200], "media_id": media_id},
        )
        return []
    return observations_from_narration(incidents)


def _reactions_from(audio) -> list[ReactionCandidate]:
    """Recover reaction candidates from the audio events that carry them.

    §20's results are persisted as microphone-track audio events with a
    ``reaction_type`` in their metadata, so this is a read rather than a
    re-detection — the analysis is not repeated to be used.
    """
    recovered: list[ReactionCandidate] = []
    for event in audio:
        name = event.metadata.get("reaction_type")
        if not name:
            continue
        try:
            reaction_type = ReactionType(name)
        except ValueError:  # a type this build does not know
            continue
        recovered.append(
            ReactionCandidate(
                reaction_type=reaction_type,
                start_seconds=event.start_seconds,
                end_seconds=event.end_seconds,
                confidence=event.confidence,
                intensity_db=float(event.metadata.get("intensity_db") or 0.0),
                correlation_offset=event.metadata.get("correlation_offset"),
                correlated_event_type=event.metadata.get("correlated_event_type"),
            )
        )
    return recovered


__all__ = ["GameEventsWorker", "OcrWorker"]
