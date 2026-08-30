"""Context expansion (SPEC section 29).

    Every candidate moment gets pre-roll + main event + post-roll (e.g. −20 s /
    +20 s). **The exact duration must be adaptive.**

The emphasis is the spec's, and it is the entire design constraint. A fixed
±20 seconds is what makes an automated highlight video recognisable as one:
every clip opens on twenty seconds of somebody walking, and ends twenty seconds
after the interesting part is over.

So expansion here is bounded by a base, allowed to grow to a maximum, and then
**snapped to something real**:

* a **scene boundary** — a cut is where the picture already changed, so a clip
  that starts there looks deliberate;
* a **speech boundary** — starting mid-sentence is the single most obvious
  artefact of automated editing, and ending mid-sentence is worse.

Both are opportunistic. If nothing suitable is within the snap window, the base
roll stands rather than being stretched to reach something distant, because a
clip that opens forty seconds early to catch a scene cut has traded one
artefact for a longer one.

How much roll a moment gets before snapping depends on what kind of moment it
is. A clutch needs the tension that led into it; a funny moment needs the beat
after it far more than the minute before.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ai.providers.base import TranscriptSegment
from backend.analysis.audio_events import AudioEvent
from backend.analysis.scenes import Scene
from backend.config.schema import ContextExpansionConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import AudioEventType, MomentType
from backend.moments.formation import Moment, replace_moment

logger = get_logger("moments.context", LogChannel.PIPELINE)

#: Multipliers on the base roll, per moment type. A clutch is *made* by its
#: build-up; a funny moment is made by the beat after it. A fixed roll treats
#: these as the same thing, and they are not.
ROLL_SHAPE: Final[dict[MomentType, tuple[float, float]]] = {
    MomentType.CLUTCH: (1.8, 1.0),
    MomentType.TENSION: (1.8, 0.8),
    MomentType.COMEBACK: (2.0, 1.2),
    MomentType.BOSS: (1.5, 1.2),
    MomentType.EPIC: (1.3, 1.2),
    MomentType.OUTPLAY: (1.3, 1.1),
    MomentType.FUNNY: (0.7, 1.6),
    MomentType.FAIL: (0.8, 1.5),
    MomentType.REACTION: (0.6, 1.5),
    MomentType.SURPRISE: (0.9, 1.3),
    MomentType.VICTORY: (1.2, 1.4),
    MomentType.DEFEAT: (1.2, 1.2),
}
_DEFAULT_SHAPE: Final[tuple[float, float]] = (1.0, 1.0)


@dataclass(frozen=True, slots=True)
class ExpansionSources:
    """What the expansion may snap to. All optional.

    A recording with no scene detection and no transcript still expands — it
    just uses the base roll, which is §95's graceful degradation applied to a
    detail nobody would otherwise notice was missing.
    """

    scenes: Sequence[Scene] = ()
    transcript: Sequence[TranscriptSegment] = ()
    audio_events: Sequence[AudioEvent] = ()
    duration_seconds: float = 0.0
    #: V2-P2. With it, a moment's context is its own build-up and come-down
    #: rather than a constant chosen by its type. Without it, the type-shaped
    #: rolls below, which is what every edit before this used.
    reader: Any | None = None


def expand(
    moments: Sequence[Moment], config: ContextExpansionConfig, sources: ExpansionSources
) -> list[Moment]:
    """Give every moment its viewing span (§29)."""
    expanded = [_expand_one(moment, config, sources) for moment in moments]
    snapped = sum(1 for moment in expanded if moment.metadata.get("snapped"))
    logger.info(
        "Expanded moment context",
        extra={
            "moments": len(expanded),
            "snapped_to_boundary": snapped,
            "mean_context_seconds": round(
                sum(moment.context_duration for moment in expanded) / max(len(expanded), 1), 2
            ),
        },
    )
    return expanded


def _semantic_roll(
    moment: Moment, config: ContextExpansionConfig, sources: ExpansionSources
) -> tuple[float, float] | None:
    """Pre- and post-roll taken from the moment's own shape, or ``None``.

    The type-shaped rolls this replaces are a reasonable guess -- a defeat
    gets more lead-in than a rare find -- but they are the same guess for
    every defeat in every session. A moment's build-up has an actual length,
    and the lanes know it: read the shape across the widest window the
    configuration would ever allow, and let the phases say where the climb
    starts and the come-down ends.

    Bounded by the same ceilings as before, so this can lengthen a clip only
    within limits §29 already set. Returns ``None`` when the shape is not
    measurable, which sends the caller back to the old behaviour rather than
    to a worse guess.
    """
    from backend.moments.phases import classify_phases

    reader = sources.reader
    if reader is None:
        return None
    widest_start = max(0.0, moment.start_seconds - config.max_pre_roll_seconds)
    widest_end = moment.end_seconds + config.max_post_roll_seconds
    if sources.duration_seconds:
        widest_end = min(widest_end, sources.duration_seconds)
    phases = classify_phases(
        reader, start_seconds=widest_start, end_seconds=widest_end
    )
    if len(phases) < 2 or phases[0].name == "unknown":
        return None

    # The lead-in reaches back to where the build began, never past it into
    # the previous situation.
    build = next(
        (
            phase
            for phase in phases
            if phase.name in ("setup", "anticipation", "escalation")
        ),
        None,
    )
    pre = 0.0
    if build is not None and build.start_seconds < moment.start_seconds:
        pre = min(
            moment.start_seconds - build.start_seconds, config.max_pre_roll_seconds
        )

    # The tail runs to the end of the reaction, and stops there: `unknown`
    # and `dead` after it are footage, not the moment landing.
    reaction = next(
        (phase for phase in reversed(phases) if phase.name == "reaction"), None
    )
    post = 0.0
    if reaction is not None and reaction.end_seconds > moment.end_seconds:
        post = min(
            reaction.end_seconds - moment.end_seconds, config.max_post_roll_seconds
        )
    return pre, post


def _expand_one(
    moment: Moment, config: ContextExpansionConfig, sources: ExpansionSources
) -> Moment:
    measured = _semantic_roll(moment, config, sources)
    if measured is not None:
        pre_roll, post_roll = measured
        shaped_from = "shape"
    else:
        pre_shape, post_shape = ROLL_SHAPE.get(moment.moment_type, _DEFAULT_SHAPE)
        pre_roll = min(
            config.base_pre_roll_seconds * pre_shape, config.max_pre_roll_seconds
        )
        post_roll = min(
            config.base_post_roll_seconds * post_shape, config.max_post_roll_seconds
        )
        shaped_from = "type"

    limit = sources.duration_seconds or moment.end_seconds + post_roll
    start = max(moment.start_seconds - pre_roll, 0.0)
    end = min(moment.end_seconds + post_roll, limit)

    notes: list[str] = []
    if config.snap_to_scene_boundary and sources.scenes:
        snapped_start = _snap_to_scene(start, sources.scenes, config.snap_window_seconds)
        if snapped_start is not None and snapped_start <= moment.start_seconds:
            start = snapped_start
            notes.append("start snapped to a scene boundary")

    if config.extend_to_speech_boundary and sources.transcript:
        sentence = _sentence_at(start, sources.transcript)
        if sentence is not None:
            # Bounded by the configured pre-roll ceiling rather than by the
            # snap window: the window governs how far to reach for a *scene*
            # boundary, which is cosmetic, while opening mid-word is audible.
            # Reaching a few seconds further to avoid it is worth it.
            earliest = moment.start_seconds - config.max_pre_roll_seconds
            if sentence.start >= earliest:
                start = sentence.start
                notes.append("start moved back to the beginning of a sentence")
            elif sentence.end < moment.start_seconds:
                # The sentence began before any allowed lead-in, so start after
                # it instead. Half a sentence is worse than none of it.
                start = sentence.end
                notes.append("start moved past a sentence that began too early")

        speech_end = _speech_end(end, sources.transcript, config.snap_window_seconds)
        if speech_end is not None and speech_end > end:
            end = min(
                speech_end, moment.end_seconds + config.max_post_roll_seconds, limit
            )
            notes.append("end extended to finish a sentence")

    start = min(start, moment.start_seconds)
    end = max(end, moment.end_seconds)

    return _with_metadata(
        moment.with_context(start, end),
        {
            "pre_roll_seconds": round(moment.start_seconds - start, 3),
            "post_roll_seconds": round(end - moment.end_seconds, 3),
            "roll_from": shaped_from,
            "snapped": bool(notes),
            "context_notes": notes,
        },
    )


def _snap_to_scene(
    target: float, scenes: Sequence[Scene], window: float
) -> float | None:
    """Return the nearest scene boundary within ``window``, if any.

    A cut is where the picture already changed, so a clip that begins there
    reads as an edit somebody made rather than an arbitrary offset.
    """
    boundaries = [scene.start_seconds for scene in scenes]
    nearest = min(boundaries, key=lambda value: abs(value - target), default=None)
    if nearest is None or abs(nearest - target) > window:
        return None
    return nearest


def _sentence_at(
    target: float, transcript: Sequence[TranscriptSegment]
) -> TranscriptSegment | None:
    """The sentence ``target`` falls inside, if any.

    Opening a clip mid-word is the most audible artefact automated editing
    produces -- more noticeable than a hard cut, because the ear expects speech
    to begin at a word.
    """
    return next(
        (segment for segment in transcript if segment.start < target < segment.end), None
    )


def _speech_end(
    target: float, transcript: Sequence[TranscriptSegment], window: float
) -> float | None:
    """If ``target`` lands inside a sentence, return where that sentence ends."""
    for segment in transcript:
        if segment.start < target < segment.end and segment.end - target <= window:
            return segment.end
    return None


def silence_boundaries(
    audio_events: Sequence[AudioEvent], *, minimum_seconds: float = 0.5
) -> list[float]:
    """Instants where a silence begins -- natural places to cut (§18 → §29).

    Offered for callers that have audio events but no transcript: silence is a
    weaker boundary than a sentence end, but a far better one than an arbitrary
    offset.
    """
    return [
        event.start_seconds
        for event in audio_events
        if event.event_type is AudioEventType.SILENCE and event.duration >= minimum_seconds
    ]


def _with_metadata(moment: Moment, extra: dict) -> Moment:
    return replace_moment(moment, metadata={**moment.metadata, **extra})


__all__ = ["ROLL_SHAPE", "ExpansionSources", "expand", "silence_boundaries"]
