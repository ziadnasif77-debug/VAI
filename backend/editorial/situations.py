"""One editorial situation, however many episodes it was reported as (V2-P11).

`backend/gaming/episodes.py` reads a run of the *same* named event type as one
episode, and deliberately refuses to merge different types: it measured 255
named events across three real recordings and found that the gap distribution
for same-type pairs is indistinguishable from different-type pairs, so time
alone cannot tell "this fight is still going" from "something else happened
nearby". Type identity is the signal. Different types stay separate and are
related by a :class:`Link`.

That decision is correct and this module does not touch it. What it adds is the
layer above:

    combat → low_health → healing → combat → victory

is five episodes and four links to the correlator, and one *situation* to an
editor: an attack that went wrong, a recovery, and a win. The episodes stay
whole inside it, because which was which is the part worth keeping.

A situation is built from links, never from a time window. Two episodes belong
to the same situation when the correlator already said they are related --
which is a claim someone measured -- rather than when they happen to be near
each other, which is a claim nobody checked. That is the whole difference
between this and the merge the episode reader refused to do.

The arc -- what caused it, how it developed, where it turned, how it ended --
is read from V2-P2's phases and the semantic lanes, not invented from event
names. A situation whose arc cannot be read says so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger

logger = get_logger("editorial.situations", LogChannel.PIPELINE)

#: The parts of an arc a situation can be read as having. Not every situation
#: has all of them, and one that has only a payoff is a highlight rather than a
#: story -- which is a true and useful thing to know about it.
PARTS: Final[tuple[str, ...]] = (
    "cause",
    "development",
    "escalation",
    "payoff",
    "reaction",
    "outcome",
)

#: How much of the session's own range the intensity must climb across a
#: situation for it to be called escalating. Percentile-normalised lanes, so
#: this is a share of what this session actually did.
ESCALATION: Final[float] = 0.15


@dataclass(frozen=True, slots=True)
class Situation:
    """A run of related episodes, read as the one thing they were."""

    id: str
    media_id: str
    start_seconds: float
    end_seconds: float
    #: The episodes this was reported as, in time order. Kept whole: a
    #: situation is a reading of them, never a replacement.
    episodes: tuple[Any, ...] = ()
    #: ``part -> the episode that plays it``, for the parts that could be read.
    arc: dict[str, Any] = field(default_factory=dict)
    #: Why this was read as one situation, in a sentence.
    because: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)

    @property
    def parts(self) -> int:
        return len(self.episodes)

    @property
    def is_compound(self) -> bool:
        """Whether more than one episode makes this up.

        A single-episode situation is not a failure of the reading -- most
        situations are one thing happening -- but the compound ones are where
        an editor has a decision to make.
        """
        return self.parts > 1

    @property
    def types(self) -> tuple[str, ...]:
        found: list[str] = []
        for episode in self.episodes:
            name = str(getattr(getattr(episode, "event_type", None), "value", ""))
            if name and name not in found:
                found.append(name)
        return tuple(found)

    @property
    def has_payoff(self) -> bool:
        return "payoff" in self.arc

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": round(self.start_seconds, 2),
            "seconds": round(self.duration, 2),
            "episodes": self.parts,
            "types": list(self.types),
            "arc": sorted(self.arc),
            "because": self.because,
        }


def read(reading: Any, *, media_id: str, reader: Any = None) -> tuple[Situation, ...]:
    """Group one recording's episodes into the situations they amount to.

    Args:
        reading: the episode reader's output -- episodes and the links it
            already found between differently typed ones.
        media_id: which recording. Two recordings of one session both have a
            second 40, and a situation attributed to the wrong one describes
            footage it never saw.
        reader: the session's semantic lanes, for reading the arc. Without one
            the situations are still grouped; they simply carry no arc, which
            is said rather than guessed.
    """
    episodes = tuple(getattr(reading, "episodes", ()) or ())
    if not episodes:
        return ()

    groups = _grouped(episodes, tuple(getattr(reading, "links", ()) or ()))
    situations: list[Situation] = []
    for index, group in enumerate(groups):
        ordered = tuple(sorted(group, key=lambda e: float(e.start_seconds)))
        start = float(ordered[0].start_seconds)
        end = max(float(episode.end_seconds) for episode in ordered)
        situations.append(
            Situation(
                id=f"sit-{media_id[-8:]}-{index:03d}",
                media_id=media_id,
                start_seconds=start,
                end_seconds=end,
                episodes=ordered,
                arc=_arc(ordered, reader),
                because=_because(ordered),
            )
        )
    logger.info(
        "The session's episodes were read as situations",
        extra={
            "media_id": media_id,
            "episodes": len(episodes),
            "situations": len(situations),
            "compound": sum(1 for s in situations if s.is_compound),
        },
    )
    return tuple(situations)


def _grouped(episodes: Sequence[Any], links: Sequence[Any]) -> list[list[Any]]:
    """Episodes joined into groups by the links the correlator already found.

    A union-find over links rather than a sweep over time. Grouping by
    proximity is exactly the merge the episode reader measured and refused;
    this only joins what something already said was related.
    """
    parent = {id(episode): id(episode) for episode in episodes}
    by_key = {id(episode): episode for episode in episodes}

    def find(key: int) -> int:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for link in links:
        earlier, later = getattr(link, "earlier", None), getattr(link, "later", None)
        if earlier is None or later is None:
            continue
        a, b = id(earlier), id(later)
        if a not in parent or b not in parent:
            continue
        parent[find(a)] = find(b)

    grouped: dict[int, list[Any]] = {}
    for key, episode in by_key.items():
        grouped.setdefault(find(key), []).append(episode)
    return sorted(grouped.values(), key=lambda group: min(e.start_seconds for e in group))


def _arc(episodes: Sequence[Any], reader: Any) -> dict[str, Any]:
    """Which episode plays which part, for the parts that can be read.

    Measured from the lanes rather than inferred from event names: a `victory`
    is not automatically a payoff, and a `low_health` is not automatically a
    cause. What makes an episode the payoff is that the tension it carried let
    go afterwards.
    """
    if reader is None or not episodes:
        return {}

    arc: dict[str, Any] = {"cause": episodes[0]}
    if len(episodes) > 2:
        arc["development"] = episodes[1]

    peaks = [(_intensity(reader, e), e) for e in episodes]
    strongest = max(peaks, key=lambda pair: pair[0])[1]

    first = _intensity(reader, episodes[0])
    last = _intensity(reader, episodes[-1])
    if _intensity(reader, strongest) - first >= ESCALATION:
        arc["escalation"] = strongest

    if _releases(reader, strongest):
        arc["payoff"] = strongest
    if episodes[-1] is not strongest:
        arc["outcome"] = episodes[-1]
    if _speech_after(reader, episodes[-1]):
        arc["reaction"] = episodes[-1]
    if last < first - ESCALATION:
        arc.setdefault("outcome", episodes[-1])
    return arc


def _intensity(reader: Any, episode: Any) -> float:
    import statistics

    try:
        window = list(
            reader.window("intensity", float(episode.start_seconds), float(episode.end_seconds))
        )
    except Exception:
        return 0.0
    return float(statistics.median(window)) if window else 0.0


def _releases(reader: Any, episode: Any) -> bool:
    """Whether the tension this episode carried let go after it."""
    import statistics

    try:
        during = list(
            reader.window("tension", float(episode.start_seconds), float(episode.end_seconds))
        )
        after = list(
            reader.window(
                "tension", float(episode.end_seconds), float(episode.end_seconds) + 8.0
            )
        )
    except Exception:
        return False
    if not during or not after:
        return False
    return statistics.median(during) > statistics.median(after)


def _speech_after(reader: Any, episode: Any) -> bool:
    import statistics

    try:
        after = list(
            reader.window(
                "speech", float(episode.end_seconds), float(episode.end_seconds) + 8.0
            )
        )
    except Exception:
        return False
    return bool(after) and statistics.median(after) >= 0.5


def _because(episodes: Sequence[Any]) -> str:
    """One sentence saying why these are one situation."""
    if len(episodes) == 1:
        return "one episode, standing alone"
    names = " → ".join(
        str(getattr(getattr(episode, "event_type", None), "value", "?"))
        for episode in episodes
    )
    return f"{len(episodes)} related episodes: {names}"


def situation_of(situations: Sequence[Situation], moment: Any) -> Situation | None:
    """The situation a moment falls inside, if any.

    By overlap rather than by containment: a moment's span is chosen for a clip
    and a situation's for a story, and neither was built to sit inside the
    other.
    """
    start = float(getattr(moment, "start_seconds", 0.0))
    end = float(getattr(moment, "end_seconds", start))
    media_id = str(getattr(moment, "media_id", ""))
    for situation in situations:
        if situation.media_id != media_id:
            continue
        if start < situation.end_seconds and end > situation.start_seconds:
            return situation
    return None


__all__ = ["ESCALATION", "PARTS", "Situation", "read", "situation_of"]
