"""Player reactions and their correlation with gameplay (SPEC sections 19, 20).

§19 is the premise: when the microphone was recorded separately, it must be
analysed independently, because "a game explosion and a player screaming carry
very different semantic values". §20 is the consequence: find the player's
reactions and line them up with what happened in the game.

**What this can honestly detect from a waveform.** Three shapes are separable
acoustically, and each is separable for a reason that survives scrutiny:

* **Laughter** modulates. Its amplitude envelope pulses at roughly 3-8 Hz, and
  nothing else a player does sounds like that. Detecting it needs a finer
  envelope than the main analysis pass provides, so candidate spans are re-read
  at a 25 ms hop -- a few seconds of audio, not the whole stream.
* **A scream** is loud and bright: far above the speaker's own baseline, with
  the spectral centroid pushed up, and sustained rather than pulsed.
* **Raised, animated speech** is elevated without being either.

**What it cannot.** Anger, fear, disappointment, confusion -- §20 lists them,
and no level curve distinguishes disappointment from confusion. Those need the
words (§14) and the situation (§21), so this module produces candidates with
the coarse type it can defend and leaves the rest to correlation and, later,
the LLM. A confident wrong label is worse than an honest coarse one: §27 lets
several detectors agree, and a detector that overstates its certainty outvotes
the ones that actually know.

Correlation is what turns a candidate into evidence. A shout on its own is a
person shouting; a shout 1.2 seconds after an explosion is a reaction, and the
offset is recorded so the moment detector can use it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np

from backend.analysis.audio_events import MICROPHONE, AudioEvent, TrackRole
from backend.analysis.signal import AudioFeatures, read_windows, rolling_baseline
from backend.config.schema import AnalysisConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import AudioEventType, ReactionType

logger = get_logger("analysis.reactions", LogChannel.PIPELINE)

#: Envelope sampling for the fine pass. A 25 ms hop samples the envelope at
#: 40 Hz, which resolves modulation up to 20 Hz -- comfortably above the
#: laughter band. The main pass runs at a 0.25 s hop, i.e. 4 Hz, which cannot
#: represent a 5 Hz pulse at all; running laughter detection on it would not be
#: inaccurate so much as meaningless.
ENVELOPE_WINDOW_SECONDS: Final[float] = 0.05
ENVELOPE_HOP_SECONDS: Final[float] = 0.025

#: Laughter's amplitude modulation band.
LAUGH_BAND_HZ: Final[tuple[float, float]] = (3.0, 8.0)
#: How much of the envelope's variation must sit in that band.
LAUGH_MODULATION_DEPTH: Final[float] = 0.22

#: A scream sits this far above the speaker's own baseline, and this bright.
SCREAM_LEVEL_ABOVE_BASELINE_DB: Final[float] = 12.0
SCREAM_CENTROID_HZ: Final[float] = 700.0

#: Shortest span worth classifying. Below this there is not enough envelope to
#: measure modulation, and a single loud frame is a click, not a reaction.
MIN_REACTION_SECONDS: Final[float] = 0.35

#: Correlation with a gameplay event raises confidence by this much, capped at
#: one. Deliberately additive and modest: co-occurrence is corroboration, not
#: proof, and two weak signals agreeing should not become a certainty.
CORRELATION_CONFIDENCE_BONUS: Final[float] = 0.2

#: Gameplay event types worth correlating against. Silence and speech are
#: states; spikes and transients are things that happened.
_CORRELATABLE: Final[frozenset[AudioEventType]] = frozenset(
    {AudioEventType.SPIKE, AudioEventType.TRANSIENT, AudioEventType.EXPLOSION,
     AudioEventType.GUNSHOT}
)


@dataclass(frozen=True, slots=True)
class ReactionCandidate:
    """One reaction the microphone track suggests (§20)."""

    reaction_type: ReactionType
    start_seconds: float
    end_seconds: float
    confidence: float
    #: How far above the speaker's own baseline this passage sat.
    intensity_db: float
    #: Seconds from the correlated gameplay event to this reaction. Positive
    #: means the reaction followed, which is the ordering that makes sense;
    #: a negative offset means the player reacted before the sound, which is
    #: possible when they saw it coming.
    correlation_offset: float | None = None
    correlated_event_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0

    @property
    def is_correlated(self) -> bool:
        return self.correlation_offset is not None

    def as_audio_event(self) -> AudioEvent:
        """Represent the candidate as an audio event, for §45 persistence.

        Reactions live in ``audio_events`` rather than a table of their own:
        they are observations on the microphone track, and §27 wants every
        detector's observations in one place to be correlated.
        """
        return AudioEvent(
            event_type=_EVENT_TYPE_FOR.get(self.reaction_type, AudioEventType.SPEECH),
            start_seconds=self.start_seconds,
            end_seconds=self.end_seconds,
            track_role=MICROPHONE,
            confidence=self.confidence,
            metadata={
                "reaction_type": self.reaction_type.value,
                "intensity_db": round(self.intensity_db, 2),
                "correlation_offset": self.correlation_offset,
                "correlated_event_type": self.correlated_event_type,
                **self.metadata,
            },
        )


#: How a reaction is stored as an audio event. Only laughter and screaming have
#: their own §18 audio event types; everything else is speech that was loud.
_EVENT_TYPE_FOR: Final[dict[ReactionType, AudioEventType]] = {
    ReactionType.LAUGH: AudioEventType.LAUGH,
    ReactionType.SCREAM: AudioEventType.SHOUT,
}


def detect_reactions(
    features: AudioFeatures,
    events: list[AudioEvent],
    config: AnalysisConfig,
    *,
    gameplay_events: list[AudioEvent] | None = None,
    track_role: TrackRole = MICROPHONE,
) -> list[ReactionCandidate]:
    """Find reactions on a microphone track and correlate them with gameplay.

    Args:
        features: the microphone track's feature table.
        events: audio events already detected on that track.
        config: the ``analysis`` section; supplies both windows and the
            confidence floor.
        gameplay_events: events from the game audio track. Absent means no
            correlation is possible, which is the single-track case -- the
            candidates are still reported, just without corroboration.

    Returns candidates at or above ``analysis.reactions.min_confidence``, in
    chronological order.
    """
    if not config.reactions.enabled or features.is_empty:
        return []

    baseline = rolling_baseline(features.rms_db, _baseline_windows(features))
    spans = _candidate_spans(events, config)

    candidates: list[ReactionCandidate] = []
    for start, end in spans:
        candidate = _classify(features, baseline, start, end)
        if candidate is None:
            continue
        candidate = _correlate(candidate, gameplay_events or [], config)
        if candidate.confidence >= config.reactions.min_confidence:
            candidates.append(candidate)

    candidates.sort(key=lambda item: item.start_seconds)
    logger.info(
        "Detected reaction candidates",
        extra={
            "path": str(features.path),
            "track_role": track_role,
            "candidates": len(candidates),
            "correlated": sum(1 for item in candidates if item.is_correlated),
        },
    )
    return candidates


# ---------------------------------------------------------------------------
# candidate selection
# ---------------------------------------------------------------------------


def _candidate_spans(
    events: list[AudioEvent], config: AnalysisConfig
) -> list[tuple[float, float]]:
    """Spans on the microphone worth classifying.

    Driven by **spikes**, not by voice activity. A reaction is a rise above the
    speaker's own baseline, and the speech detector routinely marks whole
    minutes at once -- a quiet room tone passes its level and spectrum tests.
    Merging the two would dissolve every localised burst into one span covering
    the recording, which is precisely what a reaction is not.

    Voice activity is used as a **gate** instead: a spike that overlaps no
    voiced region is a desk knock or a door, not the player reacting.

    A reaction that never rises above its own background is not detectable at
    this level, and is deliberately not guessed at.
    """
    spikes = [
        event
        for event in events
        if event.event_type is AudioEventType.SPIKE
        and event.duration >= MIN_REACTION_SECONDS
    ]
    if not spikes:
        return []

    voiced = [event for event in events if event.event_type is AudioEventType.SPEECH]
    limit = config.microphone.reaction_window_seconds

    spans: list[tuple[float, float]] = []
    for event in sorted(spikes, key=lambda item: item.start_seconds):
        if voiced and not any(_overlaps(event, region) for region in voiced):
            continue
        # A reaction is a burst, not a monologue: a spike that runs longer than
        # the configured window is trimmed to it.
        start, end = event.start_seconds, min(event.end_seconds, event.start_seconds + limit)
        if spans and start <= spans[-1][1]:
            previous_start, previous_end = spans[-1]
            spans[-1] = (previous_start, max(previous_end, end))
        else:
            spans.append((start, end))
    return spans


def _overlaps(first: AudioEvent, second: AudioEvent) -> bool:
    return first.start_seconds < second.end_seconds and second.start_seconds < first.end_seconds


def _classify(
    features: AudioFeatures, baseline: np.ndarray, start: float, end: float
) -> ReactionCandidate | None:
    """Assign the coarse reaction type this span can support."""
    if end - start < MIN_REACTION_SECONDS:
        return None

    indices = np.flatnonzero((features.times >= start) & (features.times < end))
    if indices.size == 0:
        return None

    level = float(np.max(features.rms_db[indices]))
    above = level - float(np.median(baseline[indices]))
    centroid = float(np.median(features.spectral_centroid[indices]))

    modulation_hz, depth = _modulation(features.path, start, end)
    low, high = LAUGH_BAND_HZ

    metadata: dict[str, Any] = {
        "modulation_hz": round(modulation_hz, 2),
        "modulation_depth": round(depth, 3),
        "centroid_hz": round(centroid, 1),
        "level_db": round(level, 2),
    }

    if low <= modulation_hz <= high and depth >= LAUGH_MODULATION_DEPTH:
        return ReactionCandidate(
            reaction_type=ReactionType.LAUGH,
            start_seconds=start,
            end_seconds=end,
            # Modulation depth is the evidence, so it is what sets confidence.
            confidence=min(0.45 + depth, 0.9),
            intensity_db=above,
            metadata=metadata,
        )

    if above >= SCREAM_LEVEL_ABOVE_BASELINE_DB and centroid >= SCREAM_CENTROID_HZ:
        return ReactionCandidate(
            reaction_type=ReactionType.SCREAM,
            start_seconds=start,
            end_seconds=end,
            confidence=min(0.45 + (above - SCREAM_LEVEL_ABOVE_BASELINE_DB) / 30.0, 0.9),
            intensity_db=above,
            metadata=metadata,
        )

    if above >= 4.0:
        return ReactionCandidate(
            reaction_type=ReactionType.EXCITEMENT,
            start_seconds=start,
            end_seconds=end,
            # The catch-all, and priced accordingly: raised voice with no
            # distinguishing shape. It clears the default floor only once
            # something in the game corroborates it.
            confidence=0.45,
            intensity_db=above,
            metadata=metadata,
        )
    return None


def _modulation(path: Path, start: float, end: float) -> tuple[float, float]:
    """Dominant envelope modulation frequency and its depth over a span.

    Returns ``(0.0, 0.0)`` when the span is too short to measure -- a fifth of
    a second cannot show a 4 Hz pulse, and reporting a number from it would be
    inventing evidence.
    """
    envelope: list[float] = []
    for window in read_windows(
        path,
        window_seconds=ENVELOPE_WINDOW_SECONDS,
        hop_seconds=ENVELOPE_HOP_SECONDS,
        start=start,
        end=end,
    ):
        samples = window.samples
        envelope.append(float(np.sqrt(np.mean(np.square(samples), dtype=np.float64))))

    values = np.asarray(envelope, dtype=np.float64)
    if values.size < 16:
        return 0.0, 0.0

    centred = values - float(np.mean(values))
    energy = float(np.sum(np.square(centred)))
    if energy <= 1e-12:
        return 0.0, 0.0

    spectrum = np.abs(np.fft.rfft(centred * np.hanning(centred.size)))
    frequencies = np.fft.rfftfreq(centred.size, d=ENVELOPE_HOP_SECONDS)

    low, high = LAUGH_BAND_HZ
    band = np.flatnonzero((frequencies >= low) & (frequencies <= high))
    if band.size == 0:
        return 0.0, 0.0

    total = float(np.sum(spectrum)) + 1e-12
    peak = band[int(np.argmax(spectrum[band]))]
    return float(frequencies[peak]), float(np.sum(spectrum[band]) / total)


def _correlate(
    candidate: ReactionCandidate,
    gameplay_events: list[AudioEvent],
    config: AnalysisConfig,
) -> ReactionCandidate:
    """Attach the nearest gameplay event within the correlation window (§20)."""
    window = config.reactions.correlation_window_seconds
    best: AudioEvent | None = None
    best_offset = 0.0
    for event in gameplay_events:
        if event.event_type not in _CORRELATABLE:
            continue
        offset = candidate.start_seconds - event.midpoint
        # Reactions follow their cause, with a little room for anticipation:
        # a player who saw the grenade land reacts before it goes off.
        if -1.0 <= offset <= window and (best is None or abs(offset) < abs(best_offset)):
            best, best_offset = event, offset

    if best is None:
        return candidate
    return ReactionCandidate(
        reaction_type=candidate.reaction_type,
        start_seconds=candidate.start_seconds,
        end_seconds=candidate.end_seconds,
        confidence=min(candidate.confidence + CORRELATION_CONFIDENCE_BONUS, 1.0),
        intensity_db=candidate.intensity_db,
        correlation_offset=round(best_offset, 3),
        correlated_event_type=best.event_type.value,
        metadata={**candidate.metadata, "correlated_event_confidence": best.confidence},
    )


def _baseline_windows(features: AudioFeatures) -> int:
    from backend.analysis.audio_events import BASELINE_SECONDS
    from backend.analysis.signal import seconds_to_windows

    return seconds_to_windows(BASELINE_SECONDS, features.hop_seconds)


__all__ = [
    "CORRELATION_CONFIDENCE_BONUS",
    "ENVELOPE_HOP_SECONDS",
    "ENVELOPE_WINDOW_SECONDS",
    "LAUGH_BAND_HZ",
    "MIN_REACTION_SECONDS",
    "ReactionCandidate",
    "detect_reactions",
]
