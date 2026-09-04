"""What this edit is trying to be, and how it will be made (V2-P0).

Two typed objects, and the reason they are objects rather than conditions
scattered through `story.py` and `optimizer.py`: the decisions an edit makes
are not independent. How long a shot holds, where it is cut, how much run-up it
keeps and whether a dead stretch survives are one taste expressed five ways,
and a codebase that stores them as five unrelated `if` statements can hold five
mutually contradictory opinions without anything noticing.

    the brief (EditingIntent)  +  the style (ResolvedEditingPolicy)
                              ↓
                       EditorialIntent      what this edit is trying to be
                              ↓
                      EditingStrategy       how each layer should behave
                    ↙        ↓        ↘
        ContextPolicy   CutPolicy   DeadTimePolicy

## The settings that had nowhere to go

`EditingIntent.dead_time_policy` and `EditingIntent.context_preservation` have
existed since the interaction layer was built. They are parsed out of what the
owner types, echoed back in the confirmation, stored, and learned as
preferences by `backend.preferences.learning`.

**Nothing in the editing pipeline has ever read either of them.** Someone could
write "احذف الأجزاء الميتة", be told the dead-time policy was now aggressive,
and receive byte-identical footage. The dial was connected to the display and
not to the machine.

This module is the missing consumer. It adds no new configuration -- condition
for this phase, and the right one -- it gives two settings that already exist
their first effect on a video.

## Neutral by construction

Every policy here has a neutral value that is the behaviour before it existed,
and `applied_to` returns **the caller's own object by identity** when it is
neutral, exactly as `SelectionPolicy` does. That is what lets the house edit
stay byte-identical while five other styles change: the frozen contract is not
a promise this module tries hard to keep, it is a consequence of returning the
same object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.editorial.bookends import NEUTRAL as NEUTRAL_BOOKENDS
from backend.editorial.bookends import BookendPolicy
from backend.editorial.policy import NEUTRAL as NEUTRAL_SELECTION
from backend.editorial.policy import SelectionPolicy
from backend.editorial.semantics import ShotPurpose, ShotSemantics

logger = get_logger("editorial.strategy", LogChannel.PIPELINE)

#: The furthest a cut may travel to land on a seam the footage already has.
#:
#: Two seconds. Far enough to reach the scene boundary a cut is usually a
#: moment away from, near enough that the shot is still the shot that was
#: chosen -- beyond this the policy stops improving a cut and starts choosing
#: different footage, which is the optimiser's job and not this one's.
MAX_SEAM_DRIFT: Final[float] = 2.0

#: How much of a shot the context policy may take off the front or the back.
#:
#: Bounded for the same reason the selection multipliers are: a policy that
#: could halve a shot would be re-cutting rather than trimming, and the
#: difference between a taste and a defect is whether anything bounds it.
MAX_TRIM_FRACTION: Final[float] = 0.35


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """How much air a shot keeps around the thing it is about.

    The dial `EditingIntent.context_preservation` has always set and nothing
    has ever read.
    """

    #: Share of the run-up that may be trimmed away. 0.0 keeps every shot as
    #: the moment stage expanded it, which is what the pipeline has always done.
    trim_lead_in: float = 0.0
    #: The same for the tail after the event.
    trim_tail: float = 0.0

    #: Never tighten the front of a shot that builds towards its own event.
    #: The run-up *is* the shot in that case, and trimming it turns an
    #: anticipatory shot into an abrupt one.
    protect_anticipation: bool = True
    #: Never trim the tail of a shot somebody reacts to. Cutting a reaction
    #: short is the defect this whole layer is easiest to cause.
    protect_reaction: bool = True

    #: End a shot shortly after the thing it is about was decided (V2-P2.3).
    #:
    #: The one place the editorial event span reaches a cut. Measured before
    #: this existed: a shot containing a victory ran a median of 26 seconds
    #: past its own resolution, and on one moment 110 seconds of nothing after
    #: the thing it was selected for.
    #:
    #: It is here rather than in `CutPolicy` because that is what it is.
    #: Snapping a cut to a seam a second away and ending a shot on its point
    #: are different acts, and `max_drift` -- 1.5 to 2.0 seconds -- correctly
    #: refused the second. Trimming a tail is this policy's job and is fenced
    #: by `MAX_TRIM_FRACTION`, inside which the same 54.7-second move is 30 %
    #: of its shot.
    trim_at_resolution: bool = False

    @property
    def is_neutral(self) -> bool:
        return (
            self.trim_lead_in == 0.0
            and self.trim_tail == 0.0
            and not self.trim_at_resolution
        )

    def bounds_for(
        self,
        semantics: ShotSemantics | None,
        start: float,
        end: float,
        evidence: Any = None,
    ) -> tuple[float, float]:
        """Where this shot should begin and end, given what it is for.

        Returns the caller's own numbers unchanged when the policy is neutral
        or when the shot is one the policy protects. A shot is never extended
        here -- the moment stage decided how much context exists, and this
        layer may decline some of it, not invent more.
        """
        if self.is_neutral or end <= start:
            return start, end
        purpose = semantics.purpose if semantics else None
        span = end - start
        front = 0.0 if (self.protect_anticipation and purpose is ShotPurpose.ANTICIPATION) \
            else min(self.trim_lead_in, MAX_TRIM_FRACTION)
        back = 0.0 if (self.protect_reaction and purpose is ShotPurpose.REACTION) \
            else min(self.trim_tail, MAX_TRIM_FRACTION)
        trimmed_start = start + span * front
        trimmed_end = end - span * back
        settled = self._after_resolution(semantics, evidence, start, end, span)
        if settled is not None:
            trimmed_end = min(trimmed_end, settled)
        if (trimmed_start, trimmed_end) == (start, end):
            return start, end
        return trimmed_start, trimmed_end

    def _after_resolution(
        self,
        semantics: ShotSemantics | None,
        evidence: Any,
        start: float,
        end: float,
        span: float,
    ) -> float | None:
        """Where this shot could end, once what it is about has been decided.

        Four refusals, each for its own reason:

        * a shot somebody responded to keeps its tail, exactly as it does for
          the ordinary trim -- the response is what the shot is for;
        * the **aftermath** wins over the resolution when the span found one,
          because a reaction the reading located is a fact about this shot;
        * the cut must land where the footage allows it, so it moves to the
          nearest safe seam at or after the point rather than mid-word;
        * and the whole move is fenced by `MAX_TRIM_FRACTION`, which is what
          makes this a taste rather than a re-edit.
        """
        if not self.trim_at_resolution or evidence is None:
            return None
        if (
            self.protect_reaction
            and semantics is not None
            and semantics.purpose is ShotPurpose.REACTION
        ):
            return None
        located = getattr(getattr(evidence, "span", None), "resolution", None)
        if located is None:
            return None

        aftermath = getattr(getattr(evidence, "span", None), "aftermath", None)
        point = float(aftermath.seconds if aftermath is not None else located.seconds)
        if not start < point < end:
            return None

        cuts = getattr(evidence, "cuts", None)
        if cuts is not None:
            landed = float(cuts.best_out(point))
            if point <= landed < end and cuts.safe(landed):
                point = landed
        floor = end - span * MAX_TRIM_FRACTION
        return max(point, floor)

    def describe(self) -> str:
        if self.is_neutral:
            return "keeps every shot as it was expanded"
        parts = [
            f"trims {self.trim_lead_in:.0%} off the run-up and "
            f"{self.trim_tail:.0%} off the tail"
        ]
        if self.trim_at_resolution:
            parts.append("and ends a shot once it is decided")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class CutPolicy:
    """Where a cut lands.

    The candidates already exist. `backend.editorial.evidence.CutPoints` reads
    scene boundaries and the speech a cut must not fall inside, and has offered
    `best_in` and `best_out` since V2-P11 with nothing calling either. This
    says whether to take them.
    """

    #: Move a cut onto a seam the footage already has. A cut on a scene change
    #: is invisible; a cut on a round number is a cut.
    snap_to_seams: bool = False
    #: How far it may move to find one.
    max_drift: float = MAX_SEAM_DRIFT

    @property
    def is_neutral(self) -> bool:
        return not self.snap_to_seams

    def resolve(
        self, cuts: Any, start: float, end: float, resolution: float | None = None
    ) -> tuple[float, float]:
        """The in and out points this policy would use.

        Returns the caller's own numbers when neutral, when there is nothing to
        snap to, or when the nearest seam is further than the policy allows.
        Speech is not overridden: `CutPoints.best_in` and `best_out` already
        refuse a point inside somebody's sentence, and a policy that could turn
        that off would be a policy for producing the one defect V2-P3 exists
        to prevent.
        """
        if self.is_neutral or cuts is None:
            return start, end
        moved_in = float(cuts.best_in(start))
        # `resolution` is accepted and deliberately **not** used to move the
        # out-point. V2-P2.2 tried it: a shot containing a victory runs a
        # median of 26 seconds past it, and on one moment 110 seconds, so
        # ending the shot at the first safe seam after the resolution looked
        # obviously right. It is not this policy's decision to make.
        #
        # Snapping a cut to a seam a second or two away and ending a shot on
        # its point are different acts. The first is what `max_drift` bounds,
        # at 1.5-2.0s; the second moved cuts by 54.7 seconds in the case
        # measured, which the bound correctly refused. Widening the bound to
        # let it through would have turned a guard into a formality for the
        # sake of one feature -- and trimming a shot's tail is
        # `ContextPolicy`'s job, bounded at `MAX_TRIM_FRACTION`, where the
        # same 54.7 seconds is 30 % of the shot and inside the fence.
        #
        # The parameter stays so the call site keeps passing what it knows.
        moved_out = float(cuts.best_out(end))
        if abs(moved_in - start) > self.max_drift:
            moved_in = start
        if abs(moved_out - end) > self.max_drift:
            moved_out = end
        # A seam that would invert or empty the shot is not a seam worth having.
        if moved_out <= moved_in:
            return start, end
        return moved_in, moved_out

    def describe(self) -> str:
        return (
            f"snaps cuts to seams within {self.max_drift:.1f}s"
            if self.snap_to_seams
            else "cuts where the moment ends"
        )


@dataclass(frozen=True, slots=True)
class ReactionPolicy:
    """Whether a shot somebody responded to is held until the response lands.

    The one place this layer may *lengthen* a shot. `ContextPolicy` declines
    air the moment stage created and never invents more, on the principle that
    the moment stage decided how much context exists -- and that principle is
    right for context. A response is different: it is a specific thing, at a
    known second, that the shot was cut before reaching.

    Measured on this machine before it existed: of 22 selected shots somebody
    responded to, **18 stopped before the response finished**, by a median of
    two seconds, with the source sitting right there -- a median of 1,357
    unused seconds after the clip.
    """

    #: Hold the shot until the response finishes.
    hold_for_response: bool = False
    #: The most it may be held for. Three seconds covers the median shortfall
    #: of two and most of the tail; beyond that the "response" is a coarse
    #: transcript segment rather than a sentence, and following it would be
    #: editing to a transcriber's punctuation.
    max_extra: float = 3.0

    @property
    def is_neutral(self) -> bool:
        return not self.hold_for_response

    def held_end(self, semantics: ShotSemantics | None, end: float, limit: float) -> float:
        """Where this shot should end so the response is inside it.

        `limit` is how far the recording actually goes. Returns the caller's
        own number when the policy is neutral, when the shot is not one
        somebody responded to, or when there is no room.
        """
        if self.is_neutral or semantics is None:
            return end
        if semantics.purpose is not ShotPurpose.REACTION:
            return end
        needed = float(getattr(semantics, "response_seconds", 0.0) or 0.0)
        if needed <= 0.0:
            return end
        return min(end + min(needed, self.max_extra), limit) if limit > end else end

    def describe(self) -> str:
        return (
            f"holds a shot up to {self.max_extra:.0f}s so the response lands"
            if self.hold_for_response
            else "ends a shot where it was cut, response or not"
        )


@dataclass(frozen=True, slots=True)
class DeadTimePolicy:
    """Whether the redefined deadness reaches the optimiser, and how hard.

    Off by default, and that is the whole reason the house edit survives P0.
    `moment.dead_time_score` has been zero on every moment this system has
    ever stored, so switching it on for everyone would change every video ever
    made by this machine in one commit. It is available, it is a decision, and
    a style makes it.
    """

    #: Whether the optimiser sees editorial deadness at all.
    enabled: bool = False
    #: How much of it to apply, on top of the objective's own penalty weight.
    #: Bounded by `SelectionPolicy`'s own limits when it reaches the optimiser.
    weight: float = 1.0

    @property
    def is_neutral(self) -> bool:
        return not self.enabled

    def score_for(self, semantics: ShotSemantics | None) -> float:
        """What the optimiser should treat as this moment's dead time."""
        if self.is_neutral or semantics is None:
            return 0.0
        if semantics.value.is_blind:
            # A reading resting on nothing is not evidence of deadness. It is
            # evidence that nobody looked, and penalising it would punish a
            # shot for the analysis stage having been quiet.
            return 0.0
        return round(min(1.0, semantics.dead_weight * self.weight), 4)

    def describe(self) -> str:
        return (
            f"penalises editorial dead time at {self.weight:.2f}x"
            if self.enabled
            else "ignores dead time, as every edit before it has"
        )


#: The behaviour before any of this existed. Shared singletons so `is` works.
NEUTRAL_CONTEXT: Final[ContextPolicy] = ContextPolicy()
NEUTRAL_CUT: Final[CutPolicy] = CutPolicy()
NEUTRAL_DEAD_TIME: Final[DeadTimePolicy] = DeadTimePolicy()
NEUTRAL_REACTION: Final[ReactionPolicy] = ReactionPolicy()


@dataclass(frozen=True, slots=True)
class EditorialIntent:
    """What this edit is trying to be.

    The brief and the style, resolved into one answer rather than two sources
    a later stage has to reconcile. Every field is something a downstream layer
    acts on; nothing here is carried for description.
    """

    #: The style that resolved, by name, for the record and the stamp.
    style: str = "default"
    #: What the owner asked to happen to stretches that earn nothing.
    dead_time: str = "balanced"
    #: How much air the owner asked to keep.
    context: str = "medium"
    #: Whether the recording's own order is kept. The constitution, carried
    #: here so a strategy cannot be built that quietly disagrees with it.
    chronological: bool = True

    def describe(self) -> str:
        return (
            f"{self.style}: dead time {self.dead_time}, context {self.context}"
            + ("" if self.chronological else ", free order")
        )


@dataclass(frozen=True, slots=True)
class EditingStrategy:
    """How each layer should behave to produce the intent.

    One object, threaded to the stages that make an edit. Not five arguments:
    the stages have to agree about which taste is cutting, and a signature that
    can carry four of the five is a signature that will eventually carry four
    of the five.
    """

    intent: EditorialIntent = EditorialIntent()
    selection: SelectionPolicy = NEUTRAL_SELECTION
    context: ContextPolicy = NEUTRAL_CONTEXT
    cut: CutPolicy = NEUTRAL_CUT
    dead_time: DeadTimePolicy = NEUTRAL_DEAD_TIME
    #: Where the video begins and ends (V2-P1). The one editorial decision a
    #: chronological edit can still make about its own shape, because choosing
    #: a boundary is not reordering.
    bookends: BookendPolicy = NEUTRAL_BOOKENDS
    #: Whether a shot is held until the response to it lands (V2-P1.4).
    reaction: ReactionPolicy = NEUTRAL_REACTION

    @property
    def is_neutral(self) -> bool:
        """Whether this asks for the edit the pipeline made before P0.

        The frozen contract reads this. A strategy that is neutral must leave
        every stage reaching for the same values it reached for before, and
        the identity returns in each policy are what make that true rather
        than approximately true.
        """
        return (
            self.selection.is_neutral
            and self.context.is_neutral
            and self.cut.is_neutral
            and self.dead_time.is_neutral
            and self.bookends.is_neutral
            and self.reaction.is_neutral
        )

    def describe(self) -> str:
        if self.is_neutral:
            return f"{self.intent.describe()} -- the house edit, unchanged"
        return "; ".join(
            (
                self.intent.describe(),
                self.selection.describe(),
                self.context.describe(),
                self.cut.describe(),
                self.dead_time.describe(),
                self.bookends.describe(),
                self.reaction.describe(),
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "style": self.intent.style,
            "neutral": self.is_neutral,
            "selection": self.selection.as_dict(),
            "context": {
                "trim_lead_in": self.context.trim_lead_in,
                "trim_tail": self.context.trim_tail,
            },
            "cut": {"snap_to_seams": self.cut.snap_to_seams},
            "dead_time": {
                "enabled": self.dead_time.enabled,
                "weight": self.dead_time.weight,
            },
            "reaction": {"hold": self.reaction.hold_for_response},
            "bookends": {
                "opening": self.bookends.trim_weak_opening,
                "ending": self.bookends.trim_weak_ending,
            },
        }


#: What every stage did before this module existed.
NEUTRAL: Final[EditingStrategy] = EditingStrategy()


# -- resolving the brief and the style into one strategy ---------------------

#: What the owner's context dial means, as a share of a shot that may go.
#:
#: Authored, not learned. `high` keeps everything, which is what the pipeline
#: has always done, so a project that says nothing and a project that asks for
#: high context get the same video -- the dial only ever takes air away.
_CONTEXT_TRIM: Final[dict[str, tuple[float, float]]] = {
    "high": (0.00, 0.00),
    "medium": (0.00, 0.00),
    "low": (0.15, 0.20),
    "none": (0.30, 0.35),
}

#: What the owner's dead-time dial means.
_DEAD_TIME_WEIGHT: Final[dict[str, float]] = {
    "keep": 0.0,
    "balanced": 0.0,
    "aggressive": 1.0,
}


def resolve(
    policy: Any = None, *, intent: Any = None, style_context: Any = None
) -> EditingStrategy:
    """The strategy for one edit, from the brief and the resolved style.

    Args:
        policy: the :class:`~backend.editorial.doctrine.ResolvedEditingPolicy`
            the style produced, or None for the house style.
        intent: the owner's :class:`~backend.interaction.models.EditingIntent`,
            or None when a project carries none.
        style_context: the style's own shot doctrine. Defaults to the one the
            resolved policy carries, so a caller cannot forget to pass it --
            an argument that must be supplied for a feature to work is an
            argument that will be omitted, and the omission looks exactly
            like a style that asked for nothing.

    **Neutral unless something asked for otherwise.** A project with no brief
    and the default style resolves to :data:`NEUTRAL`, which every policy then
    short-circuits by identity. That is the frozen contract, obtained by
    construction rather than by care.
    """
    selection = getattr(policy, "selection", None) or NEUTRAL_SELECTION
    style_name = str(getattr(policy, "name", "") or "default")
    if style_context is None:
        style_context = getattr(policy, "shots", None)

    dead_name = _named(intent, "dead_time_policy", "balanced")
    context_name = _named(intent, "context_preservation", "medium")
    chronological = bool(getattr(intent, "chronological", True))

    trim_front, trim_back = _CONTEXT_TRIM.get(context_name, (0.0, 0.0))
    context = (
        NEUTRAL_CONTEXT
        if (trim_front, trim_back) == (0.0, 0.0)
        else ContextPolicy(trim_lead_in=trim_front, trim_tail=trim_back)
    )

    weight = _DEAD_TIME_WEIGHT.get(dead_name, 0.0)
    dead_time = (
        NEUTRAL_DEAD_TIME if weight <= 0.0 else DeadTimePolicy(enabled=True, weight=weight)
    )

    cut = NEUTRAL_CUT
    ends = NEUTRAL_BOOKENDS
    held = NEUTRAL_REACTION
    if style_context is not None:
        context, cut, dead_time = _from_style(style_context, context, cut, dead_time)
        ends = _bookends(style_context)
        held = _reaction(style_context)

    return EditingStrategy(
        intent=EditorialIntent(
            style=style_name,
            dead_time=dead_name,
            context=context_name,
            chronological=chronological,
        ),
        selection=selection,
        context=context,
        cut=cut,
        dead_time=dead_time,
        bookends=ends,
        reaction=held,
    )


def _from_style(
    doctrine: Any, context: ContextPolicy, cut: CutPolicy, dead_time: DeadTimePolicy
) -> tuple[ContextPolicy, CutPolicy, DeadTimePolicy]:
    """Let a style ask for what the brief did not.

    The brief wins where both speak: the owner typed theirs, and a style
    overriding an explicit instruction is the behaviour that made
    `chronological` a per-project setting the owner had to re-defeat.
    """
    if context.is_neutral:
        front = _number(doctrine, "trim_lead_in", 0.0)
        back = _number(doctrine, "trim_tail", 0.0)
        settle = bool(getattr(doctrine, "trim_at_resolution", False))
        if (front, back) != (0.0, 0.0) or settle:
            context = ContextPolicy(
                trim_lead_in=front, trim_tail=back, trim_at_resolution=settle
            )
    if cut.is_neutral and bool(getattr(doctrine, "snap_to_seams", False)):
        cut = CutPolicy(
            snap_to_seams=True,
            max_drift=min(_number(doctrine, "max_drift", MAX_SEAM_DRIFT), MAX_SEAM_DRIFT),
        )
    if dead_time.is_neutral:
        weight = _number(doctrine, "dead_time_weight", 0.0)
        if weight > 0.0:
            dead_time = DeadTimePolicy(enabled=True, weight=weight)
    return context, cut, dead_time


def _reaction(doctrine: Any) -> ReactionPolicy:
    """Whether the style holds a shot until the response to it lands."""
    if not bool(getattr(doctrine, "hold_for_response", False)):
        return NEUTRAL_REACTION
    return ReactionPolicy(hold_for_response=True)


def _bookends(doctrine: Any) -> BookendPolicy:
    """What the style says about where its videos start and stop."""
    opening = bool(getattr(doctrine, "trim_weak_opening", False))
    ending = bool(getattr(doctrine, "trim_weak_ending", False))
    if not (opening or ending):
        return NEUTRAL_BOOKENDS
    return BookendPolicy(trim_weak_opening=opening, trim_weak_ending=ending)


def _named(intent: Any, field: str, fallback: str) -> str:
    value = getattr(intent, field, None)
    return str(getattr(value, "value", value) or fallback)


def _number(doctrine: Any, field: str, fallback: float) -> float:
    try:
        return float(getattr(doctrine, field, fallback) or fallback)
    except (TypeError, ValueError):
        return fallback


def apply(
    moments: Any,
    strategy: EditingStrategy,
    reading: Any = None,
    durations: Any = None,
) -> Any:
    """Shape the moments the way this strategy asks, before anything selects.

    The single consumer of :class:`ContextPolicy`, :class:`CutPolicy` and
    :class:`DeadTimePolicy`. One pass, before the optimiser sees anything, so
    that what the optimiser receives is a list of moments -- exactly what it
    has always received, exactly as testable as it has always been. The style
    reaches it as shaped input rather than as a taste it would have to read.

    **Returns the caller's own list, by identity, when the strategy is
    neutral.** Not a copy with the same contents: the same object. That is the
    house edit's guarantee, and it is checkable with `is` rather than by
    comparing ninety spans.
    """
    if strategy.is_neutral or not moments:
        return moments

    lengths = dict(durations or {})
    shaped = []
    for moment in moments:
        semantics = reading.semantics_of(moment) if reading is not None else None
        evidence = reading.shot(moment) if reading is not None else None
        shaped.append(
            _shape(
                moment,
                strategy,
                semantics,
                evidence,
                float(lengths.get(moment.media_id, 0.0)),
            )
        )
    return shaped


def _shape(
    moment: Any,
    strategy: EditingStrategy,
    semantics: Any,
    evidence: Any,
    source_length: float = 0.0,
) -> Any:
    """One moment, with the strategy's policies applied in order.

    Context first, then cuts, then the response hold. The context policy says
    how much of the shot to use and the cut policy says where the edges land;
    doing those the other way round would snap to a seam and then trim away
    from it. The hold is last because it is the only one that *lengthens* a
    shot, and running it earlier would let the other two take back the seconds
    it just bought.
    """
    start = float(moment.context_start)
    end = float(moment.context_end)

    start, end = strategy.context.bounds_for(semantics, start, end, evidence)
    located = getattr(getattr(evidence, "span", None), "resolution", None)
    start, end = strategy.cut.resolve(
        getattr(evidence, "cuts", None) if evidence is not None else None,
        start,
        end,
        located.seconds if located is not None else None,
    )
    end = strategy.reaction.held_end(semantics, end, source_length or end)

    # P0.3, brief rule 3: style logic may not widen an authorized span. The
    # policies above may only use what the moment was granted; the reaction
    # hold -- the one policy that lengthens a shot -- stops at the grant, and
    # the refusal is written into the moment's own ledger (§80).
    refusal = None
    governing = _governing_span(moment)
    if governing is not None:
        wanted = (start, end)
        start, end = max(start, governing.start), min(end, governing.end)
        if (start, end) != wanted:
            refusal = (
                f"style asked for [{wanted[0]:.2f}, {wanted[1]:.2f}] beyond the authorized "
                f"span [{governing.start:.2f}, {governing.end:.2f}] granted by "
                f"{governing.granted_by.value}; refused -- a style may not widen (P0.3)"
            )
            logger.info("A style asked past the authorized span; refused", extra={
                "wanted": [round(wanted[0], 2), round(wanted[1], 2)],
                "granted": [round(governing.start, 2), round(governing.end, 2)],
            })

    dead = strategy.dead_time.score_for(semantics)

    unchanged = (start, end) == (moment.context_start, moment.context_end)
    if unchanged and dead == moment.dead_time_score and refusal is None:
        return moment
    shaped = moment.with_context(start, end) if (start, end) != (
        moment.context_start,
        moment.context_end,
    ) else moment
    if dead != shaped.dead_time_score:
        from backend.moments.formation import replace_moment

        shaped = replace_moment(shaped, dead_time_score=dead)
    if refusal is not None:
        from backend.moments.formation import replace_moment

        shaped = replace_moment(shaped, explanation=(*shaped.explanation, refusal))
    return shaped


def _governing_span(moment: Any):
    """The span that bounds what a style may do to this moment, or ``None``
    for a moment that carries no chain (the engine's own bare fixtures)."""
    from backend.moments.grants import spans_of
    from backend.timeline.authorization import newest_for

    chain = spans_of(moment)
    return newest_for(chain, moment.media_id) if chain else None


__all__ = [
    "MAX_SEAM_DRIFT",
    "MAX_TRIM_FRACTION",
    "NEUTRAL",
    "NEUTRAL_CONTEXT",
    "NEUTRAL_CUT",
    "NEUTRAL_DEAD_TIME",
    "BookendPolicy",
    "ContextPolicy",
    "CutPolicy",
    "DeadTimePolicy",
    "EditingStrategy",
    "EditorialIntent",
    "ReactionPolicy",
    "apply",
    "resolve",
]
