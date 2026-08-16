"""The candidate cascade (SPEC sections 15, 16, 27).

§15 states the constraint and the remedy in one breath: **the vision model must
not process every frame** — instead, scene detection → keyframes → candidate
frames → vision analysis.

The arithmetic behind that sentence is worth writing down, because it is the
whole reason this module exists. A two-hour recording sampled every three
seconds is 2 400 frames. A local 7B vision model spends seconds on each. That
is an afternoon of GPU time to analyse one video, and the user asked for a
video, not an afternoon.

So cheap detectors run first and nominate regions:

    audio spike · scene change · frame difference · speech activity · HUD change
        → candidate regions → keyframes → the model

Every one of those detectors has already run by the time this module is called.
Nominating costs nothing new; it is reading results the pipeline already has.

**The budget is a ceiling, not a target.** ``max_frames_per_source_hour``
bounds model work per hour of source, so analysis time stays predictable no
matter how eventful the recording is. When more regions are nominated than the
budget can cover, the weakest are dropped **and the number dropped is
reported** — a silent truncation would read as "we looked at everything" when
we did not.

**Agreement outranks intensity.** A region nominated by three independent
detectors is a better bet than one very loud bang, which is the same principle
§27 applies to events: several sources agreeing is the strongest evidence
available before a model has looked at anything.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from ai.providers.base import TranscriptSegment
from backend.analysis.audio_events import AudioEvent
from backend.analysis.scenes import SceneResult
from backend.config.schema import AnalysisConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import AudioEventType

logger = get_logger("analysis.candidates", LogChannel.PIPELINE)

#: Trigger names, matching ``analysis.vision.candidate_detectors``. A trigger
#: absent from configuration contributes nothing, so the cascade can be tuned
#: without code changes (§91).
AUDIO_SPIKE: Final[str] = "audio_spike"
SCENE_CHANGE: Final[str] = "scene_change"
FRAME_DIFFERENCE: Final[str] = "frame_difference"
SPEECH_ACTIVITY: Final[str] = "speech_activity"
HUD_CHANGE: Final[str] = "hud_change"

SECONDS_PER_HOUR: Final[float] = 3600.0

#: How much of the priority comes from detector agreement rather than from the
#: strength of any single piece of evidence. Weighted towards agreement for the
#: §27 reason: one loud noise is weaker evidence than three detectors that
#: independently noticed the same instant.
AGREEMENT_WEIGHT: Final[float] = 0.6

#: Size a frame is decoded down to for difference scoring. Large enough to see
#: a scene change or a muzzle flash, small enough that scoring 2 400 frames is
#: seconds rather than minutes.
DIFFERENCE_THUMBNAIL: Final[tuple[int, int]] = (64, 64)


@dataclass(frozen=True, slots=True)
class Trigger:
    """One detector's reason for looking somewhere."""

    source: str
    start_seconds: float
    end_seconds: float
    confidence: float = 1.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0


@dataclass(frozen=True, slots=True)
class CandidateRegion:
    """A span worth a model's attention, and why."""

    start_seconds: float
    end_seconds: float
    sources: frozenset[str]
    priority: float
    triggers: tuple[Trigger, ...] = ()
    #: Instants the model will actually see. Empty when the budget did not
    #: reach this region — it stays in the plan as evidence, unanalysed.
    keyframes: tuple[float, ...] = ()

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0

    @property
    def is_analysed(self) -> bool:
        return bool(self.keyframes)

    @property
    def agreement(self) -> int:
        """How many distinct detectors nominated this region."""
        return len(self.sources)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_seconds": round(self.start_seconds, 3),
            "end_seconds": round(self.end_seconds, 3),
            "sources": sorted(self.sources),
            "priority": round(self.priority, 4),
            "keyframes": [round(value, 3) for value in self.keyframes],
        }


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    """What the vision stage will look at, and what it will not."""

    regions: tuple[CandidateRegion, ...]
    duration_seconds: float
    frame_budget: int
    #: Regions the budget could not reach. Reported, never silent.
    dropped_regions: int = 0

    def __len__(self) -> int:
        return len(self.regions)

    def __iter__(self):
        return iter(self.regions)

    @property
    def analysed_regions(self) -> tuple[CandidateRegion, ...]:
        return tuple(region for region in self.regions if region.is_analysed)

    @property
    def frames_planned(self) -> int:
        return sum(len(region.keyframes) for region in self.regions)

    @property
    def keyframes(self) -> tuple[float, ...]:
        """Every instant the model will see, in chronological order."""
        return tuple(
            sorted(
                timestamp for region in self.regions for timestamp in region.keyframes
            )
        )

    @property
    def was_capped(self) -> bool:
        return self.dropped_regions > 0

    @property
    def coverage(self) -> float:
        """Fraction of the source inside an analysed region.

        The honest headline for §60: "the model looked at 4 % of this
        recording, chosen by four detectors" is the claim the cascade actually
        supports.
        """
        if self.duration_seconds <= 0:
            return 0.0
        covered = sum(region.duration for region in self.analysed_regions)
        return min(covered / self.duration_seconds, 1.0)

    def summary(self) -> dict[str, Any]:
        return {
            "regions": len(self.regions),
            "analysed_regions": len(self.analysed_regions),
            "dropped_regions": self.dropped_regions,
            "frames_planned": self.frames_planned,
            "frame_budget": self.frame_budget,
            "coverage": round(self.coverage, 4),
            "capped": self.was_capped,
        }


# ---------------------------------------------------------------------------
# triggers
# ---------------------------------------------------------------------------


def triggers_from_audio(events: Iterable[AudioEvent], config: AnalysisConfig) -> list[Trigger]:
    """Nominate where the audio jumped (§16, §18).

    Spikes and transients only. Silence and speech are states, not events, and
    a detector that nominated every voiced second would nominate the whole
    recording.
    """
    if AUDIO_SPIKE not in config.vision.candidate_detectors:
        return []
    wanted = {AudioEventType.SPIKE, AudioEventType.TRANSIENT}
    return [
        Trigger(
            source=AUDIO_SPIKE,
            start_seconds=event.start_seconds,
            end_seconds=event.end_seconds,
            confidence=event.confidence,
            detail={"event_type": event.event_type.value, "track": event.track_role},
        )
        for event in events
        if event.event_type in wanted
    ]


def triggers_from_scenes(result: SceneResult, config: AnalysisConfig) -> list[Trigger]:
    """Nominate shot boundaries (§16, §17).

    Confidence scales with the measured change, normalised against the
    detector's own threshold: a boundary that only just crossed it is weaker
    evidence than one that crossed it four times over.
    """
    if SCENE_CHANGE not in config.vision.candidate_detectors:
        return []
    threshold = max(result.threshold, 1e-6)
    triggers: list[Trigger] = []
    for scene in result.scenes[1:]:
        score = scene.change_score
        confidence = 0.5 if score is None else min(score / (threshold * 4.0), 1.0)
        triggers.append(
            Trigger(
                source=SCENE_CHANGE,
                start_seconds=scene.start_seconds,
                end_seconds=scene.start_seconds,
                confidence=max(confidence, 0.1),
                detail={"change_score": score, "scene_index": scene.index},
            )
        )
    return triggers


def triggers_from_transcript(
    segments: Iterable[TranscriptSegment], config: AnalysisConfig
) -> list[Trigger]:
    """Nominate where the player was talking (§16, §14).

    Weak on its own — a commentary-heavy recording is speech throughout — which
    is exactly why it is weighted by agreement rather than counted alone.
    """
    if SPEECH_ACTIVITY not in config.vision.candidate_detectors:
        return []
    return [
        Trigger(
            source=SPEECH_ACTIVITY,
            start_seconds=segment.start,
            end_seconds=segment.end,
            confidence=min(segment.confidence or 0.5, 1.0),
            detail={"words": len(segment.words)},
        )
        for segment in segments
        if segment.end > segment.start
    ]


def triggers_from_frame_difference(
    scores: Sequence[tuple[float, float]], config: AnalysisConfig
) -> list[Trigger]:
    """Nominate where the picture moved between sampled frames (§16).

    Distinct from scene detection: a scene boundary is a cut, while this is
    motion *within* a shot. A firefight has no cuts at all and is exactly the
    thing the cascade must not miss.
    """
    if FRAME_DIFFERENCE not in config.vision.candidate_detectors:
        return []
    threshold = config.vision.frame_difference_threshold
    return [
        Trigger(
            source=FRAME_DIFFERENCE,
            start_seconds=timestamp,
            end_seconds=timestamp,
            confidence=min(score, 1.0),
            detail={"difference": round(score, 4)},
        )
        for timestamp, score in scores
        if score >= threshold
    ]


def frame_difference_scores(
    frames: Sequence[tuple[float, Path]],
) -> list[tuple[float, float]]:
    """Score how much each sampled frame differs from the one before it.

    Frames are decoded straight to a 64x64 grey thumbnail using the JPEG
    decoder's own scaling, so a 2 400-frame pass costs seconds. The score is
    the mean absolute difference in 0-1, which is what
    ``analysis.vision.frame_difference_threshold`` is expressed in.

    Returns an empty list if the imaging libraries are unavailable — the
    cascade then runs on its other detectors rather than failing (§95).
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:  # pragma: no cover - reported by doctor.py
        logger.warning("Pillow is unavailable; frame-difference nomination is disabled")
        return []

    scores: list[tuple[float, float]] = []
    previous: Any = None
    for timestamp, path in frames:
        try:
            with Image.open(path) as image:
                # draft() lets libjpeg decode at reduced scale, which is far
                # cheaper than decoding full size and resizing afterwards.
                image.draft("L", DIFFERENCE_THUMBNAIL)
                thumbnail = image.convert("L").resize(DIFFERENCE_THUMBNAIL)
                current = np.asarray(thumbnail, dtype=np.float32) / 255.0
        except (OSError, ValueError):
            continue
        if previous is not None:
            scores.append((timestamp, float(np.mean(np.abs(current - previous)))))
        previous = current
    return scores


# ---------------------------------------------------------------------------
# the cascade
# ---------------------------------------------------------------------------


def build_candidates(
    triggers: Iterable[Trigger],
    config: AnalysisConfig,
    *,
    duration_seconds: float,
    frame_budget: int | None = None,
) -> CandidatePlan:
    """Turn detector nominations into the plan the vision stage executes.

    Args:
        triggers: everything the cheap detectors nominated.
        config: ``analysis`` — supplies the roll, the per-request frame count
            and the hourly ceiling.
        duration_seconds: source length, which is what the budget scales with.
        frame_budget: overrides the computed ceiling. Tests use it; production
            does not.

    The steps are: widen each trigger into a span, merge overlapping spans,
    score each merged region by agreement and evidence strength, then allocate
    frames in priority order until the budget is spent.
    """
    sampling = config.frame_sampling
    budget = (
        frame_budget
        if frame_budget is not None
        else _frame_budget(duration_seconds, config.vision.max_frames_per_source_hour)
    )

    spans = _widen(triggers, sampling, duration_seconds)
    merged = _merge(spans, max_seconds=_max_region_seconds(sampling))
    if not merged:
        return CandidatePlan(
            regions=(), duration_seconds=duration_seconds, frame_budget=budget
        )

    scored = sorted(
        (_score(start, end, group, config) for start, end, group in merged),
        key=lambda region: (-region.priority, region.start_seconds),
    )

    per_region = max(config.vision.max_frames_per_request, 1)
    allocated: list[CandidateRegion] = []
    remaining = budget
    dropped = 0
    for region in scored:
        if remaining <= 0:
            # Kept in the plan without keyframes: the evidence is still real,
            # and a later re-run with a larger budget can reach it.
            allocated.append(region)
            dropped += 1
            continue
        count = min(per_region, remaining)
        allocated.append(
            CandidateRegion(
                start_seconds=region.start_seconds,
                end_seconds=region.end_seconds,
                sources=region.sources,
                priority=region.priority,
                triggers=region.triggers,
                keyframes=_keyframes(region, count),
            )
        )
        remaining -= count

    allocated.sort(key=lambda region: region.start_seconds)
    plan = CandidatePlan(
        regions=tuple(allocated),
        duration_seconds=duration_seconds,
        frame_budget=budget,
        dropped_regions=dropped,
    )
    if plan.was_capped:
        logger.warning(
            "The frame budget could not cover every candidate region",
            extra=plan.summary(),
        )
    else:
        logger.info("Built candidate plan", extra=plan.summary())
    return plan


def _frame_budget(duration_seconds: float, per_hour: int) -> int:
    """Frames the model may see for a source of this length (§15).

    Rounded up, and never below one hour's worth: a ninety-second clip should
    not be analysed more sparsely than its length implies is possible.
    """
    if duration_seconds <= 0:
        return 0
    hours = duration_seconds / SECONDS_PER_HOUR
    return max(math.ceil(per_hour * hours), 1)


def _widen(
    triggers: Iterable[Trigger], sampling, duration_seconds: float
) -> list[tuple[float, float, Trigger]]:
    """Expand each trigger into the span §16 says to analyse around it.

    Pre-roll and post-roll are the point: an event is rarely legible from its
    own instant, and the seconds leading up to it are what make it a moment.
    """
    widened: list[tuple[float, float, Trigger]] = []
    for trigger in triggers:
        start = max(trigger.start_seconds - sampling.candidate_pre_roll_seconds, 0.0)
        end = trigger.end_seconds + sampling.candidate_post_roll_seconds
        if duration_seconds > 0:
            end = min(end, duration_seconds)
        if end > start:
            widened.append((start, end, trigger))
    return widened


def _merge(
    spans: list[tuple[float, float, Trigger]], *, max_seconds: float
) -> list[tuple[float, float, list[Trigger]]]:
    """Merge overlapping spans, keeping every trigger that contributed.

    Two detectors nominating the same explosion must produce one region, not
    two — otherwise the same seconds are analysed twice and the budget pays for
    it twice.

    Merging is bounded, and the bound is what makes the cascade work at all.
    Unbounded, a recording with something loud every thirty seconds collapses
    into a single region spanning two hours, which then receives four keyframes
    — four frames to describe two hours, while the plan reports full coverage.
    Past ``max_seconds`` a new region starts instead, so a long firefight
    becomes several regions that each get their own keyframes.
    """
    if not spans:
        return []
    ordered = sorted(spans, key=lambda item: item[0])
    merged: list[tuple[float, float, list[Trigger]]] = []
    for start, end, trigger in ordered:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end, group = merged[-1]
            extended = max(previous_end, end)
            if extended - previous_start <= max_seconds:
                merged[-1] = (previous_start, extended, [*group, trigger])
                continue
        merged.append((start, end, [trigger]))
    return merged


def _max_region_seconds(sampling) -> float:
    """How long one candidate region may be.

    Derived from the sampling configuration rather than picked: a region is
    what the dense pass covers, so its natural size is the roll on each side
    plus the span ``max_frames_per_candidate`` frames occupy at the candidate
    interval. With the shipped defaults that is one minute — long enough for a
    firefight, short enough that four keyframes still describe it.
    """
    dense = sampling.max_frames_per_candidate * sampling.candidate_interval_seconds
    return max(
        sampling.candidate_pre_roll_seconds + sampling.candidate_post_roll_seconds + dense,
        1.0,
    )


def _score(
    start: float, end: float, triggers: list[Trigger], config: AnalysisConfig
) -> CandidateRegion:
    """Rank a region by who nominated it and how strongly.

    Agreement carries most of the weight. One very loud noise is weaker
    evidence than three independent detectors noticing the same instant, and
    ranking by intensity alone would spend the whole budget on the loudest
    minute of a firefight.
    """
    sources = frozenset(trigger.source for trigger in triggers)
    enabled = max(len(config.vision.candidate_detectors), 1)
    agreement = min(len(sources) / enabled, 1.0)
    strength = max((trigger.confidence for trigger in triggers), default=0.0)
    priority = AGREEMENT_WEIGHT * agreement + (1.0 - AGREEMENT_WEIGHT) * min(strength, 1.0)
    return CandidateRegion(
        start_seconds=start,
        end_seconds=end,
        sources=sources,
        priority=priority,
        triggers=tuple(triggers),
    )


def _keyframes(region: CandidateRegion, count: int) -> tuple[float, ...]:
    """Choose the instants inside a region that the model will see.

    **Where the detectors pointed**, strongest first. A region is a *widened
    and merged* span — the median one measured 57 seconds — and the triggers
    inside it are the whole reason it exists: an audio spike at second 3 and a
    shot change at second 4 are the events, and the fifty seconds around them
    are context that was added to hold them.

    Spreading the frames evenly across that span was the original rule, and it
    threw the nomination away. Measured on a real region: four frames landed
    at 11.4, 22.8, 34.2 and 45.6 seconds while the triggers sat at 3, 4 and
    31 — **8.4 seconds** between the loudest thing in the recording and the
    nearest frame anybody looked at. Across two real projects the events the
    pipeline could not name sat a median of 12-13 seconds from the nearest
    analysed frame, against 2-3 seconds for the ones it could.

    Falls back to the even spread when a region has no triggers to aim at,
    which is the shape the old rule was right about.
    """
    if count <= 0 or region.duration <= 0:
        return ()

    aimed = _aimed_at_triggers(region, count)
    if aimed:
        return aimed

    if count == 1:
        return (round(region.midpoint, 3),)
    step = region.duration / (count + 1)
    return tuple(
        round(region.start_seconds + step * (index + 1), 3) for index in range(count)
    )


#: Two frames closer together than this look at the same instant twice. A
#: burst of triggers a few hundred milliseconds apart is one event, and
#: spending the whole region's budget on it would blind the rest of the span.
_KEYFRAME_SPACING_SECONDS: Final[float] = 2.0


def _aimed_at_triggers(region: CandidateRegion, count: int) -> tuple[float, ...]:
    """Instants on the region's own triggers, strongest first.

    Ordered by confidence and then by agreement in time: when two detectors
    fired within a couple of seconds of each other they are describing one
    moment, and one frame answers for both. Whatever budget survives that is
    spread across the parts of the region no trigger claimed, so a long region
    is not left with four frames bunched at one end.
    """
    if not region.triggers:
        return ()

    chosen: list[float] = []
    for trigger in sorted(region.triggers, key=lambda item: -item.confidence):
        at = min(max(trigger.midpoint, region.start_seconds), region.end_seconds)
        if any(abs(at - taken) < _KEYFRAME_SPACING_SECONDS for taken in chosen):
            continue
        chosen.append(at)
        if len(chosen) >= count:
            break

    chosen.extend(_fill_the_gaps(region, chosen, count - len(chosen)))
    return tuple(round(value, 3) for value in sorted(chosen))


def _fill_the_gaps(
    region: CandidateRegion, taken: Sequence[float], spare: int
) -> list[float]:
    """Spend leftover frames on the widest unwatched stretches of a region."""
    if spare <= 0:
        return []
    added: list[float] = []
    for _ in range(spare):
        edges = sorted([region.start_seconds, *taken, *added, region.end_seconds])
        widest = max(pairwise(edges), key=lambda pair: pair[1] - pair[0])
        middle = (widest[0] + widest[1]) / 2.0
        if widest[1] - widest[0] < _KEYFRAME_SPACING_SECONDS:
            break
        added.append(middle)
    return added


__all__ = [
    "AGREEMENT_WEIGHT",
    "AUDIO_SPIKE",
    "FRAME_DIFFERENCE",
    "HUD_CHANGE",
    "SCENE_CHANGE",
    "SPEECH_ACTIVITY",
    "CandidatePlan",
    "CandidateRegion",
    "Trigger",
    "build_candidates",
    "frame_difference_scores",
    "triggers_from_audio",
    "triggers_from_frame_difference",
    "triggers_from_scenes",
    "triggers_from_transcript",
]
