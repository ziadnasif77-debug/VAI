"""Analysis: what can be measured from a recording (Phase 3+, SPEC §14-§20).

The layering here is deliberate and worth keeping:

* :mod:`~backend.analysis.signal` turns audio into a feature table. It streams,
  so an eight-hour recording never enters memory (§7).
* :mod:`~backend.analysis.audio_events` turns features into observations --
  silence, spikes, onsets, voice activity (§18).
* :mod:`~backend.analysis.reactions` reads the microphone track independently
  of the game (§19) and correlates what it finds with gameplay audio (§20).

Detectors describe **signals**, not meanings. Whether an onset was a gunshot,
or raised voice was excitement rather than anger, is decided later by
correlating several sources (§27) -- a detector that overstated its certainty
here would outvote the ones that actually know.
"""

from backend.analysis.audio_events import (
    GAMEPLAY,
    MICROPHONE,
    AudioEvent,
    LoudnessSummary,
    detect_audio_events,
    measure_loudness,
)
from backend.analysis.reactions import ReactionCandidate, detect_reactions
from backend.analysis.signal import AudioFeatures, analyse_stream, read_windows

__all__ = [
    "GAMEPLAY",
    "MICROPHONE",
    "AudioEvent",
    "AudioFeatures",
    "LoudnessSummary",
    "ReactionCandidate",
    "analyse_stream",
    "detect_audio_events",
    "detect_reactions",
    "measure_loudness",
    "read_windows",
]
