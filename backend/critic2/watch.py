"""Watching the finished video, and naming what is wrong with it.

Every detector here measures the *assembly* -- the thing a viewer meets --
rather than the source it came from. That is the whole reason the module
exists: three similar shots in a row, a gesture that fires twice in ten
seconds, an ending that stops rather than ends are all invisible to a stage
that reads the recording, because none of them is a property of the recording.

Two kinds of evidence, and the split is deliberate. Frames sampled from the
render answer "does this look like the last one"; the plan answers "was this
supposed to". A defect needs both to be worth acting on: a repeated look is
fine if it is one continuous situation, and a quiet stretch is fine if the
audio director asked for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.critic2.models import ANSWERS, EditCorrection, Evidence, Finding
from backend.semantic.reader import SemanticReader

logger = get_logger("critic2.watch", LogChannel.QA)

#: How alike two shots' descriptions must be before they are the same shot
#: twice. Measured on labels rather than pixels: a viewer forgives an
#: identical frame and notices an identical *situation*.
REPEAT_OVERLAP: Final[float] = 0.75

#: How many similar shots in a row before it reads as repetition rather than
#: as a sequence. Two is a pair; three is a pattern.
REPEAT_RUN: Final[int] = 3

#: A tail this much quieter than its own shot has stopped being the shot.
TAIL_FRACTION: Final[float] = 0.45
#: ...and must be at least this long to be worth trimming.
MIN_TAIL_SECONDS: Final[float] = 1.5

#: The opening has this long to earn the rest of the video.
HOOK_SECONDS: Final[float] = 15.0

#: The last shot is measured against the edit's own average heat.
ENDING_RATIO: Final[float] = 0.7

#: The window the density is read over.
WINDOW_SECONDS: Final[float] = 10.0

#: More than this many effects in ten seconds is a pile, not emphasis.
EFFECTS_PER_TEN_SECONDS: Final[float] = 4.0

#: A stretch with no change of level, no speech and no event for this long is
#: where a viewer leaves.
FATIGUE_SECONDS: Final[float] = 25.0


def findings(
    *,
    clips: Sequence[Any],
    looks: dict[str, tuple[str, ...]],
    reader: SemanticReader | None,
    effects: Sequence[Any] = (),
    planned_silences: Sequence[tuple[float, float]] = (),
    duration_seconds: float,
) -> list[Finding]:
    """Everything wrong with this video that can be measured.

    Args:
        clips: the finished timeline's shots, in order.
        looks: ``clip_id -> vision labels`` from frames of the RENDER.
        reader: the session's lanes in programme seconds.
        effects: placed effects, for the density check.
    """
    found: list[Finding] = []
    found += _repetition(clips, looks)
    found += _tails(clips, reader)
    found += _hook(clips, reader)
    found += _ending(clips, reader, duration_seconds)
    found += _effect_overuse(effects, duration_seconds)
    found += _fatigue(clips, reader, planned_silences, duration_seconds)
    found.sort(key=lambda item: item.at_seconds)
    logger.info(
        "Watched the render",
        extra={
            "clips": len(clips),
            "findings": len(found),
            "codes": sorted({item.code for item in found}),
        },
    )
    return found


def _repetition(clips: Sequence[Any], looks: dict[str, tuple[str, ...]]) -> list[Finding]:
    """Three shots in a row that look like the same thing.

    Compared on what the vision model saw in the RENDER, not on moment types:
    two clips typed ``chaos`` can look completely different, and two typed
    differently can be the same corridor twice.
    """
    found: list[Finding] = []
    run: list[Any] = []
    for clip in clips:
        here = set(looks.get(clip.id, ()))
        previous = set(looks.get(run[-1].id, ())) if run else set()
        alike = bool(here and previous) and (
            len(here & previous) / max(len(here | previous), 1) >= REPEAT_OVERLAP
        )
        if alike:
            run.append(clip)
            continue
        if len(run) >= REPEAT_RUN:
            found.append(_repeat_finding(run, looks))
        run = [clip]
    if len(run) >= REPEAT_RUN:
        found.append(_repeat_finding(run, looks))
    return found


def _repeat_finding(run: Sequence[Any], looks: dict[str, tuple[str, ...]]) -> Finding:
    shared = set(looks.get(run[0].id, ()))
    for clip in run[1:]:
        shared &= set(looks.get(clip.id, ()))
    return Finding(
        code="repetition",
        at_seconds=run[0].timeline_start,
        detail=(
            f"{len(run)} shots in a row show the same thing "
            f"({', '.join(sorted(shared)) or 'no distinguishing labels'})"
        ),
        confidence=min(1.0, 0.4 + 0.15 * len(run)),
        measured={
            "shots": len(run),
            "shared_labels": sorted(shared),
            # Named, so the correction acts on what was seen rather than on
            # whatever happens to sit at that second later.
            "clip_ids": [clip.id for clip in run],
        },
    )


def _tails(clips: Sequence[Any], reader: SemanticReader | None) -> list[Finding]:
    """A shot that keeps running after its own moment is over."""
    if reader is None:
        return []
    found: list[Finding] = []
    for clip in clips:
        if clip.duration < MIN_TAIL_SECONDS * 2:
            continue
        whole = reader.intensity_between(clip.timeline_start, clip.timeline_end)
        if whole <= 0.0:
            continue
        cursor = clip.timeline_end
        while cursor - clip.timeline_start > MIN_TAIL_SECONDS:
            step = cursor - 0.5
            if reader.intensity_between(step, cursor) > whole * TAIL_FRACTION:
                break
            cursor = step
        tail = clip.timeline_end - cursor
        if tail >= MIN_TAIL_SECONDS:
            found.append(
                Finding(
                    code="low_intensity_tail",
                    at_seconds=cursor,
                    detail=(
                        f"the shot at {clip.timeline_start:.0f}s runs "
                        f"{tail:.1f}s past its moment"
                    ),
                    confidence=min(1.0, tail / clip.duration + 0.3),
                    measured={"clip_id": clip.id, "tail_seconds": round(tail, 2)},
                )
            )
    return found


def _hook(clips: Sequence[Any], reader: SemanticReader | None) -> list[Finding]:
    """Whether the opening earns the rest of the video."""
    if reader is None or not clips:
        return []
    opening = reader.intensity_between(0.0, min(HOOK_SECONDS, clips[-1].timeline_end))
    whole = reader.intensity_between(0.0, clips[-1].timeline_end)
    if whole <= 0.0 or opening >= whole:
        return []
    ratio = opening / whole
    if ratio > 0.85:
        return []
    return [
        Finding(
            code="weak_hook",
            at_seconds=0.0,
            detail=(
                f"the first {HOOK_SECONDS:.0f}s sit at {ratio:.0%} of the video's own "
                "average -- the opening is quieter than what follows it"
            ),
            confidence=min(1.0, (0.85 - ratio) * 2),
            measured={"opening": round(opening, 3), "video": round(whole, 3)},
        )
    ]


def _ending(
    clips: Sequence[Any], reader: SemanticReader | None, duration_seconds: float
) -> list[Finding]:
    """Whether the video ends on something, or merely stops."""
    if reader is None or not clips:
        return []
    last = clips[-1]
    ending = reader.intensity_between(last.timeline_start, last.timeline_end)
    whole = reader.intensity_between(0.0, duration_seconds)
    if whole <= 0.0 or ending >= whole * ENDING_RATIO:
        return []
    return [
        Finding(
            code="weak_ending",
            at_seconds=last.timeline_start,
            detail=(
                f"the last shot sits at {ending / whole:.0%} of the video's average -- "
                "it stops rather than ends"
            ),
            confidence=min(1.0, (ENDING_RATIO - ending / whole) * 2),
            measured={"clip_id": last.id, "ending": round(ending, 3)},
        )
    ]


def _effect_overuse(effects: Sequence[Any], duration_seconds: float) -> list[Finding]:
    """Where emphasis piles up faster than a viewer can read it."""
    if not effects:
        return []
    # Programme time, not the stored clip-relative time. Reading the latter
    # put every effect of a 250s video inside one ten-second window.
    times = sorted(float(item.timeline_start) for item in effects)
    found: list[Finding] = []
    for index, at in enumerate(times):
        window = [other for other in times[index:] if other - at <= 10.0]
        if len(window) > EFFECTS_PER_TEN_SECONDS:
            found.append(
                Finding(
                    code="effect_overuse",
                    at_seconds=at,
                    detail=f"{len(window)} effects inside ten seconds at {at:.0f}s",
                    confidence=min(1.0, len(window) / (EFFECTS_PER_TEN_SECONDS * 2)),
                    measured={
                        "effects": len(window),
                        "window_seconds": 10.0,
                        "per_minute": round(
                            len(times) / max(duration_seconds / 60.0, 1e-6), 2
                        ),
                    },
                )
            )
            break
    return found


def _fatigue(
    clips: Sequence[Any],
    reader: SemanticReader | None,
    planned_silences: Sequence[tuple[float, float]],
    duration_seconds: float,
) -> list[Finding]:
    """A long stretch where nothing changes.

    Not "quiet" -- quiet is a choice the audio director makes on purpose, and
    the planned silences are excluded for exactly that reason. This is *level*
    not changing, speech absent and no event landing, for long enough that a
    viewer has nothing to hold onto.
    """
    if reader is None or duration_seconds <= FATIGUE_SECONDS:
        return []
    step = 1.0
    start = 0.0
    level = reader.level_for(0.0, step)
    found: list[Finding] = []
    at = step
    while at < duration_seconds:
        here = reader.level_for(at, at + step)
        speaking = reader.value_at("speech", at) >= 0.5
        eventful = reader.value_at("events", at) >= 0.3
        if here != level or speaking or eventful:
            if at - start >= FATIGUE_SECONDS and not _overlaps(start, at, planned_silences):
                found.append(
                    Finding(
                        code="visual_fatigue",
                        at_seconds=start,
                        detail=(
                            f"{at - start:.0f}s at one level with no speech and no event, "
                            f"from {start:.0f}s"
                        ),
                        confidence=min(1.0, (at - start) / (FATIGUE_SECONDS * 2)),
                        measured={"seconds": round(at - start, 1), "level": level},
                    )
                )
            level, start = here, at
        at += step
    return found


def _overlaps(start: float, end: float, spans: Sequence[tuple[float, float]]) -> bool:
    return any(a < end and start < b for a, b in spans)


def corrections(
    found: Sequence[Finding],
    *,
    clips: Sequence[Any],
    effects: Sequence[Any] = (),
    floor_for=None,
    min_confidence: float = 0.5,
    min_clips: int = 4,
) -> tuple[list[EditCorrection], list[str]]:
    """Turn findings into changes, or say why they could not be.

    Only the codes :data:`ANSWERS` gives a verb are acted on. A finding with
    no verb behind it is reported and left alone: inventing a trim that does
    not address what was seen is worse than the defect, because the report
    would then say the problem was handled.
    """
    by_id = {clip.id: clip for clip in clips}
    made: list[EditCorrection] = []
    refused: list[str] = []
    dropped: set[str] = set()

    for finding in found:
        where = f"{finding.code} at {finding.at_seconds:.0f}s"
        if not ANSWERS.get(finding.code):
            refused.append(f"{where}: reported; §42 has no verb that answers it")
            continue
        if finding.confidence < min_confidence:
            refused.append(f"{where}: only {finding.confidence:.2f} sure")
            continue

        if finding.code == "low_intensity_tail":
            _trim_tail(finding, by_id, floor_for, made, refused, where)
        elif finding.code == "repetition":
            _thin_repeat(finding, by_id, clips, dropped, made, refused, where, min_clips)
        elif finding.code == "effect_overuse":
            _thin_effects(finding, effects, made, refused, where)
    return made, refused


def _trim_tail(finding, by_id, floor_for, made, refused, where) -> None:
    """Cut the seconds a shot runs past its own moment."""
    target = finding.measured.get("clip_id")
    clip = by_id.get(target)
    if clip is None:
        refused.append(f"{where}: {target} is not in the edit")
        return
    amount = float(finding.measured.get("tail_seconds", 0.0))
    floor = float(floor_for(clip.id)) if floor_for else 0.8
    if clip.duration - amount < floor:
        refused.append(
            f"{where}: trimming would leave a {clip.duration - amount:.2f}s shot"
        )
        return
    made.append(
        EditCorrection(
            action="trim_end",
            target=target,
            amount=amount,
            reason="low_intensity_tail",
            confidence=finding.confidence,
            evidence=Evidence(
                at_seconds=finding.at_seconds,
                measured=finding.measured,
                note=finding.detail,
            ),
        )
    )


def _thin_repeat(finding, by_id, clips, dropped, made, refused, where, min_clips) -> None:
    """Drop the weakest shot of a repeated run.

    One, not all of them: three shots of the same corridor is a defect, and
    two is a sequence. Removing the run entire would answer "this repeats" by
    deleting the situation, which is a different video rather than a better
    one.
    """
    run = [by_id[item] for item in finding.measured.get("clip_ids", ()) if item in by_id]
    if len(run) < REPEAT_RUN:
        refused.append(f"{where}: the run is no longer in the edit")
        return
    if len(clips) - len(dropped) <= min_clips:
        refused.append(f"{where}: the edit is down to {min_clips} shots already")
        return
    weakest = min(run, key=lambda clip: (clip.score, -clip.duration))
    if weakest.id in dropped:
        return
    dropped.add(weakest.id)
    made.append(
        EditCorrection(
            action="drop",
            target=weakest.id,
            amount=weakest.duration,
            reason="repetition",
            confidence=finding.confidence,
            evidence=Evidence(
                at_seconds=weakest.timeline_start,
                measured={**finding.measured, "dropped_score": round(weakest.score, 3)},
                note=finding.detail,
            ),
        )
    )


def _thin_effects(finding, effects, made, refused, where) -> None:
    """Remove the weakest free-standing effects until the pile reads as one.

    Never a composition member. P4 admits a sentence whole or not at all, and
    a critic that pulls one word out of it undoes the exact guarantee that
    stage exists to make -- so the members are counted toward the pile, which
    is honest, and excluded from the thinning, which is the rule.
    """
    at = finding.at_seconds
    window = [
        item for item in effects if at <= item.timeline_start <= at + WINDOW_SECONDS
    ]
    free = [item for item in window if not item.composition_id]
    excess = len(window) - int(EFFECTS_PER_TEN_SECONDS)
    if excess <= 0:
        refused.append(f"{where}: {len(window)} effects here is not a pile after all")
        return
    if not free:
        refused.append(
            f"{where}: all {len(window)} are composed emphasis, which stays whole"
        )
        return
    if excess > len(free):
        refused.append(
            f"{where}: only {len(free)} of {len(window)} may be thinned; "
            f"the rest are composed"
        )
    free.sort(key=lambda item: (item.strength, item.duration_seconds))
    for item in free[:excess]:
        made.append(
            EditCorrection(
                action="remove_effect",
                target=item.id,
                amount=0.0,
                reason="effect_overuse",
                confidence=finding.confidence,
                evidence=Evidence(
                    at_seconds=item.timeline_start,
                    measured={
                        **finding.measured,
                        "effect": item.effect.value,
                        "strength": round(item.strength, 3),
                    },
                    note=finding.detail,
                ),
            )
        )



__all__ = [
    "EFFECTS_PER_TEN_SECONDS",
    "FATIGUE_SECONDS",
    "HOOK_SECONDS",
    "REPEAT_RUN",
    "corrections",
    "findings",
]
