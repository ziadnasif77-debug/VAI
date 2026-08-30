"""The session's lanes, resampled onto the finished video.

Every reader so far speaks *source* time, which is right for everything that
decides what to cut. The stages after the cut -- the mix, and the critic that
will watch the render -- work in programme time, and until now they simply had
no way to ask what the session was doing at 2:41 of the finished video.

This is that translation and nothing else: each programme bin takes the value
of the source bin the edit put there. Footage the edit dropped is not
averaged in, because it is not in the video; a bin no clip covers reads zero,
because nothing is there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from backend.semantic.reader import LANES, LEVELS, Level, SemanticReader, ShapeSegment


class ProgrammeReader:
    """A :class:`SemanticReader` over the finished timeline."""

    def __init__(
        self,
        *,
        hz: int,
        duration_seconds: float,
        lanes: dict[str, list[float]],
        levels: dict[str, float],
        min_segment_seconds: float,
        source_range: tuple[float, float] | None = None,
    ) -> None:
        self.media_id = "programme"
        self.hz = hz
        self.duration_s = duration_seconds
        self.lanes = lanes
        self._levels = levels
        self._min_segment_s = min_segment_seconds
        self._range: tuple[float, float] | None = source_range

    @classmethod
    def build(
        cls,
        clips: Sequence[Any],
        readers: dict[str, SemanticReader],
        *,
        duration_seconds: float,
        config: Any,
    ) -> ProgrammeReader | None:
        """Resample ``readers`` onto the programme the ``clips`` describe."""
        if not clips or not readers or duration_seconds <= 0:
            return None
        semantic = config.editorial.semantic
        hz = int(semantic.hz)
        n = max(1, round(duration_seconds * hz))
        lanes = {name: [0.0] * n for name in LANES}
        step = 1.0 / hz
        for clip in clips:
            reader = readers.get(clip.media_id)
            if reader is None:
                continue
            offset = clip.source_in - clip.timeline_start
            first = max(0, int(clip.timeline_start * hz))
            last = min(n, int(clip.timeline_end * hz) + 1)
            for index in range(first, last):
                source_at = index * step + offset
                for name in LANES:
                    try:
                        lanes[name][index] = reader.value_at(name, source_at)
                    except KeyError:
                        continue
        return cls(
            hz=hz,
            duration_seconds=duration_seconds,
            lanes=lanes,
            levels={
                level: float(getattr(semantic.levels, level)) for level in LEVELS[:-1]
            },
            min_segment_seconds=float(semantic.min_segment_seconds),
            source_range=_session_range(readers),
        )

    # -- the reader contract ---------------------------------------------

    def _index(self, seconds: float) -> int:
        return max(0, min(len(self.lanes["intensity"]) - 1, int(seconds * self.hz)))

    def lane(self, name: str) -> list[float]:
        try:
            return self.lanes[name]
        except KeyError:
            raise KeyError(f"no lane {name!r} on the programme") from None

    def window(self, name: str, start: float, end: float) -> list[float]:
        values = self.lane(name)
        a, b = self._index(start), self._index(end)
        return values[a : b + 1] if b > a else [values[a]]

    def value_at(self, name: str, seconds: float) -> float:
        return self.lane(name)[self._index(seconds)]

    def intensity_between(self, start: float, end: float) -> float:
        window = self.window("intensity", start, end)
        return 0.6 * (sum(window) / len(window)) + 0.4 * max(window)

    def level_for(self, start: float, end: float) -> Level:
        value = self.intensity_between(start, end)
        low, high = self._robust_range
        if high - low >= 0.15:
            value = (value - low) / (high - low)
        for level in LEVELS[:-1]:
            if value <= self._levels[level]:
                return level
        return "climax"

    @property
    def _robust_range(self) -> tuple[float, float]:
        """The scale a level is graded against.

        The SESSION's range, not the programme's. Selection has already
        removed the valleys, so a finished edit is uniformly interesting and
        grading it against itself says "all normal" -- on the gate session
        that produced exactly one music section over a video containing three
        payoffs. A climax is a climax because of what it was in the recording,
        not because of what else survived the cut.
        """
        if self._range is None:
            ordered = sorted(self.lanes["intensity"])
            n = len(ordered)
            self._range = (ordered[int(0.05 * (n - 1))], ordered[int(0.95 * (n - 1))])
        return self._range

    def shape(self, *, min_segment: float | None = None) -> list[ShapeSegment]:
        floor = self._min_segment_s if min_segment is None else min_segment
        lane = self.lanes["intensity"]
        if not lane:
            return []
        step = 1.0 / self.hz
        segments: list[ShapeSegment] = []
        current = self.level_for(0.0, step)
        start = 0.0
        for index in range(1, len(lane)):
            at = index * step
            level = self.level_for(at, at + step)
            if level != current:
                segments.append(ShapeSegment(start, at, current))
                current, start = level, at
        segments.append(ShapeSegment(start, self.duration_s, current))

        merged: list[ShapeSegment] = []
        for segment in segments:
            if merged and segment.seconds < floor:
                previous = merged[-1]
                merged[-1] = ShapeSegment(
                    previous.start_seconds, segment.end_seconds, previous.level
                )
                continue
            merged.append(segment)
        joined: list[ShapeSegment] = []
        for segment in merged:
            if joined and joined[-1].level == segment.level:
                joined[-1] = ShapeSegment(
                    joined[-1].start_seconds, segment.end_seconds, segment.level
                )
                continue
            joined.append(segment)
        return joined

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "start_seconds": round(segment.start_seconds, 1),
                "end_seconds": round(segment.end_seconds, 1),
                "level": segment.level,
            }
            for segment in self.shape()
        ]


def _session_range(readers: dict[str, Any]) -> tuple[float, float] | None:
    """The widest intensity range among the recordings behind this edit."""
    ranges = [
        getattr(reader, "_robust_range", None)
        for reader in readers.values()
    ]
    real = [item for item in ranges if item]
    if not real:
        return None
    return (min(low for low, _ in real), max(high for _, high in real))


__all__ = ["ProgrammeReader"]
