"""How long a shot runs, and why (V2-P3).

P1 made the cut length a function of one thing: the semantic level at the
second the cut starts from. That was the whole difference between a montage
script and an editor, and it measured -- 95 of 95 clips inside their band,
climax shots at a third the length of calm ones.

It was also blind in five ways a person is not. It would end a shot in the
middle of a spoken sentence. It would cut a second before an explosion rather
than on it. It would follow a 0.9s shot with another 0.9s shot until the
sequence read as a stutter. It would cut on a still frame, where the join has
nothing to hide behind. And it treated the last shot of the video like any
other.

So the band is where a decision starts, not where it ends. Each rule below
exists because its absence is visible on screen, each is applied in a fixed
order, and every one that fires is recorded on the shot it changed -- §80's
requirement, and the practical one: nine rounds of the P1 gate were spent
guessing why a number came out the way it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.semantic.reader import Level, SemanticReader

logger = get_logger("editorial.pacing", LogChannel.PIPELINE)

#: A shot that lands within this of a strong event is on the beat. Wider and
#: the cut reads as early; narrower and no real seam ever qualifies.
ON_THE_BEAT_SECONDS: Final[float] = 0.35

#: Below this a shot is short enough that another one just like it reads as a
#: stutter rather than as pace.
STUTTER_SECONDS: Final[float] = 1.2

#: How much a shot grows to break a run of very short ones.
STUTTER_RELIEF: Final[float] = 1.15

#: A cut lands softest where the picture is already moving -- motion masks
#: the join, which is why editors cut on action. On a still frame the same cut
#: announces itself, so a shot ending in stillness is held a little longer to
#: reach something worth cutting on.
STILLNESS_RELIEF: Final[float] = 1.25
#: Below this the picture is effectively still.
STILL_MOTION: Final[float] = 0.25

#: The last shot of the video keeps its tail: an ending that stops is not an
#: ending. Applied only to the clip the story stage marked as such.
ENDING_RELIEF: Final[float] = 1.5


@dataclass(frozen=True, slots=True)
class PacingContext:
    """Everything the length of one shot depends on.

    Assembled by the caller because only the caller knows the edit: the
    engine reads the session, not the timeline it is being cut into.
    """

    #: Source seconds the shot starts from.
    position: float
    #: The session's level there.
    level: Level = "normal"
    #: Sustained pressure, 0..1 -- a session that has been climbing for a
    #: while cuts tighter than one that just spiked.
    tension: float = 0.0
    #: Somebody is talking at ``position``.
    speech: bool = False
    #: Seconds until speech next stops, when it is running.
    speech_ends_in: float = 0.0
    #: Strong events per ten seconds around here.
    event_density: float = 0.0
    #: Seconds to the next strong event, or ``inf``.
    next_event_in: float = float("inf")
    #: What this clip is doing in the story: hook, body, ending.
    role: str = "body"
    #: How long the previous shot ran, or 0 for the first.
    previous_length: float = 0.0
    #: How much the picture is moving where the shot would end, 0..1.
    motion_at_cut: float = 0.5


@dataclass(frozen=True, slots=True)
class ShotLength:
    """A length and the rules that produced it (§80)."""

    seconds: float
    level: Level
    rules: tuple[str, ...] = field(default=())

    def __float__(self) -> float:
        return self.seconds


def shot_length(context: PacingContext, config: Any) -> ShotLength:
    """How long the shot starting at ``context.position`` may run.

    The band for the level is the starting point. Every rule after it names
    itself on the way past, so a finished video can answer "why is this shot
    four seconds" with the list rather than with a shrug.
    """
    pacing = config.editorial.pacing
    band = getattr(pacing.bands, context.level)
    seconds = float(band.max)
    rules: list[str] = [f"{context.level} band caps at {band.max:.1f}s"]

    # Sustained tension tightens within the band rather than across it: a
    # session that has been climbing for half a minute is not the same as one
    # that spiked once, and the level alone cannot tell them apart.
    if context.tension > 0.6 and seconds > band.min:
        tightened = max(band.min, seconds * (1.0 - 0.25 * (context.tension - 0.6) / 0.4))
        if tightened < seconds:
            seconds = tightened
            rules.append(f"sustained tension {context.tension:.2f} tightens toward {band.min:.1f}s")

    # Never end a shot inside a sentence. This is the one rule that may
    # lengthen past the band: half a sentence is a defect the viewer hears,
    # and no pacing target is worth it.
    if context.speech and context.speech_ends_in > seconds:
        seconds = context.speech_ends_in
        rules.append(f"held {seconds:.1f}s to finish a sentence")

    # Land on the beat. A cut a beat early is the most common amateur tell in
    # a gaming edit: the explosion happens in the next shot, not this one.
    if context.next_event_in <= seconds + ON_THE_BEAT_SECONDS and not context.speech:
        landed = max(0.1, context.next_event_in)
        if abs(landed - seconds) > 1e-6:
            seconds = landed
            rules.append(f"landed on an event {context.next_event_in:.2f}s away")

    # Break a stutter. Two machine-gun shots are pace; five are a glitch.
    if 0.0 < context.previous_length < STUTTER_SECONDS and seconds < STUTTER_SECONDS:
        seconds *= STUTTER_RELIEF
        rules.append("lengthened to break a run of very short shots")

    # Cut on movement. A join on a still frame is a join the viewer sees.
    if context.motion_at_cut < STILL_MOTION and not context.speech:
        seconds *= STILLNESS_RELIEF
        rules.append("held for something to cut on; the picture is still here")

    if context.role == "hook":
        # The opening has fifteen seconds to earn the rest, and this is the
        # one place the doctrine asks for speed over comfort.
        seconds = max(band.min, seconds * 0.75)
        rules.append("tightened for the hook")
    elif context.role == "ending":
        seconds *= ENDING_RELIEF
        rules.append("the last shot keeps its tail")

    floor = float(pacing.min_piece_seconds)
    if seconds < floor:
        seconds = floor
        rules.append(f"raised to the {floor:.1f}s readability floor")

    return ShotLength(seconds=seconds, level=context.level, rules=tuple(rules))


def context_at(
    position: float,
    reader: SemanticReader | None,
    *,
    role: str = "body",
    previous_length: float = 0.0,
    events: tuple[float, ...] = (),
    probe_seconds: float = 2.0,
) -> PacingContext | None:
    """Read the session at ``position``, or ``None`` without a reader.

    The probe is a window rather than an instant because a single half-second
    bin is a sample: P1's gate spent four rounds on a grader that read one bin
    and called it the stretch.
    """
    if reader is None:
        return None
    end = position + probe_seconds
    level = reader.level_for(position, end)
    speech_now = reader.value_at("speech", position) >= 0.5
    speech_ends_in = _speech_ends_in(reader, position) if speech_now else 0.0
    ahead = [event for event in events if event > position]
    return PacingContext(
        position=position,
        level=level,
        tension=reader.value_at("tension", position),
        speech=speech_now,
        speech_ends_in=speech_ends_in,
        event_density=_density(events, position),
        next_event_in=(ahead[0] - position) if ahead else float("inf"),
        role=role,
        previous_length=previous_length,
        # Read where the shot would plausibly end rather than where it starts:
        # the cut is what lands on the frame, not the opening.
        motion_at_cut=reader.value_at("motion", end),
    )


def _speech_ends_in(reader: SemanticReader, position: float, *, limit: float = 12.0) -> float:
    """Seconds until the speech lane falls silent, capped.

    Capped because a lane that never falls silent -- a commentary track, a
    long monologue -- would otherwise hold one shot for the whole recording.
    Past the cap the sentence rule stops applying and the band takes over.
    """
    lane = reader.lane("speech")
    step = 1.0 / reader.hz
    index = max(0, min(len(lane) - 1, int(position * reader.hz)))
    stop = min(len(lane), index + int(limit * reader.hz) + 1)
    for cursor in range(index, stop):
        if lane[cursor] < 0.5:
            return (cursor - index) * step
    return 0.0


def _density(events: tuple[float, ...], position: float, *, window: float = 10.0) -> float:
    """Strong events per ten seconds around ``position``."""
    if not events:
        return 0.0
    half = window / 2.0
    return float(
        sum(1 for event in events if position - half <= event <= position + half)
    )


def describe(shot: ShotLength) -> str:
    """One line for a plan or a log (§80)."""
    return f"{shot.seconds:.2f}s ({shot.level}): " + "; ".join(shot.rules)


__all__ = [
    "ON_THE_BEAT_SECONDS",
    "PacingContext",
    "ShotLength",
    "context_at",
    "describe",
    "shot_length",
]
