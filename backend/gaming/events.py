"""Game event detection (SPEC sections 21, 23, 26).

Each detector here reads one kind of evidence the pipeline has already produced
— vision labels, OCR text, audio events, player reactions, scene boundaries —
and emits **observations**: "at this instant, this source suggests this kind of
event, this strongly". Nothing here decides that an event happened. That is
:mod:`backend.gaming.correlation`'s job, and the split is deliberate: §27 says
several detectors seeing the same instant become *one* high-confidence moment,
which is only expressible if detectors report separately first.

**§23 governs what may be claimed.** With no game profile there is no kill feed
to read and no victory banner whose wording is known, so a generic detector
cannot honestly say "kill". What it can say is that several independent sources
noticed the same instant, and that is reported as
:attr:`GameEventType.UNEXPECTED_EVENT` — the taxonomy's own name for *something
happened here and we cannot name it*. A profile then turns the same evidence
into a specific type, which is exactly the §22/§23 bargain: a profile improves
accuracy and is never required.

Naming an event the evidence does not support would be worse than not naming
it. A wrong `CLUTCH` reaches the narrative stage as a fact and gets edited into
the video.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ai.providers.base import StoredObservation
from backend.analysis.audio_events import MICROPHONE, AudioEvent
from backend.analysis.reactions import ReactionCandidate
from backend.analysis.scenes import Scene
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import AudioEventType, GameEventType, ReactionType
from backend.gaming.ocr import FrameText
from backend.gaming.profiles import GameProfile

logger = get_logger("gaming.events", LogChannel.PIPELINE)

#: Detector source names, recorded on every event (§26 ``sources``).
VISION: Final[str] = "vision"
OCR: Final[str] = "ocr"
AUDIO: Final[str] = "audio"
MICROPHONE_SOURCE: Final[str] = "microphone"
SCENE: Final[str] = "scene"
PROFILE: Final[str] = "profile"

#: Vision labels that justify a specific event type on their own. Deliberately
#: short: these are screen *states* a model can report without knowing the game,
#: and everything subtler is left to correlation.
_LABEL_EVENTS: Final[dict[str, GameEventType]] = {
    "victory_screen": GameEventType.VICTORY,
    "victory": GameEventType.VICTORY,
    "defeat_screen": GameEventType.DEFEAT,
    "defeat": GameEventType.DEFEAT,
    "low_health": GameEventType.LOW_HEALTH,
    "boss": GameEventType.BOSS_FIGHT,
    "boss_health": GameEventType.BOSS_FIGHT,
}

#: Labels that describe gameplay without naming an event. They become context
#: observations: evidence a fusion rule can read, never a claim on their own.
#: Screen states (``menu``, ``loading``, ``cutscene``) are deliberately absent
#: — those are handled by :mod:`backend.analysis.frame_state`, and letting a
#: menu corroborate an event would be the opposite of what it means.
_CONTEXT_LABELS: Final[frozenset[str]] = frozenset(
    {
        "combat",
        "driving",
        "exploration",
        "navigation",
        "interaction",
        "resource_gathering",
        "quest",
        "dialogue",
        "gameplay",
    }
)

#: Text that means the same thing in most games, for the no-profile path (§23).
#: English only, and only words whose meaning does not depend on the game.
_GENERIC_TEXT_EVENTS: Final[tuple[tuple[str, GameEventType, float], ...]] = (
    (r"\bvictory\b|\byou\s+win\b|\bwinner\b", GameEventType.VICTORY, 0.7),
    (r"\bdefeat\b|\byou\s+(?:lose|died)\b|\bgame\s+over\b", GameEventType.DEFEAT, 0.7),
    (r"\beliminated\b|\bknocked\s+out\b", GameEventType.KILL, 0.55),
    (r"\bmission\s+(?:complete|accomplished)\b", GameEventType.OBJECTIVE, 0.6),
    (r"\bmission\s+failed\b", GameEventType.OBJECTIVE_FAILURE, 0.6),
)

#: Reactions that suggest a moment's character (§20 → §21).
_REACTION_EVENTS: Final[dict[ReactionType, GameEventType]] = {
    ReactionType.LAUGH: GameEventType.FUNNY_MOMENT,
    ReactionType.SCREAM: GameEventType.UNEXPECTED_EVENT,
}


@dataclass(frozen=True, slots=True)
class EventObservation:
    """One detector's suggestion that something happened.

    Not an event. An event is what correlation makes of several of these.
    """

    event_type: GameEventType
    start_seconds: float
    end_seconds: float
    source: str
    confidence: float
    detail: dict[str, Any] = field(default_factory=dict)
    #: Evidence *about* an instant rather than a claim that something happened
    #: there. A frame labelled ``combat`` describes the screen; on its own it
    #: is not an event, and §27 must not let a stream of them become one. It
    #: still travels into the cluster, because Phase 0.2's fusion rules name
    #: instants precisely by reading a description alongside a signal.
    context_only: bool = False

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds


def observations_from_vision(
    observations: Iterable[StoredObservation], *, min_confidence: float = 0.0
) -> list[EventObservation]:
    """Read event suggestions out of the model's frame descriptions (§15 → §21).

    Only labels map to types. The free-text description is evidence a human can
    read and the LLM will use later, but §93 forbids taking a pipeline decision
    from prose, so nothing here parses it.

    Labels that name no event still travel, as **context** observations. The
    prompt asks the model for ten labels; this map knew three of them, so the
    other seven were produced, stored and dropped here — ``combat`` 53 times on
    one recording, the single closest honest signal to a fight that footage
    without a kill feed can offer. A context observation claims nothing on its
    own and cannot become an event by itself; it is what lets Phase 0.2 name an
    instant that a signal and a description describe together.
    """
    found: list[EventObservation] = []
    for item in observations:
        if item.confidence < min_confidence:
            continue
        for label in item.labels:
            key = str(label).strip().lower()
            event_type = _LABEL_EVENTS.get(key)
            if event_type is None and key not in _CONTEXT_LABELS:
                continue
            found.append(
                EventObservation(
                    event_type=event_type or GameEventType.UNEXPECTED_EVENT,
                    start_seconds=item.timestamp,
                    end_seconds=item.timestamp,
                    source=VISION,
                    confidence=item.confidence,
                    detail={"label": key, "description": item.description[:200]},
                    context_only=event_type is None,
                )
            )
    return found


def observations_from_ocr(
    frames: Iterable[FrameText], profile: GameProfile
) -> list[EventObservation]:
    """Read event suggestions out of on-screen text (§25 → §21).

    A profile's rules win when they match: they know this game's wording and
    which box it appears in. The generic patterns are the §23 fallback, and are
    scored lower because "ELIMINATED" on screen is weaker evidence when nobody
    has told us where a kill feed is or what else the game writes there.
    """
    found: list[EventObservation] = []
    for frame in frames:
        for detection in frame.detections:
            rules = profile.rules_for(detection.text, region=detection.region)
            if rules:
                found.extend(
                    EventObservation(
                        event_type=rule.event_type,
                        start_seconds=detection.timestamp,
                        end_seconds=detection.timestamp + rule.duration_seconds,
                        source=PROFILE,
                        # The rule's own worth, tempered by how clearly the text
                        # was read: a rule matching an uncertain reading is an
                        # uncertain event.
                        confidence=rule.confidence * max(detection.confidence, 0.1),
                        detail={
                            "text": detection.text,
                            "region": detection.region,
                            "profile": profile.id,
                        },
                    )
                    for rule in rules
                )
                continue

            for pattern, event_type, weight in _GENERIC_TEXT_EVENTS:
                if re.search(pattern, detection.text, re.IGNORECASE):
                    found.append(
                        EventObservation(
                            event_type=event_type,
                            start_seconds=detection.timestamp,
                            end_seconds=detection.timestamp + 2.0,
                            source=OCR,
                            confidence=weight * max(detection.confidence, 0.1),
                            detail={"text": detection.text, "region": detection.region},
                        )
                    )
    return found


def observations_from_audio(events: Iterable[AudioEvent]) -> list[EventObservation]:
    """Turn audio spikes and onsets into "something happened here" (§18 → §21).

    Always :attr:`UNEXPECTED_EVENT`. A loud transient is a fact about the
    waveform; calling it a gunshot would be a claim about the game that the
    waveform cannot support on its own (§27 exists for exactly this).
    """
    interesting = {AudioEventType.SPIKE, AudioEventType.TRANSIENT}
    return [
        EventObservation(
            event_type=GameEventType.UNEXPECTED_EVENT,
            start_seconds=event.start_seconds,
            end_seconds=event.end_seconds,
            source=MICROPHONE_SOURCE if event.track_role == MICROPHONE else AUDIO,
            confidence=event.confidence,
            detail={"audio_event": event.event_type.value, "track": event.track_role},
        )
        for event in events
        if event.event_type in interesting
    ]


def observations_from_reactions(
    reactions: Iterable[ReactionCandidate],
) -> list[EventObservation]:
    """Turn player reactions into event suggestions (§20 → §21).

    A laugh is the one signal that names a moment's character without knowing
    anything about the game, which is why §20 exists at all.
    """
    found: list[EventObservation] = []
    for reaction in reactions:
        event_type = _REACTION_EVENTS.get(reaction.reaction_type)
        if event_type is None:
            continue
        found.append(
            EventObservation(
                event_type=event_type,
                start_seconds=reaction.start_seconds,
                end_seconds=reaction.end_seconds,
                source=MICROPHONE_SOURCE,
                confidence=reaction.confidence,
                detail={
                    "reaction": reaction.reaction_type.value,
                    "correlated": reaction.is_correlated,
                },
            )
        )
    return found


def observations_from_scenes(
    scenes: Sequence[Scene], *, min_change: float
) -> list[EventObservation]:
    """Turn strong shot changes into weak event suggestions (§17 → §21).

    Weak on purpose. §17 is explicit that boundaries are supporting information,
    so a scene change alone must never become an event — it can only agree with
    something else.
    """
    return [
        EventObservation(
            event_type=GameEventType.UNEXPECTED_EVENT,
            start_seconds=scene.start_seconds,
            end_seconds=scene.start_seconds,
            source=SCENE,
            confidence=0.25,
            detail={"change_score": scene.change_score, "scene_index": scene.index},
        )
        for scene in scenes[1:]
        if scene.change_score is not None and scene.change_score >= min_change
    ]


def observations_from_hud(
    readings: Sequence[Any], profile: GameProfile
) -> list[EventObservation]:
    """Read event suggestions out of HUD state changes (§24 → §21).

    The HUD is the one detector that reads a *number*, and the number is only
    evidence when it moves: a wanted level held at four for six minutes is
    context, and the second it went from two to four is the event. The tracking
    and the rules both live in :mod:`backend.gaming.hud`; this is the adapter
    that puts the result alongside every other detector's, so §27 weighs them
    the same way.
    """
    if not readings or not profile.hud:
        return []

    from backend.gaming.hud import changes_to_events, track

    return [
        EventObservation(
            event_type=item["event_type"],
            start_seconds=float(item["start_seconds"]),
            end_seconds=float(item["end_seconds"]),
            source="hud",
            confidence=float(item["confidence"]),
            detail=dict(item.get("metadata", {})),
        )
        for item in changes_to_events(track(readings), profile)
    ]


def detect(
    *,
    vision: Iterable[StoredObservation] = (),
    ocr_frames: Iterable[FrameText] = (),
    audio: Iterable[AudioEvent] = (),
    reactions: Iterable[ReactionCandidate] = (),
    scenes: Sequence[Scene] = (),
    hud_readings: Sequence[Any] = (),
    narration: Iterable[EventObservation] = (),
    profile: GameProfile,
    vision_min_confidence: float = 0.0,
    scene_min_change: float = 0.0,
) -> list[EventObservation]:
    """Run every detector and return their observations, chronologically."""
    found = [
        *observations_from_vision(vision, min_confidence=vision_min_confidence),
        *observations_from_ocr(ocr_frames, profile),
        *observations_from_audio(audio),
        *observations_from_reactions(reactions),
        *observations_from_scenes(scenes, min_change=scene_min_change),
        *observations_from_hud(hud_readings, profile),
        # Already observations: the narration reader produces them directly,
        # because what it read was a situation rather than a signal (§19).
        *narration,
    ]
    found.sort(key=lambda item: (item.start_seconds, item.source))
    logger.info(
        "Detected event observations",
        extra={
            "observations": len(found),
            "by_source": _tally(found),
            "profile": profile.id,
        },
    )
    return found


def _tally(observations: Sequence[EventObservation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in observations:
        counts[item.source] = counts.get(item.source, 0) + 1
    return counts


__all__ = [
    "AUDIO",
    "MICROPHONE_SOURCE",
    "OCR",
    "PROFILE",
    "SCENE",
    "VISION",
    "EventObservation",
    "detect",
    "observations_from_audio",
    "observations_from_hud",
    "observations_from_ocr",
    "observations_from_reactions",
    "observations_from_scenes",
    "observations_from_vision",
]
