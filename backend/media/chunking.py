"""Chunked processing of long sources (SPEC section 7).

§7 is a hard requirement: 30 minutes, 1 h, 2 h, 4 h, 6 h, 8 h, **without
loading the video into RAM**. Nothing in the pipeline may hold a whole
recording; every stage walks it in bounded pieces instead.

The plan produced here is the shared definition of those pieces, so the audio
analyser, the frame sampler, the scene detector and the vision cascade all cut
the timeline in exactly the same places and their results line up.

Two spans per chunk, and the distinction is the point:

* **core** ``[core_start, core_end)`` -- the region this chunk is responsible
  for. Cores tile the source exactly once, so every instant has exactly one
  owner and an event found twice is trivially deduplicated: it belongs to the
  chunk whose core contains its start.
* **window** ``[start, end)`` -- the region this chunk reads, being its core
  plus overlap on each side. A detector needs context on both sides of a
  boundary; an explosion two seconds before the cut is only recognisable with
  the seconds before it.

This module is pure arithmetic: no I/O, no FFmpeg, no configuration lookups.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from backend.core.errors import ErrorCode, ValidationError

#: A trailing remainder shorter than this fraction of a chunk is merged into
#: the previous chunk rather than becoming a chunk of its own. Ten minutes of
#: gameplay plus four seconds should be one unit of work, not two.
MERGE_REMAINDER_FRACTION: Final[float] = 0.25


@dataclass(frozen=True, slots=True)
class Chunk:
    """One unit of work over a source recording."""

    index: int
    #: Region this chunk owns. Cores tile the source with no gap and no overlap.
    core_start: float
    core_end: float
    #: Region this chunk reads: the core widened by the configured overlap and
    #: clamped to the source. Results found here but outside the core are
    #: context, not findings -- the neighbouring chunk owns them.
    start: float
    end: float
    #: Set by :func:`plan_chunks`. The last chunk owns its closing instant, so
    #: the final frame of a recording is not orphaned by half-open arithmetic.
    is_final: bool = False

    @property
    def duration(self) -> float:
        """Length of the read window, in seconds."""
        return self.end - self.start

    @property
    def core_duration(self) -> float:
        """Length of the owned region, in seconds."""
        return self.core_end - self.core_start

    @property
    def lead_in(self) -> float:
        """Context read before the owned region."""
        return self.core_start - self.start

    @property
    def lead_out(self) -> float:
        """Context read after the owned region."""
        return self.end - self.core_end

    def owns(self, timestamp: float) -> bool:
        """Whether this chunk is responsible for ``timestamp``.

        Half-open on the right so a boundary instant has exactly one owner.
        The final chunk includes its end, or the last frame would belong to
        nobody.
        """
        if timestamp < self.core_start:
            return False
        return timestamp < self.core_end or (self.is_final and timestamp <= self.core_end)

    def contains(self, timestamp: float) -> bool:
        """Whether ``timestamp`` falls anywhere in the read window."""
        return self.start <= timestamp <= self.end

    def clamp(self, timestamp: float) -> float:
        """Clamp ``timestamp`` into the read window."""
        return min(max(timestamp, self.start), self.end)


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    """Every chunk of one source, plus the settings that produced them."""

    duration_seconds: float
    chunk_seconds: float
    overlap_seconds: float
    event_overlap_seconds: float
    chunks: tuple[Chunk, ...]

    def __iter__(self) -> Iterator[Chunk]:
        return iter(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    def __getitem__(self, index: int) -> Chunk:
        return self.chunks[index]

    def owner_of(self, timestamp: float) -> Chunk | None:
        """Return the chunk responsible for ``timestamp``, if any."""
        return next((chunk for chunk in self.chunks if chunk.owns(timestamp)), None)

    def near_boundary(self, timestamp: float) -> bool:
        """Whether ``timestamp`` sits close enough to a core boundary to be at
        risk of double detection.

        Callers merging results from neighbouring chunks only need to compare
        candidates for which this is true -- everywhere else, a single chunk saw
        the evidence and there is nothing to reconcile (§7).
        """
        if self.event_overlap_seconds <= 0:
            return False
        for chunk in self.chunks[:-1]:
            if abs(timestamp - chunk.core_end) <= self.event_overlap_seconds:
                return True
        return False

    def covers(self) -> bool:
        """Whether the cores tile ``[0, duration]`` exactly. Invariant, asserted
        by the tests: a gap means a stretch of gameplay nothing analysed."""
        if not self.chunks:
            return False
        if self.chunks[0].core_start != 0.0:
            return False
        if abs(self.chunks[-1].core_end - self.duration_seconds) > 1e-6:
            return False
        return all(
            abs(nxt.core_start - cur.core_end) <= 1e-6
            for cur, nxt in zip(self.chunks, self.chunks[1:], strict=False)
        )


def plan_chunks(
    duration_seconds: float,
    *,
    chunk_seconds: float,
    overlap_seconds: float = 0.0,
    event_overlap_seconds: float = 0.0,
) -> ChunkPlan:
    """Divide a source of ``duration_seconds`` into chunks.

    Args:
        duration_seconds: probed source duration. Must be positive -- chunking
            an unknown length is what produces either one infinite chunk or
            none at all.
        chunk_seconds: size of each owned region (``analysis.chunk_seconds``).
        overlap_seconds: context read either side of the owned region
            (``analysis.chunk_overlap_seconds``).
        event_overlap_seconds: how close to a boundary a detection has to be
            before neighbouring chunks might both report it
            (``analysis.event_overlap_seconds``).

    Raises:
        ValidationError: on a non-positive duration or chunk size, or when the
            overlap is not smaller than the chunk (which would make chunks
            read more context than content).
    """
    if duration_seconds <= 0:
        raise ValidationError(
            "Cannot chunk a source with unknown or zero duration.",
            code=ErrorCode.BUSINESS_VALIDATION_FAILED,
            details={"duration_seconds": duration_seconds},
            recoverable=False,
        )
    if chunk_seconds <= 0:
        raise ValidationError(
            "chunk_seconds must be positive.",
            code=ErrorCode.BUSINESS_VALIDATION_FAILED,
            details={"chunk_seconds": chunk_seconds},
            recoverable=False,
        )
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise ValidationError(
            "chunk_overlap_seconds must be at least zero and smaller than chunk_seconds.",
            code=ErrorCode.BUSINESS_VALIDATION_FAILED,
            details={"chunk_seconds": chunk_seconds, "overlap_seconds": overlap_seconds},
            recoverable=False,
        )

    boundaries = _core_boundaries(duration_seconds, chunk_seconds)
    chunks = tuple(
        Chunk(
            index=index,
            core_start=core_start,
            core_end=core_end,
            start=max(core_start - overlap_seconds, 0.0),
            end=min(core_end + overlap_seconds, duration_seconds),
            is_final=index == len(boundaries) - 1,
        )
        for index, (core_start, core_end) in enumerate(boundaries)
    )
    return ChunkPlan(
        duration_seconds=duration_seconds,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
        event_overlap_seconds=event_overlap_seconds,
        chunks=chunks,
    )


def _core_boundaries(duration: float, chunk_seconds: float) -> list[tuple[float, float]]:
    """Tile ``[0, duration]`` with regions of ``chunk_seconds``.

    A short trailing remainder is absorbed by the previous region: a 4-second
    final chunk costs a process start, a seek and a cache entry to analyse what
    its neighbour already had in context.
    """
    count = max(math.ceil(duration / chunk_seconds), 1)
    boundaries = [
        (index * chunk_seconds, min((index + 1) * chunk_seconds, duration))
        for index in range(count)
    ]
    if len(boundaries) > 1:
        last_start, last_end = boundaries[-1]
        if (last_end - last_start) < chunk_seconds * MERGE_REMAINDER_FRACTION:
            previous_start, _ = boundaries[-2]
            boundaries[-2:] = [(previous_start, last_end)]
    return boundaries


def total_work_seconds(chunks: Sequence[Chunk]) -> float:
    """Total seconds of media the plan will read, overlap included.

    Progress across a chunked stage is reported against this, not against the
    source duration: with overlap the two differ, and a bar that finishes at
    103 % is a bar nobody trusts.
    """
    return sum(chunk.duration for chunk in chunks)


__all__ = [
    "MERGE_REMAINDER_FRACTION",
    "Chunk",
    "ChunkPlan",
    "plan_chunks",
    "total_work_seconds",
]
