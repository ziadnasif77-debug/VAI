"""The Semantic Timeline — meaning per half-second, from evidence that exists.

V2's foundation (design: "المخرج داخل الزمن", P1-A). Nothing new is detected:
the lanes are a fusion of what the pipeline already stored -- frame motion,
audio energy, game events weighted by importance, scene-change impulses,
speech words, and the distilled screen states. Every lane is
percentile-normalised WITHIN the session before blending, the lesson §23
paid for once: a quiet game still has a shape, and its peaks are peaks of
its own session.

Cached beside the analysis artefacts by an input signature (the
source-dead pattern): a re-run reads JSON, not tables.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger

logger = get_logger("semantic.timeline", LogChannel.PIPELINE)

LEVELS: Final[tuple[str, ...]] = ("calm", "normal", "tension", "high", "climax")


@dataclass(frozen=True, slots=True)
class ShapeSegment:
    """One stretch of the session at one dramatic level."""

    start_seconds: float
    end_seconds: float
    level: str

    @property
    def seconds(self) -> float:
        return self.end_seconds - self.start_seconds


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

    def level_for(self, start: float, end: float) -> str:
        return self._classify(self.intensity_between(start, end))

    def _classify(self, value: float) -> str:
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

    def shape(self) -> list[ShapeSegment]:
        """The session's natural form: level runs, short runs merged (§80)."""
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
            if merged and segment.seconds < self._min_segment_s:
                previous = merged[-1]
                merged[-1] = ShapeSegment(
                    previous.start_seconds, segment.end_seconds, previous.level
                )
                continue
            merged.append(segment)
        # A leading sliver merges forward instead.
        if len(merged) >= 2 and merged[0].seconds < self._min_segment_s:
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
) -> SemanticTimeline:
    """Fuse the stored evidence into lanes.

    Args carry plain tuples so tests build worlds without a database:
        frames:       (timestamp, motion_score)
        audio_events: (start, end, rms_db, event_type)
        game_events:  (start, end, importance*confidence, event_type)
        scenes:       (boundary_seconds, change_score)
        words:        (start, end)
        dead_spans:   (start, end) -- corroborated non-gameplay
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
            decay = 1.0 - (index * step - boundary) / 1.5
            scene_lane[index] = max(scene_lane[index], rank * max(0.0, decay))

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
        },
        _levels={level: float(getattr(semantic.levels, level)) for level in LEVELS[:-1]},
        _min_segment_s=float(semantic.min_segment_seconds),
    )
    return timeline


# -- database + cache ----------------------------------------------------


def load_timeline(
    database: Any,
    media_id: str,
    *,
    duration_seconds: float,
    config: Any,
    cache_dir: Path,
) -> SemanticTimeline:
    """Build from the stored analysis, through a signature-keyed cache."""
    rows = {
        "frames": database.fetch_all(
            "SELECT timestamp, motion_score FROM frames WHERE media_id = ? "
            "ORDER BY timestamp",
            (media_id,),
        ),
        "audio": database.fetch_all(
            "SELECT start_seconds, end_seconds, rms_db, event_type FROM audio_events "
            "WHERE media_id = ? ORDER BY start_seconds",
            (media_id,),
        ),
        "events": database.fetch_all(
            "SELECT start_seconds, end_seconds, importance, confidence, event_type "
            "FROM game_events WHERE media_id = ? ORDER BY start_seconds",
            (media_id,),
        ),
        "scenes": database.fetch_all(
            "SELECT start_seconds, change_score FROM scenes WHERE media_id = ? "
            "ORDER BY start_seconds",
            (media_id,),
        ),
        "words": database.fetch_all(
            "SELECT start_seconds, end_seconds FROM transcript_segments "
            "WHERE media_id = ? ORDER BY start_seconds",
            (media_id,),
        ),
    }
    signature = hashlib.sha256(
        json.dumps(
            {name: len(items) for name, items in rows.items()}
            | {"duration": round(duration_seconds, 2), "hz": int(config.editorial.semantic.hz)}
        ).encode()
    ).hexdigest()[:16]

    cache = cache_dir / f"{media_id}.json"
    if cache.is_file():
        try:
            stored = json.loads(cache.read_text(encoding="utf-8"))
            if stored.get("signature") == signature:
                semantic = config.editorial.semantic
                return SemanticTimeline(
                    media_id=media_id,
                    duration_s=stored["duration_s"],
                    hz=stored["hz"],
                    lanes=stored["lanes"],
                    _levels={
                        level: float(getattr(semantic.levels, level))
                        for level in LEVELS[:-1]
                    },
                    _min_segment_s=float(semantic.min_segment_seconds),
                )
        except (OSError, ValueError, KeyError):
            pass

    from backend.analysis import frame_state
    from backend.database.repositories.vision import VisionRepository

    spans = frame_state.spans(
        VisionRepository(database).list_for_media(media_id),
        duration_seconds=duration_seconds,
    )
    dead = [
        (span.start_seconds, span.end_seconds)
        for span in spans
        if not span.state.is_gameplay and span.observations >= 2
    ]

    timeline = build_timeline(
        media_id=media_id,
        duration_seconds=duration_seconds,
        frames=[(float(r["timestamp"]), float(r["motion_score"] or 0.0)) for r in rows["frames"]],
        audio_events=[
            (float(r["start_seconds"]), float(r["end_seconds"]),
             float(r["rms_db"] or -60.0), str(r["event_type"]))
            for r in rows["audio"]
        ],
        game_events=[
            (float(r["start_seconds"]), float(r["end_seconds"]),
             float(r["importance"] or 0.5) * float(r["confidence"] or 0.5),
             str(r["event_type"]))
            for r in rows["events"]
        ],
        scenes=[
            (float(r["start_seconds"]), float(r["change_score"] or 0.0))
            for r in rows["scenes"]
        ],
        words=[(float(r["start_seconds"]), float(r["end_seconds"])) for r in rows["words"]],
        dead_spans=dead,
        config=config,
    )

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "signature": signature,
            "duration_s": timeline.duration_s,
            "hz": timeline.hz,
            "lanes": {name: [round(v, 4) for v in lane] for name, lane in timeline.lanes.items()},
        }
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(cache)
    except OSError:
        logger.exception("Semantic timeline cache write failed; rebuilt next time")
    return timeline


__all__ = ["LEVELS", "SemanticTimeline", "ShapeSegment", "build_timeline", "load_timeline"]
