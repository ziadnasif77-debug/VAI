"""What the screen is showing, read from labels already stored (Phase 0.6).

`prompts/vision/frame_description/prompt.md` asks the model for ten labels, and
three of them describe the *screen* rather than the action: ``menu``,
``loading`` and ``cutscene``. The model produces them reliably — 78 ``menu``
and 7 ``loading`` observations on one 77-minute recording — and until this
module existed nothing read them.

The cost of not reading them was measured, twice, in the finished video: the
content QA reported *"40 moment(s) in the edit show a menu or loading screen"*
on that same recording. The evidence to prevent every one of those was already
in the database when the edit was built. This turns it into a signal the
moment layer can act on, so the check in QA becomes what it should be — a
regression detector for footage that got through, not the first line of
defence.

Everything here is deterministic and label-driven. No model runs, nothing is
inferred from prose (§93), and a frame nobody labelled stays
:attr:`FrameState.UNKNOWN` — which counts as gameplay, because an unlabelled
frame is not evidence of a menu.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from ai.providers.base import StoredObservation
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import FrameState

logger = get_logger("analysis.frame_state", LogChannel.PIPELINE)

#: Label to state. Ordered by how much the label claims: a frame labelled both
#: ``menu`` and ``combat`` is showing a menu over gameplay, and the menu is the
#: part that makes it unusable as a highlight.
#:
#: Only labels that **unambiguously** mean the game is not being played map to
#: a non-gameplay state, because the two errors cost differently: a menu that
#: slips through is one bad clip the QA still reports, while gameplay wrongly
#: called a menu is a highlight silently deleted.
#:
#: ``inventory`` is the reason that rule is written down. On real footage the
#: model uses it for *"the HUD shows health and item icons"* while the player
#: is riding across a field — 181 observations on one recording, more than any
#: other label. Calling it a menu rejected 37 of 60 moments in a measurement,
#: nearly all of them ordinary gameplay. It is HUD, not a screen.
_LABEL_STATES: Final[tuple[tuple[str, FrameState], ...]] = (
    ("loading", FrameState.LOADING),
    ("menu", FrameState.MENU),
    ("pause", FrameState.PAUSE),
    ("cutscene", FrameState.CUTSCENE),
    ("transition", FrameState.TRANSITION),
    # Drawn over gameplay rather than instead of it.
    ("inventory", FrameState.HUD_ONLY),
    ("scoreboard", FrameState.HUD_ONLY),
    ("map", FrameState.HUD_ONLY),
    ("gameplay", FrameState.GAMEPLAY),
    ("combat", FrameState.GAMEPLAY),
    ("driving", FrameState.GAMEPLAY),
    ("exploration", FrameState.GAMEPLAY),
    ("navigation", FrameState.GAMEPLAY),
    ("interaction", FrameState.GAMEPLAY),
    ("resource_gathering", FrameState.GAMEPLAY),
    ("quest", FrameState.GAMEPLAY),
    ("dialogue", FrameState.GAMEPLAY),
    ("low_health", FrameState.GAMEPLAY),
    ("victory_screen", FrameState.TRANSITION),
    ("defeat_screen", FrameState.TRANSITION),
)

#: How far either side of a *single* observation its reading is taken to hold.
#: Deliberately small. One frame is evidence about one instant, and the
#: temptation to widen it gets the logic backwards: the vision cascade samples
#: candidates rather than a grid, so a lone reading's neighbours are often far
#: away — which means we know *less* about the seconds around it, not more. A
#: 4-second reach turned one ``loading`` frame into an 8-second block and
#: rejected the only moment in a project.
#:
#: Reach also stops halfway to the neighbouring observation, because a frame
#: cannot speak for time another frame was looked at.
HALF_LIFE_SECONDS: Final[float] = 1.0

#: How far apart two observations of the same state may be and still describe
#: one stretch. Agreement across consecutive samples is what justifies claiming
#: the time between them: three ``menu`` readings a few seconds apart are one
#: menu section, while a single reading is a single frame. Generous, because
#: the cascade's sampling is uneven.
BRIDGE_SECONDS: Final[float] = 12.0


@dataclass(frozen=True, slots=True)
class StateSpan:
    """A stretch of the recording in one state."""

    state: FrameState
    start_seconds: float
    end_seconds: float
    #: How many observations agreed on it. One frame's word is weaker evidence
    #: than four in a row, and the moment layer can weigh that.
    observations: int = 1

    @property
    def duration(self) -> float:
        return max(self.end_seconds - self.start_seconds, 0.0)

    def overlaps(self, start: float, end: float) -> float:
        """Seconds this span shares with ``[start, end]``."""
        return max(0.0, min(self.end_seconds, end) - max(self.start_seconds, start))


def state_for(labels: Iterable[str]) -> FrameState:
    """The state a frame's labels describe.

    The first matching entry of :data:`_LABEL_STATES` wins, and the table is
    ordered so that a non-gameplay label beats a gameplay one on the same
    frame: a paused inventory screen over a firefight is still an inventory
    screen, and cutting it would show the viewer a menu.
    """
    present = {str(label).strip().lower() for label in labels}
    for label, state in _LABEL_STATES:
        if label in present:
            return state
    return FrameState.UNKNOWN


def spans(
    observations: Sequence[StoredObservation],
    *,
    half_life_seconds: float = HALF_LIFE_SECONDS,
    bridge_seconds: float = BRIDGE_SECONDS,
    duration_seconds: float | None = None,
) -> list[StateSpan]:
    """Merge observations into the stretches each state held.

    Consecutive observations agreeing on a state become one span covering the
    time between them — that agreement is the evidence the state persisted. A
    gap wider than ``bridge_seconds`` starts a new span, and a lone reading
    covers only ``half_life_seconds`` either side of itself, because one frame
    is evidence about one instant.
    """
    ordered = sorted(observations, key=lambda item: item.timestamp)
    cap = max(half_life_seconds, 0.0)
    built: list[StateSpan] = []

    for index, item in enumerate(ordered):
        state = state_for(item.observation.labels)
        # Halfway to each neighbour, capped. A frame speaks for the time
        # nobody else looked at, and no further.
        back = cap if index == 0 else min(cap, (item.timestamp - ordered[index - 1].timestamp) / 2)
        forward = (
            cap
            if index == len(ordered) - 1
            else min(cap, (ordered[index + 1].timestamp - item.timestamp) / 2)
        )
        start = max(0.0, item.timestamp - back)
        end = item.timestamp + forward
        if duration_seconds is not None:
            end = min(end, duration_seconds)
        if end <= start:
            continue

        previous = built[-1] if built else None
        if (
            previous is not None
            and previous.state is state
            # Bridged from the *observations*, not from their padded edges:
            # two agreeing readings twelve seconds apart describe one stretch,
            # and the seconds between them are what the agreement buys.
            and item.timestamp - previous.end_seconds <= bridge_seconds
        ):
            built[-1] = StateSpan(
                state=state,
                start_seconds=previous.start_seconds,
                end_seconds=max(previous.end_seconds, end),
                observations=previous.observations + 1,
            )
            continue
        built.append(StateSpan(state=state, start_seconds=start, end_seconds=end))

    return built


def non_gameplay(spans_: Iterable[StateSpan]) -> list[StateSpan]:
    """The spans a highlight must not be made of."""
    return [span for span in spans_ if not span.state.is_gameplay]


def overlap_ratio(start: float, end: float, spans_: Sequence[StateSpan]) -> float:
    """How much of ``[start, end]`` sits inside these spans, 0-1.

    Overlapping spans are counted once: two observations a second apart that
    each claim four seconds either side must not add up to more footage than
    the moment contains.
    """
    length = end - start
    if length <= 0 or not spans_:
        return 0.0

    intervals = sorted(
        (max(span.start_seconds, start), min(span.end_seconds, end))
        for span in spans_
        if span.overlaps(start, end) > 0
    )
    covered = 0.0
    reach = start
    for left, right in intervals:
        if right <= reach:
            continue
        covered += right - max(left, reach)
        reach = right
    return min(covered / length, 1.0)


def longest_overlap_ratio(start: float, end: float, spans_: Sequence[StateSpan]) -> float:
    """The longest *unbroken* stretch of ``[start, end]`` that is not gameplay.

    The measure the rejection rule uses, and the total is not. Scattered single
    frames reading ``menu`` inside continuous action are a transient overlay or
    a misread — the model does report one occasionally — while a moment sitting
    inside one unbroken loading screen is the defect this guard exists for.
    Summing the scattered ones reaches the same fraction as the real block and
    rejects footage that is perfectly good: on the test fixture, where the fake
    provider scatters a menu label through every eighth frame, the total rule
    rejected *every* moment in the project.
    """
    length = end - start
    if length <= 0 or not spans_:
        return 0.0

    intervals = sorted(
        (max(span.start_seconds, start), min(span.end_seconds, end))
        for span in spans_
        if span.overlaps(start, end) > 0
    )
    longest = 0.0
    run_start: float | None = None
    run_end = start
    for left, right in intervals:
        if run_start is None or left > run_end:
            run_start, run_end = left, right
        else:
            run_end = max(run_end, right)
        longest = max(longest, run_end - run_start)
    return min(longest / length, 1.0)


def report(spans_: Sequence[StateSpan], duration_seconds: float) -> dict[str, float]:
    """Phase 0.5's perception metrics for one recording."""
    if duration_seconds <= 0:
        return {"non_gameplay_ratio": 0.0}
    off = sum(span.duration for span in non_gameplay(spans_))
    counts: dict[str, float] = {"non_gameplay_ratio": round(min(off / duration_seconds, 1.0), 4)}
    for span in spans_:
        key = f"{span.state.value}_seconds"
        counts[key] = round(counts.get(key, 0.0) + span.duration, 2)
    return counts


__all__ = [
    "HALF_LIFE_SECONDS",
    "StateSpan",
    "non_gameplay",
    "overlap_ratio",
    "report",
    "spans",
    "state_for",
]
