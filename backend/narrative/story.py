"""The three video modes (SPEC sections 35, 36).

§35 defines them, and the difference is what each optimises for:

* **Story** — hook, context, build-up, event, escalation, climax, reaction,
  ending. §36: *optimises narrative coherence, not merely moment score. A
  slightly weaker moment may be selected when it creates necessary context.*
* **Best Moments** — the strongest moments, with variety enforced. No arc is
  imposed, and none is pretended.
* **Compilation** — moments grouped by type. A different product: the viewer is
  browsing kinds of thing, not following a session.

All three share the same input — §32's ranked, explained moments — and the same
duration constraint. What differs is selection and order, which is why they live
together: three files that each re-derive "pick moments to fill 20 minutes"
would be three places for that to drift.

§36's rule has a concrete consequence in `story`: a moment's value for a
*narrative slot* is not its score. A tense build-up scores modestly and is the
only thing that can fill the build-up slot, so it wins that slot over a
higher-scoring kill that would make the arc nonsense.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ai.providers.base import TranscriptSegment
from backend.config.schema import NarrativeConfig
from backend.core.duration import DurationPolicy
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import MomentType, VideoMode
from backend.moments.formation import Moment, replace_moment
from backend.narrative import pacing, refinement
from backend.narrative.hook import HookSelection, choose_hook
from backend.narrative.optimizer import OptimisationResult, optimise

logger = get_logger("narrative.story", LogChannel.PIPELINE)

#: Which moment types can fill each §35 story beat. A beat with no candidate is
#: simply absent -- a session with no defeat has no "reaction to defeat" beat,
#: and inventing one would mean inventing footage.
BEAT_TYPES: Final[dict[str, frozenset[MomentType]]] = {
    "hook": frozenset({MomentType.EPIC, MomentType.CLUTCH, MomentType.FUNNY,
                       MomentType.SURPRISE}),
    "context": frozenset({MomentType.DISCOVERY, MomentType.TENSION, MomentType.SKILL}),
    "build_up": frozenset({MomentType.TENSION, MomentType.SKILL, MomentType.DISCOVERY,
                           MomentType.RARE}),
    "event": frozenset({MomentType.SKILL, MomentType.OUTPLAY, MomentType.FUNNY,
                        MomentType.FAIL, MomentType.SURPRISE}),
    "escalation": frozenset({MomentType.CHAOS, MomentType.CLUTCH, MomentType.RAGE,
                             MomentType.COMEBACK, MomentType.TENSION}),
    "climax": frozenset({MomentType.EPIC, MomentType.CLUTCH, MomentType.BOSS,
                         MomentType.COMEBACK, MomentType.OUTPLAY}),
    "reaction": frozenset({MomentType.REACTION, MomentType.FUNNY, MomentType.RAGE}),
    "ending": frozenset({MomentType.VICTORY, MomentType.DEFEAT, MomentType.BOSS,
                         MomentType.COMEBACK}),
}


@dataclass(frozen=True, slots=True)
class NarrativePlan:
    """The edit, before it becomes a timeline.

    Ordered clips, the hook, and an account of how the plan was reached. Phase 8
    turns this into an EDL; nothing here touches video.
    """

    mode: VideoMode
    moments: tuple[Moment, ...]
    hook: HookSelection
    optimisation: OptimisationResult
    pacing: pacing.PacingReport
    #: Which story beat each moment fills, by index. Empty outside story mode.
    beats: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_seconds(self) -> float:
        return sum(moment.context_duration for moment in self.moments)

    @property
    def is_empty(self) -> bool:
        return not self.moments

    @property
    def within_target(self) -> bool:
        return self.optimisation.within_tolerance

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "clips": len(self.moments),
            "total_seconds": round(self.total_seconds, 2),
            "target_seconds": round(self.optimisation.target_seconds, 2),
            "within_target": self.within_target,
            "hook": self.hook.summary(),
            "pacing": self.pacing.summary(),
            "beats": list(self.beats),
            "notes": list(self.notes),
        }


def build_plan(
    moments: Sequence[Moment],
    *,
    mode: VideoMode,
    target_seconds: float,
    config: NarrativeConfig,
    policy: DurationPolicy,
    chronological: bool = False,
    speech: Mapping[str, Sequence[TranscriptSegment]] | None = None,
    media_durations: Mapping[str, float] | None = None,
) -> NarrativePlan:
    """Turn ranked moments into an ordered edit of the requested length (§35-§39).

    The sequence is the same for every mode: choose a subset that fits the
    duration (§39), order it for the mode, then pick a hook and measure the
    pacing. Duration is settled first because it is the only hard constraint --
    everything else is a preference, and preferences that break the constraint
    produce a video of the wrong length.

    Args:
        chronological: play the recording's own order, start to finish. In a
            story edit the hook is the only thing that breaks chronology -- a
            teaser from the middle, then back to the beginning -- and someone
            watching a session they played themselves reads that single jump as
            the whole edit being shuffled. Measured on a real project: with the
            hook, one backwards jump in fourteen clips; without it, none.
    """
    if not moments:
        return _empty(mode, target_seconds, policy)

    selection = optimise(
        moments,
        target_seconds=target_seconds,
        config=config.optimizer,
        policy=policy,
        # So a clip that grows to close a shortfall grows into footage that
        # exists: the recording's own end is the only ceiling the optimiser
        # cannot infer from the moments themselves.
        media_durations=media_durations,
    )
    if selection.is_empty:
        return _empty(mode, target_seconds, policy, notes=selection.notes)

    if mode is VideoMode.STORY:
        ordered, beats, notes = _story_order(selection.moments, config)
    elif mode is VideoMode.COMPILATION:
        ordered, notes = _compilation_order(selection.moments, config)
        beats = ()
    else:
        ordered, notes = _best_moments_order(selection.moments, config)
        beats = ()

    if mode is not VideoMode.STORY:
        # §38's run-breaker swaps a differently-typed clip backwards to break
        # a same-type streak. For best-moments and compilation that is pure
        # win; for a story it is a second way to scramble the session's
        # chronology, and the first viewer's verdict on the first scramble
        # applies verbatim. Variety in story mode is the optimiser's job at
        # selection time (§33), not a reorder at the end.
        ordered = pacing.order(ordered, config.pacing)
    if chronological:
        # An empty selection rather than None: "no hook" is a state the type
        # already models, with a reason attached, and every reader of a plan
        # expects a HookSelection to be there.
        hook = HookSelection(moment=None, reason="the edit was asked to run in time order")
        notes = [*notes, "chronological: no hook, so time only runs forwards"]
    else:
        hook = choose_hook(ordered, config.hook)
        ordered = _apply_hook(ordered, hook, config)

    if speech:
        # Last, deliberately: these are the boundaries the viewer will see, and
        # every earlier stage -- the optimiser's duration trim above all -- has
        # had its say. Measured before this existed, 8 of 26 cut points on a
        # finished video landed mid-sentence (§29 snapped them; §39 re-cut them).
        refined = refinement.refine(
            ordered,
            beats,
            speech,
            snap_window_seconds=config.refinement.snap_window_seconds,
            min_pause_seconds=config.refinement.min_pause_seconds,
            pause_split=config.refinement.pause_split,
            min_trailing_seconds=config.refinement.min_trailing_seconds,
            core_give_seconds=config.refinement.core_give_seconds,
            stretch_window_seconds=config.refinement.stretch_window_seconds,
            duration_by_media=media_durations,
            enabled=config.refinement.enabled,
        )
        ordered, beats = list(refined.moments), list(refined.beats)
        notes = [*notes, *refined.notes]
        # The totals the selection reported were measured before the snap
        # drifted them, and `within_target` reads them (§39).
        selection = dataclasses.replace(
            selection,
            total_seconds=sum(moment.context_duration for moment in ordered),
        )

    plan = NarrativePlan(
        mode=mode,
        moments=tuple(ordered),
        hook=hook,
        optimisation=selection,
        pacing=pacing.report(ordered, config.pacing),
        beats=tuple(beats),
        notes=(*selection.notes, *notes),
        metadata={"candidates": len(moments)},
    )
    logger.info("Built the narrative plan", extra=plan.summary())
    return plan


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def _story_order(
    moments: Sequence[Moment], config: NarrativeConfig
) -> tuple[list[Moment], list[str], list[str]]:
    """Arrange moments along the §35 structure, coherence first (§36).

    The beats choose **roles**, not positions. Each beat takes the best
    available moment of a fitting type -- that decides which clip carries the
    climax, which the ending -- and then everything plays in the order the
    session played, with the beat riding along as a label for pacing and
    captions.

    The first version ordered by beat, §35's sequence imposed on the footage,
    and the first hour-long session shipped a video that jumped 0:00 → 51:15 →
    38:34 → 66:36 -- four backwards leaps in twelve clips. The viewer called
    it "not a story, it is fragmented", and they were right: a single
    session's causality *is* its story. Game state visibly rewinds when the
    footage does. The one deliberate flash-forward remains the hook (§37),
    which announces itself as one.

    Chronology is per recording (media_id first in the key): two recordings in
    one project each keep their own internal order rather than interleaving by
    unrelated clocks.
    """
    story = config.story
    remaining = list(moments)
    role_of: dict[tuple[str, float], str] = {}

    for beat in story.structure:
        allowed = BEAT_TYPES.get(beat, frozenset())
        candidates = [moment for moment in remaining if moment.moment_type in allowed]
        if not candidates:
            continue
        best = max(
            candidates,
            key=lambda moment: (
                story.coherence_weight * _beat_fit(moment, beat)
                + story.score_weight * moment.score
            ),
        )
        remaining.remove(best)
        role_of[(best.media_id, best.start_seconds)] = beat

    notes: list[str] = []
    if story.require_climax and "climax" not in role_of.values():
        notes.append("no moment strong enough to serve as a climax")

    ordered = sorted(moments, key=lambda m: (m.media_id, m.context_start))
    beats = [role_of.get((m.media_id, m.start_seconds), "body") for m in ordered]
    return ordered, beats, notes


def _beat_fit(moment: Moment, beat: str) -> float:
    """How well a moment suits a beat, beyond merely being an allowed type.

    Narrative position matters: the scorer already measured how structural a
    moment is, and a beat that closes the video wants a moment that closes
    something.
    """
    structural = float(moment.score_breakdown.get("narrative", 0.5))
    if beat in {"climax", "ending"}:
        return structural
    if beat in {"context", "build_up"}:
        # A build-up should not be the loudest thing in the video.
        return 1.0 - float(moment.score_breakdown.get("audio", 0.5)) * 0.5
    return 0.5 + structural * 0.5


def _best_moments_order(
    moments: Sequence[Moment], config: NarrativeConfig
) -> tuple[list[Moment], list[str]]:
    """Strongest first, with types interleaved (§35).

    No arc is imposed and none is claimed. Interleaving is the only structure:
    without it the mode degenerates into the monotony §33 warns about.
    """
    ordered = sorted(moments, key=lambda moment: -moment.score)
    if not config.best_moments.interleave_types:
        return ordered, []

    interleaved: list[Moment] = []
    pending = list(ordered)
    previous: MomentType | None = None
    while pending:
        index = next(
            (i for i, moment in enumerate(pending) if moment.moment_type is not previous), 0
        )
        chosen = pending.pop(index)
        interleaved.append(chosen)
        previous = chosen.moment_type
    return interleaved, ["types interleaved to avoid runs of the same kind"]


def _compilation_order(
    moments: Sequence[Moment], config: NarrativeConfig
) -> tuple[list[Moment], list[str]]:
    """Group by type, in the configured order (§35)."""
    compilation = config.compilation
    priority = {name: index for index, name in enumerate(compilation.group_order)}
    ordered = sorted(
        moments,
        key=lambda moment: (
            priority.get(moment.moment_type.value, len(priority)),
            -moment.score,
        ),
    )
    groups = len({moment.moment_type for moment in ordered})
    return ordered, [f"grouped into {groups} type(s)"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _apply_hook(
    moments: Sequence[Moment], hook: HookSelection, config: NarrativeConfig
) -> list[Moment]:
    """Move the hook to the front (§37).

    When replay is allowed the moment stays in its chronological place as well,
    which is the cold-open convention. When it is not, the hooked span is
    **trimmed out of** the body copy rather than the copy being dropped: the
    hook is usually the tail of its moment, and dropping the whole moment threw
    away the setup along with the repeat. The first real viewer saw the old
    behaviour as 25 seconds shown twice, and the fix for "shown twice" must not
    be "the first half never shown at all".
    """
    if not hook.exists:
        return list(moments)

    body: list[Moment] = []
    for moment in moments:
        if not _same_moment(moment, hook.moment):
            body.append(moment)
            continue
        if config.hook.allow_replay_in_body:
            body.append(moment)
            continue
        remainder = _without_hooked_span(moment, hook.moment, config)
        if remainder is not None:
            body.append(remainder)
    opening = replace_moment(
        hook.moment, metadata={**hook.moment.metadata, "role": "hook"}
    )
    return [opening, *body]


def _without_hooked_span(
    moment: Moment, hook: Moment, config: NarrativeConfig
) -> Moment | None:
    """The body copy with the hook's seconds removed, or ``None`` if nothing
    watchable remains.

    The hook is trimmed from the front by :func:`~backend.narrative.hook._fit`,
    so it occupies the *tail* of its moment and the remainder is normally the
    lead-up. Both sides are computed anyway and the longer kept, so a future
    change to how hooks are cut cannot quietly reintroduce the repeat.
    """
    left = (moment.context_start, min(hook.context_start, moment.context_end))
    right = (max(hook.context_end, moment.context_start), moment.context_end)
    start, end = max((left, right), key=lambda span: span[1] - span[0])
    if end - start < config.hook.min_seconds:
        return None
    return replace_moment(
        moment,
        context_start=start,
        context_end=end,
        start_seconds=min(max(moment.start_seconds, start), end),
        end_seconds=max(min(moment.end_seconds, end), start),
        metadata={
            **moment.metadata,
            "hook_span_removed_seconds": round(
                moment.context_duration - (end - start), 2
            ),
        },
    )


def _same_moment(left: Moment, right: Moment | None) -> bool:
    if right is None:
        return False
    return (
        left.media_id == right.media_id
        and abs(left.start_seconds - right.start_seconds) < 1e-6
    )


def _empty(
    mode: VideoMode,
    target_seconds: float,
    policy: DurationPolicy,
    *,
    notes: Sequence[str] = (),
) -> NarrativePlan:
    from backend.narrative.hook import HookSelection
    from backend.narrative.optimizer import OptimisationResult

    return NarrativePlan(
        mode=mode,
        moments=(),
        hook=HookSelection(moment=None, reason="nothing to choose from"),
        optimisation=OptimisationResult(
            moments=(),
            target_seconds=target_seconds,
            total_seconds=0.0,
            value=0.0,
            within_tolerance=False,
            notes=tuple(notes),
        ),
        pacing=pacing.report([], _default_pacing()),
        notes=("no moments were available to build an edit from", *notes),
    )


def _default_pacing():
    from backend.config.schema import PacingConfig

    return PacingConfig()


__all__ = ["BEAT_TYPES", "NarrativePlan", "build_plan"]
