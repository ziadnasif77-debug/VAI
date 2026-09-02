"""What the soundtrack should do, second by second (V2-P5).

Sound was the last stage in this pipeline with no idea what the session was
doing. Cutting, pacing, emphasis and QA all read the Semantic Timeline;
``backend/rendering/`` did not import it at all. What that cost, concretely:

* one music bed for the whole video, chosen from a single scalar average of
  every selected moment -- so a session that opens quiet and ends in a boss
  fight got the same bed over both, and ``change_on_section: true`` sat in the
  shipped configuration promising otherwise while no code read it;
* ducking under speech, which is real and good, and no ducking under game
  audio at all -- ``event_spans`` existed, ``game_event_duck_db: -8.0`` was
  configured, and the only caller was a unit test;
* speech spans taken from *captions*, which are opt-in, so a video without
  captions had no ducking whatever the configuration said;
* and silence treated as a defect in two places (a QA check and a scoring
  penalty) and as a tool in none.

This module decides. It renders nothing: it produces a plan the mixing layer
turns into a filter graph, which keeps the decision testable without decoding
a single sample.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, NamedTuple

from backend.core.logging import LogChannel, get_logger
from backend.semantic.reader import Level, SemanticReader

logger = get_logger("audio.director", LogChannel.RENDERING)

#: Which shelf a section's level asks for. The shelves are the user's own
#: folders; a session with none falls back to whatever files exist.
SHELF_FOR: Final[dict[Level, str]] = {
    "calm": "low",
    "normal": "low",
    "tension": "build",
    "high": "peak",
    "climax": "peak",
}

#: A section shorter than this is not worth its own bed: swapping the music
#: every four seconds is not scoring, it is fidgeting.
MIN_SECTION_SECONDS: Final[float] = 20.0

#: How long a bed takes to become the next one.
CROSSFADE_SECONDS: Final[float] = 1.5

#: The quiet before a payoff. Long enough to be felt, short enough that it
#: reads as a held breath rather than a dropout.
SILENCE_BEFORE_PAYOFF: Final[float] = 0.8

#: A payoff needs at least this much certainty before the music stops for it.
#: Silence over a beat that turns out to be nothing is just a hole.
SILENCE_MIN_STRENGTH: Final[float] = 0.6

#: The shortest loud stretch worth ducking under. The envelope's own attack
#: (120ms) and release (700ms) together outlast a half-second dip, so a
#: shorter span would render as a ramp down and straight back up -- audible
#: as a wobble in the bed, and never as emphasis.
MIN_DUCK_SECONDS: Final[float] = 1.0

#: Where game audio starts competing with the music bed, on the audio lane's
#: normalised scale. A span begins here -- and, since V2-P2.6, the value that
#: crossed it is kept rather than discarded, because it is the difference
#: between a footstep and an explosion.
LOUD_ENOUGH: Final[float] = 0.75


class LoudEvent(NamedTuple):
    """A stretch where the game is loud, and how loud it got.

    ``peak`` is the highest value the audio lane reached inside the span, on
    the same 0-1 scale as :data:`LOUD_ENOUGH`. It is a *measurement* and not a
    decision: how far the music should step aside for it is decided where all
    the other depths are, against `audio.ducking`.
    """

    start: float
    end: float
    peak: float



@dataclass(frozen=True, slots=True)
class MusicSection:
    """One bed, over one stretch of the video."""

    start_seconds: float
    end_seconds: float
    shelf: str
    level: Level
    crossfade_seconds: float = CROSSFADE_SECONDS

    @property
    def seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True, slots=True)
class PlannedSilence:
    """A deliberate gap in the music.

    Carried explicitly so QA can be told: the ``extreme_silence`` check and
    the dead-time scoring penalty both read silence as a defect, and a system
    that cannot distinguish its own choice from a fault will report itself.
    """

    start_seconds: float
    end_seconds: float
    reason: str
    anchor_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AudioPlan:
    """The soundtrack's shape, before a single filter is written."""

    sections: tuple[MusicSection, ...] = field(default=())
    speech_spans: tuple[tuple[float, float], ...] = field(default=())
    #: Where the game got loud, with the peak that decided each span.
    event_spans: tuple[LoudEvent, ...] = field(default=())
    silences: tuple[PlannedSilence, ...] = field(default=())
    notes: tuple[str, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        return not self.sections and not self.speech_spans and not self.event_spans


def plan_audio(
    *,
    reader: SemanticReader | None,
    duration_seconds: float,
    spoken: Sequence[tuple[float, float]] = (),
    beats: Sequence[tuple[float, float]] = (),
    config: Any,
    style: Any = None,
) -> AudioPlan:
    """Decide what the soundtrack does.

    Args:
        reader: the session's lanes, in TIMELINE seconds. Without one the plan
            is what the mixer already did: no sections, and ducking from
            whatever spans the caller passed.
        spoken: ``(start, end)`` speech, in timeline seconds.
        beats: ``(seconds, strength)`` payoff anchors, for the silences.
        style: the resolved Style Bible entry (V2-P8). Without one the
            constants below apply, which are the values this module has always
            used and the ones the bible's defaults restate.
    """
    notes: list[str] = []
    sections = _sections(reader, duration_seconds, config, notes, style)
    silences = _silences(beats, spoken, config, notes, style)
    return AudioPlan(
        sections=tuple(sections),
        speech_spans=tuple(spoken),
        event_spans=tuple(
            span for span in _event_spans(reader, config) if span.end > span.start
        ),
        silences=tuple(silences),
        notes=tuple(notes),
    )


def _sections(
    reader: SemanticReader | None,
    duration_seconds: float,
    config: Any,
    notes: list[str],
    style: Any = None,
) -> list[MusicSection]:
    """One bed per stretch of the session, merged until each is worth a swap."""
    shortest = float(
        getattr(getattr(style, "audio", None), "min_section_seconds", MIN_SECTION_SECONDS)
    )
    music = config.audio.music
    if reader is None or not music.enabled or not music.change_on_section:
        return []
    shape = list(reader.shape())
    if not shape:
        return []

    merged: list[MusicSection] = []
    for segment in shape:
        shelf = (
            style.shelf_for(segment.level, SHELF_FOR.get(segment.level, "build"))
            if style is not None
            else SHELF_FOR.get(segment.level, "build")
        )
        if merged and (
            merged[-1].shelf == shelf
            or segment.end_seconds - segment.start_seconds < shortest
        ):
            # Same bed, or a stretch too short to be worth changing for.
            previous = merged[-1]
            merged[-1] = MusicSection(
                previous.start_seconds,
                min(segment.end_seconds, duration_seconds),
                previous.shelf,
                previous.level,
            )
            continue
        merged.append(
            MusicSection(
                min(segment.start_seconds, duration_seconds),
                min(segment.end_seconds, duration_seconds),
                shelf,
                segment.level,
            )
        )
    merged = [section for section in merged if section.seconds > 0.5]
    if merged:
        merged[-1] = MusicSection(
            merged[-1].start_seconds, duration_seconds, merged[-1].shelf, merged[-1].level
        )
    notes.append(
        f"music follows the session: {len(merged)} section(s) across "
        f"{len({section.shelf for section in merged})} shelf/shelves"
    )
    return merged


def _event_spans(reader: SemanticReader | None, config: Any) -> list[LoudEvent]:
    """Where game audio is loud enough to duck the music under it.

    The lane, not a list of events: an explosion's *sound* is what competes
    with the bed, and the audio lane is the measurement of exactly that.
    ``game_event_duck_db`` has been configured since Phase 12 with no consumer
    but a test.

    The peak travels with the span (V2-P2.6). It was measured here already --
    the comparison against :data:`LOUD_ENOUGH` is what defines the span -- and
    was then thrown away, so a footstep a hair over the line and a full
    explosion ducked the bed by exactly the same 8 dB. Measured across 1,900
    spans on this machine: 304 sit under 0.80 and 110 are at full scale.
    """
    if reader is None or not config.audio.ducking.enabled:
        return []
    lane = reader.lane("audio")
    step = 1.0 / reader.hz
    spans: list[LoudEvent] = []
    start: float | None = None
    peak = 0.0
    for index, value in enumerate(lane):
        loud = value >= LOUD_ENOUGH
        if loud:
            if start is None:
                start = index * step
                peak = value
            else:
                peak = max(peak, value)
        elif start is not None:
            spans.append(LoudEvent(start, index * step, peak))
            start = None
            peak = 0.0
    if start is not None:
        spans.append(LoudEvent(start, len(lane) * step, peak))
    return [span for span in spans if span.end - span.start >= MIN_DUCK_SECONDS]


def _silences(
    beats: Sequence[tuple[float, float]],
    spoken: Sequence[tuple[float, float]],
    config: Any,
    notes: list[str],
    style: Any = None,
) -> list[PlannedSilence]:
    """A held breath before the strongest payoffs.

    Never under speech: a gap while somebody is talking is a dropout, not a
    beat. And never for a beat the classifier was unsure of -- silence over
    something that turns out to be nothing is just a hole in the soundtrack.
    """
    if not config.audio.music.enabled:
        return []
    taste = getattr(style, "audio", None)
    floor = float(getattr(taste, "silence_min_strength", SILENCE_MIN_STRENGTH))
    lead = float(getattr(taste, "silence_before_payoff", SILENCE_BEFORE_PAYOFF))
    planned: list[PlannedSilence] = []
    for seconds, strength in sorted(beats, key=lambda item: -item[1]):
        if strength < floor:
            continue
        start = seconds - lead
        if start < 0:
            continue
        if any(a < seconds and start < b for a, b in spoken):
            continue
        if any(
            start < item.end_seconds and item.start_seconds < seconds
            for item in planned
        ):
            continue
        planned.append(
            PlannedSilence(
                start_seconds=start,
                end_seconds=seconds,
                reason=f"held for the payoff at {seconds:.1f}s",
                anchor_seconds=seconds,
            )
        )
    planned.sort(key=lambda item: item.start_seconds)
    if planned:
        notes.append(f"{len(planned)} deliberate silence(s) before a payoff")
    return planned


def shelf_directory(root: Path, shelf: str) -> Path:
    """Where a shelf's files live, or the flat directory when it has none."""
    candidate = root / shelf
    return candidate if candidate.is_dir() else root


__all__ = [
    "CROSSFADE_SECONDS",
    "MIN_SECTION_SECONDS",
    "SHELF_FOR",
    "SILENCE_BEFORE_PAYOFF",
    "AudioPlan",
    "MusicSection",
    "PlannedSilence",
    "plan_audio",
    "shelf_directory",
]


#: How far down a planned silence takes the music. Not to absolute zero: a
#: bed that vanishes and returns reads as a fault, one that drops away reads
#: as a decision.
SILENCE_DB: Final[float] = -40.0
