"""One reader for a recording's excluded stretches (P0.2, shared since P0.3).

The moments stage and the EDL stage each read the same evidence -- the stored
OCR, the stored vision observations, the game's profile -- through
:mod:`backend.gaming.content` to decide what is not gameplay. P0.3 adds a third
reader, the story stage, which needs the same spans to issue authorized spans
that never reach into an exclusion. Three copies of one query is how the
copies drift, so this is the one place the question is asked.

Never fatal for a store that will not answer (§95): an empty result is
returned and logged. A configuration error is not a store declining to
answer -- a missing profiles directory would turn every game generic and look
like the feature working -- so that one is allowed through (P0.2.1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.errors import ConfigurationError
from backend.core.logging import LogChannel, get_logger

logger = get_logger("gaming.exclusions", LogChannel.PIPELINE)


@dataclass(frozen=True, slots=True)
class Exclusions:
    """What one recording's evidence refuses, ready for every consumer."""

    media_id: str
    #: Merged, bridged stretches that are not gameplay -- what the moment and
    #: timeline stages refuse, and what no authorized span may reach into.
    spans: tuple[tuple[float, float], ...]
    #: The states behind the spans, for anyone who needs to say *what* was
    #: on screen (the EDL's refusal tally names them).
    states: tuple[Any, ...]
    profile: Any

    @property
    def seconds(self) -> float:
        return sum(end - start for start, end in self.spans)


def profile_for(database: Any, media_id: str, profiles_dir: Path) -> Any:
    """The game's profile, or the generic one.

    A recording with no OCR, or a game with no profile, is generic. A
    profiles directory that is not there is a configuration error and is
    raised, not swallowed (P0.2.1).
    """
    from backend.gaming.profiles import GENERIC_PROFILE, load_profile

    row = database.fetch_one(
        "SELECT game_profile FROM ocr_results WHERE media_id = ? "
        "AND game_profile IS NOT NULL LIMIT 1",
        (media_id,),
    )
    name = str(row["game_profile"]) if row is not None else ""
    if not name:
        return GENERIC_PROFILE
    return load_profile(name, profiles_dir).profile


def read_exclusions(
    database: Any,
    media_id: str,
    *,
    duration_seconds: float,
    profiles_dir: Path,
    vision: Sequence[Any] | None = None,
) -> Exclusions:
    """The excluded stretches of one recording, from everything stored about it.

    Args:
        vision: the stored vision observations when the caller already holds
            them; loaded here otherwise.

    Raises:
        ConfigurationError: the profiles directory is missing (P0.2.1).
    """
    from backend.analysis import frame_state
    from backend.database.repositories.gaming import OcrRepository
    from backend.database.repositories.vision import VisionRepository
    from backend.gaming import content

    try:
        profile = profile_for(database, media_id, profiles_dir)
        if vision is not None:
            observations = list(vision)
        else:
            observations = VisionRepository(database).list_for_media(media_id)
        detections = OcrRepository(database).list_for_media(media_id)
        states = content.read(
            detections=detections,
            frame_spans=frame_state.non_gameplay(
                frame_state.spans(observations, duration_seconds=float(duration_seconds))
            ),
            profile=profile,
            duration_seconds=float(duration_seconds),
        )
    except ConfigurationError:
        raise
    except Exception:
        logger.exception(
            "Content states unavailable; the recording is read without exclusions",
            extra={"media_id": media_id},
        )
        return Exclusions(media_id=media_id, spans=(), states=(), profile=None)
    spans = content.excluded_spans(
        states,
        observed_at=[d.timestamp for d in detections]
        + [float(getattr(o, "timestamp", 0.0)) for o in observations],
    )
    return Exclusions(
        media_id=media_id,
        spans=tuple(spans),
        states=tuple(item for item in states if item.excludes),
        profile=profile,
    )


def exclusions_for_media(
    database: Any, media_id: str, profiles_dir: Path
) -> tuple[tuple[float, float], ...]:
    """The excluded stretches of one recording, for a grant issued outside
    the pipeline -- a person trimming a clip outward (P0.3)."""
    row = database.fetch_one("SELECT duration_seconds FROM media WHERE id = ?", (media_id,))
    duration = float(row["duration_seconds"] or 0.0) if row is not None else 0.0
    if duration <= 0.0:
        return ()
    return read_exclusions(
        database, media_id, duration_seconds=duration, profiles_dir=profiles_dir
    ).spans


__all__ = ["Exclusions", "exclusions_for_media", "profile_for", "read_exclusions"]
