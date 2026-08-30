"""The contract every stage reads the session through.

The Semantic Timeline existed before this file and almost nothing used it:
six lanes were computed, one was read, and the three stages that did read it
imported the builder directly. So the lanes could not change shape without
rewriting every caller, and a lane nobody consumed looked identical to a lane
that mattered.

This is the seam. Consumers depend on :class:`SemanticReader`; only the
builder and the store depend on the concrete timeline. And every lane named in
:data:`LANES` is either read by a stage or listed in :data:`AWAITING_CONSUMER`
with the phase that will read it -- the same rule P0 applied to configuration,
for the same reason: a computed thing nobody uses is indistinguishable from a
broken one until someone needs it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

#: Heat levels, ascending. A window is graded into exactly one of these.
Level = Literal["calm", "normal", "tension", "high", "climax"]
LEVELS: Final[tuple[Level, ...]] = ("calm", "normal", "tension", "high", "climax")

#: Every lane the builder produces, and what each one means.
#:
#: * ``intensity``     -- the weighted fusion; the lane levels are graded from
#: * ``tension``       -- sustained pressure: an EMA over intensity
#: * ``motion``        -- how much the picture is moving, ranked within the session
#: * ``audio``         -- loudness of what is happening, ranked within the session
#: * ``events``        -- named game events, weighted by importance, with decay
#: * ``speech``        -- 1.0 where somebody is talking. Deliberately NOT fused
#:   into intensity: a quiet stretch where the player explains something is not
#:   a climax, and treating speech as heat would say it is.
#: * ``scene_changes`` -- an impulse at each detected shot change
#: * ``novelty``       -- how unlike the preceding minutes this stretch looks
#: * ``dead_zones``    -- 1.0 where the screen is corroborated non-gameplay
LANES: Final[tuple[str, ...]] = (
    "intensity",
    "tension",
    "motion",
    "audio",
    "events",
    "speech",
    "scene_changes",
    "novelty",
    "dead_zones",
)

#: Lanes with no consumer yet, and the phase that will read them. A lane may
#: sit here; it may not sit nowhere. Delete the entry when the consumer lands.
AWAITING_CONSUMER: Final[dict[str, str]] = {
    # One entry, and it took a test to get here. "audio" sat in this register
    # for a whole phase after P5's audio director began reading it; "events"
    # was listed as awaiting P4 while P7's fatigue check was already reading
    # it. Both were removed by the test below, which is the point of it: a
    # register of what has not been built yet is worth nothing if it does not
    # notice when something gets built.
    #
    # Novelty is genuinely unread. P4 was going to use it for repetition and
    # did not; P7 answers that question from what the vision model saw in the
    # finished render instead, which is a better source for it.
    "novelty": "P8 style bible (how much a channel repeats itself)",
}


@dataclass(frozen=True)
class ShapeSegment:
    """One run of a single level -- the session's form at a glance (§80)."""

    start_seconds: float
    end_seconds: float
    level: Level

    @property
    def seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@runtime_checkable
class SemanticReader(Protocol):
    """What a consumer may assume about the session, and nothing more.

    Deliberately read-only and lane-agnostic: a stage asks for the lane it
    needs by name, so adding a tenth lane costs no caller a line.
    """

    media_id: str
    hz: int
    duration_s: float

    def lane(self, name: str) -> Sequence[float]:
        """The whole lane, one value per bin. Unknown names raise."""
        ...

    def window(self, name: str, start: float, end: float) -> Sequence[float]:
        """The bins of ``name`` covering ``[start, end]``."""
        ...

    def value_at(self, name: str, seconds: float) -> float:
        """One lane's value at one instant."""
        ...

    def intensity_between(self, start: float, end: float) -> float:
        """The stretch's heat: mean carries it, the peak keeps a spike alive."""
        ...

    def level_for(self, start: float, end: float) -> Level:
        """The stretch's level, graded within this session's own range."""
        ...

    def shape(self, *, min_segment: float | None = None) -> Sequence[ShapeSegment]:
        """Level runs, shorter ones merged. ``min_segment`` overrides the
        configured floor: the narrative shape names sections, pacing wants a
        finer one."""
        ...

    def summary(self) -> list[dict[str, Any]]:
        """The shape as plain rows, for a report or a job result (§80)."""
        ...


__all__ = [
    "AWAITING_CONSUMER",
    "LANES",
    "LEVELS",
    "Level",
    "SemanticReader",
    "ShapeSegment",
]
