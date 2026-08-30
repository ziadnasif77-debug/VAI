"""The Semantic Timeline — meaning per half-second, from evidence that exists.

V2's foundation (design: "المخرج داخل الزمن", P1-A). Nothing new is detected:
the lanes are a fusion of what the pipeline already stored -- frame motion,
audio energy, game events weighted by importance, scene-change impulses,
speech words, and the distilled screen states. Every lane is
percentile-normalised WITHIN the session before blending, the lesson §23
paid for once: a quiet game still has a shape, and its peaks are peaks of
its own session.

Built once per recording by the SEMANTIC stage and stored in
``semantic_timelines``, keyed by a digest of the values the lanes were built
from -- not of their row counts, which was the first version and meant
re-scoring an event without changing how many there were returned a stale
timeline in silence.

Consumers read through :class:`backend.semantic.reader.SemanticReader`; only
this module and the store know the concrete class.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.semantic.reader import LANES, LEVELS, Level, ShapeSegment

logger = get_logger("semantic.timeline", LogChannel.PIPELINE)

#: Bumped when the lanes change shape or meaning, so a stored timeline built
#: by an older build is rebuilt rather than trusted.
BUILDER_VERSION: Final[str] = "2"

#: How far back a stretch is compared with, for the novelty lane.
NOVELTY_MEMORY_SECONDS: Final[float] = 300.0
#: How much footage counts as "here" when measuring novelty.
NOVELTY_WINDOW_SECONDS: Final[float] = 15.0


@dataclass
class SemanticTimeline:
    """Per-half-second lanes over one recording."""

    media_id: str
    duration_s: float
    hz: int
    lanes: dict[str, list[float]] = field(default_factory=dict)
    _levels: dict[str, float] = field(default_factory=dict)
    _min_segment_s: float = 4.0
    _range_cache: tuple[float, float] | None = None

    # -- reading ---------------------------------------------------------

    def lane(self, name: str) -> list[float]:
        """The whole lane. An unknown name is a programming error, not a
        missing measurement, so it raises rather than returning zeros."""
        try:
            return self.lanes[name]
        except KeyError:
            raise KeyError(
                f"no lane {name!r}; this timeline carries {sorted(self.lanes)}"
            ) from None

    def window(self, name: str, start: float, end: float) -> list[float]:
        """The bins of ``name`` covering ``[start, end]``, never empty."""
        values = self.lane(name)
        a, b = self._index(start), self._index(end)
        return values[a : b + 1] if b > a else [values[a]]

    def value_at(self, name: str, seconds: float) -> float:
        return self.lane(name)[self._index(seconds)]

    def _index(self, t: float) -> int:
        return max(0, min(len(self.lanes["intensity"]) - 1, int(t * self.hz)))

    def intensity_between(self, start: float, end: float) -> float:
        lane = self.lanes["intensity"]
        a, b = self._index(start), self._index(end)
        if b <= a:
            return lane[a]
        window = lane[a : b + 1]
        # Mean carries the stretch; the peak keeps a spike from washing out.
        return 0.6 * (sum(window) / len(window)) + 0.4 * max(window)

    def level_for(self, start: float, end: float) -> Level:
        return self._classify(self.intensity_between(start, end))

    def _classify(self, value: float) -> Level:
        lo, hi = self._robust_range
        if hi - lo >= 0.15:
            # Rescale into THIS session's dynamic range before grading.
            # Absolute thresholds on blended values meant no real session
            # ever reached climax (the lanes rarely peak in unison); pure
            # rank-quantiles crowned every molehill on a bimodal session.
            # Value-within-range keeps both honest: a small bump near the
            # session floor stays calm, the session top always grades hot.
            value = (value - lo) / (hi - lo)
        for level in LEVELS[:-1]:
            if value <= self._levels[level]:
                return level
        return "climax"

    @property
    def _robust_range(self) -> tuple[float, float]:
        """The session's p05..p95 intensity span; below 0.15 of spread the
        lane is flat and grading falls back to absolute thresholds."""
        if self._range_cache is None:
            ordered = sorted(self.lanes["intensity"])
            n = len(ordered)
            self._range_cache = (
                ordered[int(0.05 * (n - 1))],
                ordered[int(0.95 * (n - 1))],
            )
        return self._range_cache

    def shape(self, *, min_segment: float | None = None) -> list[ShapeSegment]:
        """The session's natural form: level runs, short runs merged (§80).

        ``min_segment`` overrides how short a run may be before it merges into
        its neighbour. The configured value is the *narrative* shape -- what a
        person would call a section. Pacing asks for a finer one: a two-second
        burst is not a section, but it is absolutely a turn worth cutting on.
        """
        floor = self._min_segment_s if min_segment is None else min_segment
        lane = self.lanes["intensity"]
        if not lane:
            return []
        step = 1.0 / self.hz
        segments: list[ShapeSegment] = []
        current = self._classify(lane[0])
        start = 0.0
        for index in range(1, len(lane)):
            level = self._classify(lane[index])
            if level != current:
                segments.append(ShapeSegment(start, index * step, current))
                current, start = level, index * step
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
        # A leading sliver merges forward instead.
        if len(merged) >= 2 and merged[0].seconds < floor:
            merged[1] = ShapeSegment(
                merged[0].start_seconds, merged[1].end_seconds, merged[1].level
            )
            merged = merged[1:]
        # Sliver absorption can leave same-level neighbours; one run each.
        coalesced: list[ShapeSegment] = []
        for segment in merged:
            if coalesced and coalesced[-1].level == segment.level:
                coalesced[-1] = ShapeSegment(
                    coalesced[-1].start_seconds, segment.end_seconds, segment.level
                )
                continue
            coalesced.append(segment)
        return coalesced

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "start_seconds": round(segment.start_seconds, 1),
                "end_seconds": round(segment.end_seconds, 1),
                "level": segment.level,
            }
            for segment in self.shape()
        ]


# -- construction --------------------------------------------------------


def _percentile_ranks(values: list[float]) -> list[float]:
    """Each value's rank within the list, 0..1. Flat input ranks 0.5."""
    if not values:
        return []
    ordered = sorted(values)
    if ordered[0] == ordered[-1]:
        return [0.5] * len(values)
    n = len(ordered)
    return [bisect_right(ordered, value) / n for value in values]


def build_timeline(
    *,
    media_id: str,
    duration_seconds: float,
    frames: list[tuple[float, float]],
    audio_events: list[tuple[float, float, float, str]],
    game_events: list[tuple[float, float, float, str]],
    scenes: list[tuple[float, float]],
    words: list[tuple[float, float]],
    dead_spans: list[tuple[float, float]],
    config: Any,
    labels: list[tuple[float, tuple[str, ...]]] | None = None,
) -> SemanticTimeline:
    """Fuse the stored evidence into lanes.

    Args carry plain tuples so tests build worlds without a database:
        frames:       (timestamp, motion_score)
        audio_events: (start, end, rms_db, event_type)
        game_events:  (start, end, importance*confidence, event_type)
        scenes:       (boundary_seconds, change_score)
        words:        (start, end)
        dead_spans:   (start, end) -- corroborated non-gameplay
        labels:       (timestamp, vision labels) -- for the novelty lane
    """
    semantic = config.editorial.semantic
    hz = int(semantic.hz)
    n = max(1, round(duration_seconds * hz))
    step = 1.0 / hz

    # motion: sparse samples -> linear interpolation -> session percentile
    motion = [0.0] * n
    if frames:
        frames = sorted(frames)
        ranks = _percentile_ranks([score for _, score in frames])
        times = [t for t, _ in frames]
        for index in range(n):
            t = index * step
            j = bisect_right(times, t)
            if j <= 0:
                motion[index] = ranks[0]
            elif j >= len(frames):
                motion[index] = ranks[-1]
            else:
                t0, t1 = times[j - 1], times[j]
                k = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
                motion[index] = ranks[j - 1] * (1 - k) + ranks[j] * k

    # audio: paint event intervals with the session-percentile of their RMS
    audio = [0.0] * n
    loud = [rms for _, _, rms, kind in audio_events if kind != "silence"]
    loud_ranks = _percentile_ranks(loud)
    li = 0
    for start, end, _rms, kind in audio_events:
        if kind == "silence":
            continue
        rank = loud_ranks[li]
        li += 1
        for index in range(max(0, int(start * hz)), min(n, int(end * hz) + 1)):
            audio[index] = max(audio[index], rank)

    # events: importance bumps with padded decay
    events_lane = [0.0] * n
    pad = float(semantic.event_pad_seconds)
    for start, end, weight, _kind in game_events:
        lo = max(0, int((start - pad) * hz))
        hi = min(n, int((end + pad) * hz) + 1)
        for index in range(lo, hi):
            t = index * step
            if t < start:
                k = 1.0 - (start - t) / pad if pad > 0 else 0.0
            elif t > end:
                k = 1.0 - (t - end) / pad if pad > 0 else 0.0
            else:
                k = 1.0
            events_lane[index] = max(events_lane[index], max(0.0, k) * min(1.0, weight))

    # scene impulses: change_score decayed over 1.5s after each boundary
    scene_lane = [0.0] * n
    scene_ranks = _percentile_ranks([score for _, score in scenes])
    for (boundary, _score), rank in zip(scenes, scene_ranks, strict=True):
        lo = max(0, int(boundary * hz))
        hi = min(n, int((boundary + 1.5) * hz) + 1)
        for index in range(lo, hi):
            # The bin containing the boundary starts fractionally BEFORE it
            # (int() truncates), and a negative elapsed time made the decay
            # term exceed 1.0 -- an impulse of 1.25 in a lane that promises
            # 0..1. Full strength up to the boundary, decay after it.
            elapsed = max(0.0, index * step - boundary)
            scene_lane[index] = max(scene_lane[index], rank * max(0.0, 1.0 - elapsed / 1.5))

    weights = semantic.weights
    intensity = [
        min(
            1.0,
            weights.motion * motion[i]
            + weights.audio * audio[i]
            + weights.events * events_lane[i]
            + weights.scene * scene_lane[i],
        )
        for i in range(n)
    ]
    # dead screens carry no intensity, whatever the sensors said
    for start, end in dead_spans:
        for index in range(max(0, int(start * hz)), min(n, int(end * hz) + 1)):
            intensity[index] = 0.0

    # tension: EMA over the configured window
    window = max(1, int(semantic.tension_window_seconds * hz))
    alpha = 2.0 / (window + 1)
    tension = []
    running = intensity[0] if intensity else 0.0
    for value in intensity:
        running = alpha * value + (1 - alpha) * running
        tension.append(running)

    speech = [0.0] * n
    for start, end in words:
        for index in range(max(0, int(start * hz)), min(n, int(end * hz) + 1)):
            speech[index] = 1.0

    # dead zones: the same corroborated spans the screen guard cuts by,
    # as a lane rather than a mask, so a consumer can ask "is this gameplay"
    # without knowing how the mask was applied.
    dead_lane = [0.0] * n
    for start, end in dead_spans:
        for index in range(max(0, int(start * hz)), min(n, int(end * hz) + 1)):
            dead_lane[index] = 1.0

    novelty = _novelty_lane(labels or [], n=n, hz=hz)

    timeline = SemanticTimeline(
        media_id=media_id,
        duration_s=duration_seconds,
        hz=hz,
        lanes={
            "intensity": intensity,
            "tension": tension,
            "motion": motion,
            "audio": audio,
            "events": events_lane,
            "speech": speech,
            "scene_changes": scene_lane,
            "novelty": novelty,
            "dead_zones": dead_lane,
        },
        _levels={level: float(getattr(semantic.levels, level)) for level in LEVELS[:-1]},
        _min_segment_s=float(semantic.min_segment_seconds),
    )
    missing = set(LANES) - set(timeline.lanes)
    if missing:
        # A consumer asking for a lane the builder forgot would read zeros and
        # call them a measurement. Better to fail where the mistake is.
        raise KeyError(f"the builder produced no {sorted(missing)} lane(s)")
    return timeline


def _novelty_lane(
    labels: list[tuple[float, tuple[str, ...]]], *, n: int, hz: int
) -> list[float]:
    """How unlike the preceding minutes this stretch looks.

    Measured on what the vision pass named, because that is the only record of
    *what is on screen* rather than how much it moved: the share of the labels
    here that were not among the labels of the last five minutes. A stretch
    that introduces a boss, a new area or a vehicle scores high; the twentieth
    minute of the same forest scores near zero.

    With nothing to compare -- no observations, or none yet behind us -- the
    answer is 0.0 rather than a guess. "Not novel" costs a consumer nothing;
    "novel" invented from silence would promote footage on no evidence.
    """
    lane = [0.0] * n
    if not labels:
        return lane
    ordered = sorted(labels)
    times = [t for t, _ in ordered]
    step = 1.0 / hz
    for index in range(n):
        t = index * step
        here = _labels_between(
            ordered, times, t - NOVELTY_WINDOW_SECONDS, t + NOVELTY_WINDOW_SECONDS
        )
        if not here:
            continue
        before = _labels_between(
            ordered, times, t - NOVELTY_MEMORY_SECONDS, t - NOVELTY_WINDOW_SECONDS
        )
        if not before:
            continue
        lane[index] = len(here - before) / len(here)
    return lane


def _labels_between(
    ordered: list[tuple[float, tuple[str, ...]]],
    times: list[float],
    start: float,
    end: float,
) -> set[str]:
    """Labels in ``[start, end)`` -- half-open, and that matters.

    Closed at both ends, the memory window and the window it is compared with
    share their boundary observation, so the first frame of something new is
    counted as already seen and novelty reads zero exactly where it should
    peak.
    """
    from bisect import bisect_left

    lo = bisect_left(times, start)
    hi = bisect_left(times, end)
    found: set[str] = set()
    for _, names in ordered[lo:hi]:
        found.update(names)
    return found


# -- database + store ----------------------------------------------------


def timeline_signature(
    rows: dict[str, list], *, duration_seconds: float, config: Any
) -> str:
    """A digest of the VALUES the lanes will be built from.

    The first version hashed each table's row *count*, so re-scoring an
    event's importance -- the same events, differently weighted -- returned
    the cached timeline unchanged, and every pacing decision downstream was
    graded from heat that no longer existed. Nothing announced it.

    Identical evidence must give an identical digest; one changed number must
    change it. Everything the builder reads goes in, including the config
    section that shapes the fusion and the builder's own version.
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(
        f"{BUILDER_VERSION}|{duration_seconds:.3f}|{config.editorial.semantic.hz}".encode()
    )
    digest.update(
        json.dumps(
            config.editorial.semantic.model_dump(mode="json"), sort_keys=True
        ).encode()
    )
    for name in sorted(rows):
        digest.update(f"|{name}|".encode())
        for row in rows[name]:
            digest.update(
                "\x1f".join("" if value is None else str(value) for value in row).encode()
            )
            digest.update(b"\x1e")
    return digest.hexdigest()


def _inputs(database: Any, media_id: str, duration_seconds: float) -> dict[str, list]:
    """Every stored row the lanes are built from, as plain tuples.

    Tuples rather than rows so the digest sees exactly what the builder sees:
    a signature over a different shape than the build is a signature over
    nothing.
    """
    from backend.analysis import frame_state
    from backend.database.repositories.vision import VisionRepository

    def fetch(sql: str) -> list[tuple]:
        return [tuple(row) for row in database.fetch_all(sql, (media_id,))]

    observations = VisionRepository(database).list_for_media(media_id)
    spans = frame_state.spans(observations, duration_seconds=duration_seconds)
    return {
        "frames": fetch(
            "SELECT timestamp, motion_score FROM frames WHERE media_id = ? "
            "ORDER BY timestamp"
        ),
        "audio": fetch(
            "SELECT start_seconds, end_seconds, rms_db, event_type FROM audio_events "
            "WHERE media_id = ? ORDER BY start_seconds"
        ),
        "events": fetch(
            "SELECT start_seconds, end_seconds, importance, confidence, event_type "
            "FROM game_events WHERE media_id = ? ORDER BY start_seconds"
        ),
        "scenes": fetch(
            "SELECT start_seconds, change_score FROM scenes WHERE media_id = ? "
            "ORDER BY start_seconds"
        ),
        "words": fetch(
            "SELECT start_seconds, end_seconds FROM transcript_segments "
            "WHERE media_id = ? ORDER BY start_seconds"
        ),
        "labels": [
            (round(item.timestamp, 3), tuple(sorted(item.labels)))
            for item in observations
        ],
        "dead": [
            (round(span.start_seconds, 3), round(span.end_seconds, 3))
            for span in spans
            if not span.state.is_gameplay and span.observations >= 2
        ],
    }


def build_from_inputs(
    rows: dict[str, list], *, media_id: str, duration_seconds: float, config: Any
) -> SemanticTimeline:
    """The builder, fed from stored rows. Pure: same rows, same lanes."""
    return build_timeline(
        media_id=media_id,
        duration_seconds=duration_seconds,
        frames=[(float(t), float(score or 0.0)) for t, score in rows["frames"]],
        audio_events=[
            (float(a), float(b), float(rms if rms is not None else -60.0), str(kind))
            for a, b, rms, kind in rows["audio"]
        ],
        game_events=[
            (
                float(a),
                float(b),
                float(importance if importance is not None else 0.5)
                * float(confidence if confidence is not None else 0.5),
                str(kind),
            )
            for a, b, importance, confidence, kind in rows["events"]
        ],
        scenes=[(float(t), float(score or 0.0)) for t, score in rows["scenes"]],
        words=[(float(a), float(b)) for a, b in rows["words"]],
        dead_spans=[(float(a), float(b)) for a, b in rows["dead"]],
        labels=[(float(t), tuple(names)) for t, names in rows["labels"]],
        config=config,
    )


def load_timeline(
    database: Any,
    media_id: str,
    *,
    duration_seconds: float,
    config: Any,
) -> SemanticTimeline:
    """The session's lanes: stored when they are current, built when not.

    Building is idempotent and deterministic, so a consumer that arrives
    before the SEMANTIC stage has run gets the same answer that stage would
    have stored -- it just pays for it.
    """
    from backend.database.repositories.semantic import SemanticRepository

    semantic = config.editorial.semantic
    rows = _inputs(database, media_id, duration_seconds)
    signature = timeline_signature(rows, duration_seconds=duration_seconds, config=config)

    repository = SemanticRepository(database)
    stored = repository.get(media_id, signature=signature)
    if stored is not None and set(stored["lanes"]) >= set(LANES):
        return SemanticTimeline(
            media_id=media_id,
            duration_s=stored["duration_seconds"],
            hz=stored["hz"],
            lanes=stored["lanes"],
            _levels={level: float(getattr(semantic.levels, level)) for level in LEVELS[:-1]},
            _min_segment_s=float(semantic.min_segment_seconds),
        )

    timeline = build_from_inputs(
        rows, media_id=media_id, duration_seconds=duration_seconds, config=config
    )
    try:
        repository.save(
            media_id,
            signature=signature,
            builder_version=BUILDER_VERSION,
            hz=timeline.hz,
            duration_seconds=timeline.duration_s,
            lanes=timeline.lanes,
        )
    except Exception:
        # A timeline that could not be stored is still a correct timeline;
        # the next consumer rebuilds it rather than failing the stage.
        logger.exception(
            "Semantic timeline could not be stored; it will be rebuilt",
            extra={"media_id": media_id},
        )
    return timeline
