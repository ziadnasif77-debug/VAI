"""What a shot is *for* (V2-P0).

Interpretation, not measurement. Every number here is read from evidence the
analysis stages already stored -- scenes, vision observations, transcript
segments, audio events, correlated game events, and the semantic lanes where a
session has them. Nothing new is inferred, no model is called, and no table is
written. That is the constraint this module was built under, and it is also
what makes it cheap enough to run for every moment of every style on every
re-edit.

## Dead time, redefined

The pipeline has always had a `dead_time_score`, and it has always been zero.
Not through a bug in a threshold: `backend.moments.dead_time` searches *the
stretches no moment's context occupies*, and `dead_time_ratio` then asks how
much of those stretches fall inside a moment's context window. The producer and
the consumer look at disjoint regions of the timeline by construction, so the
answer cannot be anything but zero -- and it has not been, on any of the 435
moments this machine has stored.

Fixing the arithmetic would have been a one-line change and the wrong one. The
question that matters is not "how much of this stretch was quiet"; it is what
the owner asked for:

> a dead stretch is one that adds no **context**, no **anticipation**, no
> **progression**, no **payoff** and no **reaction**

Five ways a piece of footage can earn its place in a video. A shot that does
none of them is dead however loud it is, and a silent stretch that sets up the
next thing is not dead at all. So :class:`EditorialValue` scores the five, and
deadness is what is left over -- `1 - the strongest of them`.

This is a stricter claim than "quiet", and a more useful one. It is also
falsifiable: each of the five is derived from a named store, and a shot that
scores zero on all five names which stores were empty.

## What it does not do

It does not decide anything. The strategy layer decides, this layer reads --
the same separation `backend.moments.dead_time` states in its own docstring
and for the same reason. In particular, **nothing here reaches the optimiser
unless a style asks for it**: the house edit is frozen, and giving a term that
has been zero since the first migration a real value would change every video
this system has ever made. That change is available, it is a decision, and it
is not one this module gets to make quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger
from backend.editorial.evidence import EditorialEvidence

logger = get_logger("editorial.semantics", LogChannel.PIPELINE)

#: How many named events inside a span count as "fully active".
#:
#: Three, because two is a coincidence and four is a montage. Measured across
#: this machine's 435 moments, a span with three or more named events is one a
#: person would describe as "something happening" without qualification.
BUSY_EVENTS: Final[int] = 3

#: How many distinct vision labels count as an established place.
#:
#: A shot showing one thing is showing a thing; a shot showing four is showing
#: a *situation*, which is what "context" means when an editor says it.
ESTABLISHED_LABELS: Final[int] = 4

#: Words spoken after a shot that count as a full reaction.
#:
#: Eight. Short enough that "oh my god" plus a name clears it, long enough that
#: a single filler word does not turn every shot into a reaction shot.
REACTION_WORDS: Final[int] = 8

#: Event types that are a resolution rather than an occurrence.
#:
#: The event-store answer to "did this pay off". A kill is a thing that
#: happened; a victory is a thing that *concluded*, and only the second is a
#: payoff without the tension lane to say so. Taken from the correlator's own
#: vocabulary rather than invented here, so a new event type is a deliberate
#: addition to this set rather than a silent omission from it.
RESOLVING: Final[frozenset[str]] = frozenset(
    {
        "victory",
        "defeat",
        "boss_defeat",
        "clutch",
        "comeback",
        "outplay",
        "escape",
        "objective",
        "objective_failure",
        "multi_kill",
    }
)

#: The furthest past a shot's end a response is followed.
#:
#: Eight seconds, the same window the reading looks ahead over. A transcript
#: segment can run to 392 seconds on this machine's coarser recordings, and a
#: shot held until one of those ends would be following the transcriber rather
#: than making an edit.
RESPONSE_WINDOW: Final[float] = 8.0

#: Below this, a shot's strongest editorial claim is too weak to name, and the
#: purpose comes back DEAD rather than as the least bad of five poor options.
NAMED_FLOOR: Final[float] = 0.15


class ShotPurpose(str, Enum):
    """What this shot is doing in the edit.

    Deliberately five and not fifteen. Each name here is one an editor would
    use out loud, and each is separable from the others by evidence rather than
    by taste -- a distinction that needs a model to make is not one this layer
    is allowed to offer.
    """

    SETUP = "setup"
    ANTICIPATION = "anticipation"
    ACTION = "action"
    PAYOFF = "payoff"
    REACTION = "reaction"
    #: None of the five, by enough of a margin to say so.
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class EditorialValue:
    """The five ways a stretch of footage can earn its place.

    Each 0..1, and **independent**: a shot can be both a payoff and a reaction,
    and forcing those to share a budget would make a good shot look like two
    mediocre ones. They are combined only at the point something needs a single
    answer, and then by `max` rather than by sum.
    """

    #: It shows where we are, or who is involved.
    context: float = 0.0
    #: Something is coming, and this is the run-up to it.
    anticipation: float = 0.0
    #: Something is happening, here, now.
    progression: float = 0.0
    #: Something that was building has let go.
    payoff: float = 0.0
    #: Somebody responded to it.
    reaction: float = 0.0

    #: Which stores were empty for this shot, named. A zero from "we looked and
    #: there was nothing" and a zero from "nobody looked" are different facts,
    #: and only the second is a reason to distrust the score.
    unobserved: tuple[str, ...] = ()

    @property
    def best(self) -> float:
        """The strongest single claim this shot makes."""
        return max(
            self.context,
            self.anticipation,
            self.progression,
            self.payoff,
            self.reaction,
        )

    @property
    def dead_weight(self) -> float:
        """How dead this shot is, editorially: what is left after its best claim.

        The number `backend.narrative.optimizer` has been multiplying by zero
        since the first migration. It is not offered to the optimiser from
        here -- a style asks for it through `EditingStrategy` -- but this is
        what it means when one does.
        """
        return round(max(0.0, 1.0 - self.best), 4)

    @property
    def is_blind(self) -> bool:
        """Whether the score rests on nothing, and should not be acted on."""
        return len(self.unobserved) >= 4

    def as_dict(self) -> dict[str, Any]:
        return {
            "context": round(self.context, 3),
            "anticipation": round(self.anticipation, 3),
            "progression": round(self.progression, 3),
            "payoff": round(self.payoff, 3),
            "reaction": round(self.reaction, 3),
            "dead_weight": self.dead_weight,
            "unobserved": list(self.unobserved),
        }


@dataclass(frozen=True, slots=True)
class ShotSemantics:
    """One moment, interpreted: what it is for, and what it is worth.

    Holds no span of its own. The shot's boundaries belong to the evidence and
    to the moment, and a third copy of them here would be a third thing able to
    disagree.
    """

    moment_id: str
    purpose: ShotPurpose = ShotPurpose.DEAD
    value: EditorialValue = field(default_factory=EditorialValue)

    #: Seconds past the shot's end at which the response finishes, or 0.
    #:
    #: Read by `ReactionPolicy`, which is the only thing that may act on it.
    #: Capped at the reading window: a transcript segment can run for minutes,
    #: and holding a shot until a two-minute segment ends would be following a
    #: transcriber's idea of a sentence rather than making an edit.
    response_seconds: float = 0.0

    #: Why this purpose and not another, in one clause a person can check.
    why: str = ""

    @property
    def dead_weight(self) -> float:
        return self.value.dead_weight

    @property
    def carries_speech(self) -> bool:
        """Whether cutting this tight would cut somebody off mid-sentence."""
        return self.purpose is ShotPurpose.REACTION

    def as_dict(self) -> dict[str, Any]:
        return {
            "moment_id": self.moment_id,
            "purpose": self.purpose.value,
            "why": self.why,
            **self.value.as_dict(),
        }


def read(evidence: EditorialEvidence, *, inside: Any = None, after: Any = None) -> ShotSemantics:
    """Interpret one shot from evidence that already exists.

    Args:
        evidence: the shot, as :mod:`backend.editorial.evidence` read it.
        inside: the projection over the moment's own span, when the caller has
            it. Without one the counts fall back to what the evidence carries,
            which is fewer signals and says so.
        after: the projection over the stretch following the moment. This is
            the only way to see anticipation and reaction, both of which are
            claims about what happens *next*.

    The five components are derived from different stores on purpose. A reading
    where every component came from the same signal would be one number wearing
    five hats, and would move all five ways at once whenever that signal was
    noisy.
    """
    unobserved: list[str] = []
    if not evidence.observed:
        unobserved.append("stores")
    if evidence.unknown and "during" in evidence.unknown:
        unobserved.append("lanes")

    context = _context(evidence, unobserved)
    progression = _progression(evidence, inside, unobserved)
    anticipation = _anticipation(evidence, inside, after, unobserved)
    payoff = _payoff(evidence, inside, after)
    reaction = _reaction(evidence, after, unobserved)

    value = EditorialValue(
        context=context,
        anticipation=anticipation,
        progression=progression,
        payoff=payoff,
        reaction=reaction,
        unobserved=tuple(dict.fromkeys(unobserved)),
    )
    purpose, why = _purpose(value)
    return ShotSemantics(
        moment_id=evidence.moment_id,
        purpose=purpose,
        value=value,
        response_seconds=_response_seconds(evidence, after)
        if purpose is ShotPurpose.REACTION
        else 0.0,
        why=why,
    )


def _response_seconds(evidence: EditorialEvidence, after: Any) -> float:
    """How long after the shot ends the response takes to finish.

    Measured from the transcript rather than from the speech lane, because the
    lane is a level and this needs an edge. The *first* response's end, not the
    last: holding a shot until every overlapping segment is done would follow
    a transcriber's paragraph rather than a person's sentence.
    """
    if after is None:
        return 0.0
    end = evidence.source_end
    # Only speech that *begins* after the shot is a response to it. Speech
    # already running through the shot is commentary, and the segment carrying
    # it can run for minutes -- the first version took the latest end of
    # everything overlapping, which meant every reaction shot asked for the
    # bound and the policy became a flat three seconds wearing a measurement's
    # clothes. Every hold it produced was exactly 3.00s, which is what gave it
    # away.
    starts_after = [
        float(getattr(said, "end", 0.0) or 0.0)
        for said in (getattr(after, "said", ()) or ())
        if float(getattr(said, "start", 0.0) or 0.0) >= end
    ]
    if not starts_after:
        return 0.0
    past = min(starts_after) - end
    return round(min(max(past, 0.0), RESPONSE_WINDOW), 3)


# -- the five, each from its own evidence -----------------------------------


def _context(evidence: EditorialEvidence, unobserved: list[str]) -> float:
    """Does this shot establish something?

    From the vision labels across the widened span: a stretch showing several
    distinct things is showing a place and the people in it, which is what an
    editor keeps a shot for when nothing in particular happens in it.
    """
    if not evidence.subjects:
        unobserved.append("vision")
        return 0.0
    # One label is a thing being on screen, which every shot has. The claim
    # starts at two, so the count is measured from there rather than from zero.
    beyond_one = len(evidence.subjects) - 1
    return round(min(1.0, max(0, beyond_one) / (ESTABLISHED_LABELS - 1)), 4)


def _progression(evidence: EditorialEvidence, inside: Any, unobserved: list[str]) -> float:
    """Is something happening here?

    Named events inside the moment's own span. `unknown_event` is excluded
    upstream, so this counts things a detector could put a name to rather than
    things that merely registered.
    """
    named = _named_count(inside, fallback=len(evidence.events))
    if named == 0 and inside is None and not evidence.events:
        unobserved.append("events")
    density = min(1.0, named / BUSY_EVENTS)
    # The lanes refine it where a session has them: a busy stretch that the
    # intensity lane also calls busy is a stronger claim than either alone.
    if evidence.during.intensity > 0:
        density = max(density, min(1.0, evidence.during.intensity))
    return round(density, 4)


def _anticipation(
    evidence: EditorialEvidence, inside: Any, after: Any, unobserved: list[str]
) -> float:
    """Is this the run-up to something?

    Two shapes, and the shot's own is the one that fires. A moment is formed
    *around* an event, so "more events after it than in it" is nearly
    impossible by construction -- measured across 293 shots on this machine it
    fired once. What is common, and what an editor actually means by a run-up,
    is a shot that **builds**: quiet at the top, and the thing it is about
    arriving some way in.

    So the primary reading is how much of the shot precedes its first named
    event. That is a claim about this shot rather than about the silence
    around it, it is available on every recording, and it is exactly what
    `ContextPolicy` needs in order to know which shots can be tightened from
    the front and which must not be.
    """
    lead_in = _lead_in(evidence, inside)

    ahead = _named_count(after, fallback=0)
    if after is None:
        unobserved.append("after")
        # The lanes can still answer it: tension higher afterwards than during
        # is the same statement in a different store.
        if evidence.after.tension > evidence.during.tension:
            return round(max(lead_in, min(1.0, evidence.after.tension)), 4)
        return round(lead_in, 4)

    here = _named_count(inside, fallback=len(evidence.events))
    follows = 0.0 if ahead <= here else min(1.0, (ahead - here) / BUSY_EVENTS)
    return round(max(lead_in, follows), 4)


def _lead_in(evidence: EditorialEvidence, inside: Any) -> float:
    """How much of the shot happens before the first thing happens in it.

    A shot whose event lands six seconds in has a six-second run-up; one that
    opens on the event has none. Expressed as a share of the shot so it is
    comparable across a two-second reaction and a ninety-second fight, and
    doubled because a run-up occupying half a shot is already as anticipatory
    as footage gets.
    """
    if inside is None or evidence.duration <= 0:
        return 0.0
    starts = [
        float(getattr(event, "start_seconds", 0.0))
        for event in (getattr(inside, "named_events", ()) or ())
    ]
    if not starts:
        return 0.0
    quiet = min(starts) - evidence.source_start
    if quiet <= 0:
        return 0.0
    return min(1.0, (quiet / evidence.duration) * 2.0)


def _payoff(evidence: EditorialEvidence, inside: Any, after: Any) -> float:
    """Did something that was building let go?

    With lanes this is measurable: `resolves` says the tension the moment
    carried was lower afterwards, and how much lower says how strongly.

    Without them it is *not* inferable from activity dying down. The first
    version read "things happened here and then stopped" as a payoff, which
    made every isolated burst of action a payoff -- three quarters of the
    shots on this machine, including ones where the action simply moved
    elsewhere. Activity ending and tension resolving are different facts and
    the event store only holds the first.

    What it does hold is **outcomes**. A shot containing a victory, a defeat,
    a clutch or a boss going down is a resolution by the correlator's own
    naming, whatever the lanes say -- and that is a claim about this shot
    rather than about the silence after it.
    """
    # V2-P2.2: strongest evidence first, which is the event and its own
    # timestamp. The editorial span locates a resolution when one is really
    # there -- a resolving event with a span of its own, or the tension lane
    # letting go at a nameable second -- and carries the confidence of
    # whatever established it.
    #
    # This used to sit *below* the lane check, and the ordering was the bug.
    # `resolves` is true for any tension decrease at all, so a fall of 0.016
    # returned a payoff of 0.032 and a located victory at confidence 0.89 was
    # never reached. A weak signal preempting a strong one is not a fallback
    # chain, it is a first-match-wins list in the wrong order.
    span = getattr(evidence, "span", None)
    located = getattr(span, "resolution", None) if span is not None else None
    if located is not None:
        return round(min(1.0, max(0.0, float(located.confidence))), 4)

    if evidence.resolves:
        drop = evidence.during.tension - evidence.after.tension
        return round(min(1.0, max(0.0, drop) * 2.0), 4)

    outcomes = sum(1 for name in _inside_names(inside, evidence) if name in RESOLVING)
    if not outcomes:
        return 0.0
    return round(min(1.0, outcomes / max(BUSY_EVENTS - 1, 1)), 4)


def _inside_names(inside: Any, evidence: EditorialEvidence) -> tuple[str, ...]:
    """Named event types inside the shot, from the projection or the evidence."""
    if inside is None:
        return evidence.events
    found = []
    for event in getattr(inside, "named_events", ()) or ():
        name = str(getattr(getattr(event, "event_type", None), "value", "") or "")
        if name:
            found.append(name)
    return tuple(found)


def _reaction(evidence: EditorialEvidence, after: Any, unobserved: list[str]) -> float:
    """Did anybody respond to it?

    Speech that starts after the shot and was not in it. The `reaction_follows`
    reading says this from the lanes; the transcript says it directly, and says
    it directly, and it says it where the lanes cannot be built at all.

    An earlier version of this comment claimed fifteen of seventeen projects
    here have no semantic lanes. That was wrong: `load_timeline` builds them
    when they are not stored, so only the *cache* was empty for fifteen.
    """
    if evidence.reaction_follows:
        return 1.0
    if after is None:
        unobserved.append("speech")
        return 0.0
    spoken = getattr(after, "words", None)
    words = len(spoken().split()) if callable(spoken) else 0
    if not words:
        return 0.0
    # Speech that was already running through the shot is not a reaction to
    # it -- it is commentary, and it would make every shot in a talkative
    # recording look like a punchline.
    if evidence.during.speaking or len(evidence.speech.split()) >= REACTION_WORDS:
        return 0.0
    return round(min(1.0, words / REACTION_WORDS), 4)


# -- naming it ---------------------------------------------------------------


def _purpose(value: EditorialValue) -> tuple[ShotPurpose, str]:
    """The strongest claim, named -- or DEAD when none of them is strong.

    Order matters where two claims tie. Payoff beats action because a payoff
    is an action that also resolved something, and calling it merely an action
    loses the part that made it worth keeping; reaction beats setup for the
    same reason in the other direction.
    """
    ranked = (
        (value.payoff, ShotPurpose.PAYOFF, "tension let go here"),
        (value.reaction, ShotPurpose.REACTION, "somebody responded to it"),
        (value.progression, ShotPurpose.ACTION, "things happen inside it"),
        (value.anticipation, ShotPurpose.ANTICIPATION, "it runs up to what follows"),
        (value.context, ShotPurpose.SETUP, "it establishes where this is"),
    )
    best = max(ranked, key=lambda item: item[0])
    if best[0] < NAMED_FLOOR:
        return ShotPurpose.DEAD, (
            "no context, anticipation, progression, payoff or reaction"
            if not value.unobserved
            else f"nothing was recorded here ({', '.join(value.unobserved)})"
        )
    return best[1], best[2]


def _named_count(projection: Any, *, fallback: int) -> int:
    if projection is None:
        return fallback
    return len(getattr(projection, "named_events", ()) or ())


__all__ = [
    "BUSY_EVENTS",
    "ESTABLISHED_LABELS",
    "NAMED_FLOOR",
    "REACTION_WORDS",
    "RESPONSE_WINDOW",
    "RESOLVING",
    "EditorialValue",
    "ShotPurpose",
    "ShotSemantics",
    "read",
]
