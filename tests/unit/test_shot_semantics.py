"""What a shot is for, read from evidence that already exists (V2-P0).

Two things are being protected here.

**The redefinition.** `dead_time_score` was zero on all 435 moments this
machine has stored, structurally rather than by accident, and the fix was not
to repair the arithmetic but to ask a different question: does this stretch add
context, anticipation, progression, payoff or reaction? Each of those five is
derived from a *different* store on purpose, so the tests below check them one
at a time with the others starved -- a component that quietly read the same
signal as its neighbour would pass a combined test and fail these.

**That the reading reaches real moments at all.** It did not, for the whole of
V2-P11. `EditorialReading` looked up `moment.id`, the real `Moment` had no such
attribute, `getattr` returned the default, and every project produced an empty
reading. No test saw it because every fixture in this suite declares an `id`
field the real class does not have -- the stub was more capable than the thing
it stood for. `TestTheRealMomentClass` uses the real class for exactly that
reason.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.core.models.enums import GameEventType, MomentType
from backend.editorial.evidence import EditorialEvidence, State
from backend.editorial.semantics import (
    BUSY_EVENTS,
    RESPONSE_WINDOW,
    ESTABLISHED_LABELS,
    NAMED_FLOOR,
    REACTION_WORDS,
    EditorialValue,
    ShotPurpose,
    read,
)
from backend.moments.formation import Moment

pytestmark = pytest.mark.unit


@dataclass
class _Said:
    start: float
    end: float


@dataclass
class _Projection:
    """What `backend.evidence.project` hands back, reduced to what is read."""

    events: tuple = ()
    said: str = ""

    @property
    def named_events(self) -> tuple:
        return self.events

    def words(self, limit: int = 0) -> str:
        return self.said

@dataclass
class _Event:
    event_type: GameEventType = GameEventType.KILL
    is_named: bool = True
    #: Where inside the shot it lands. Defaults to the shot's own start, so a
    #: test that does not care about the run-up does not accidentally create
    #: one.
    start_seconds: float = 40.0


def _events(count: int) -> tuple:
    return tuple(_Event() for _ in range(count))


def _evidence(**overrides) -> EditorialEvidence:
    """A shot with nothing going for it, so each test adds exactly one thing."""
    base = {
        "media_id": "media-1",
        "moment_id": "mom-1",
        "source_start": 40.0,
        "source_end": 58.0,
        "subjects": (),
        "events": (),
        "observed": True,
    }
    return EditorialEvidence(**{**base, **overrides})


class TestDeadTimeRedefined:
    """Deadness is what is left after a shot's strongest claim."""

    def test_a_shot_with_no_claim_at_all_is_entirely_dead(self) -> None:
        assert EditorialValue().dead_weight == 1.0

    def test_deadness_is_one_minus_the_strongest_claim(self) -> None:
        value = EditorialValue(context=0.2, payoff=0.75, reaction=0.4)
        assert value.best == 0.75
        assert value.dead_weight == 0.25

    def test_claims_do_not_share_a_budget(self) -> None:
        """A shot that is both a payoff and a reaction is not two half-shots.

        The temptation is to sum, which would make a shot doing two things
        score higher than one doing a single thing perfectly -- and would make
        deadness depend on how many ways a stretch was described rather than
        on how strong the best description was.
        """
        one = EditorialValue(payoff=0.8)
        both = EditorialValue(payoff=0.8, reaction=0.8)
        assert one.dead_weight == both.dead_weight

    def test_a_silent_stretch_that_sets_something_up_is_not_dead(self) -> None:
        """The whole point of the redefinition, as one assertion.

        Nothing happens inside, nobody speaks, the lanes are flat -- and four
        distinct things are on screen, which is what an editor keeps a shot
        for when it establishes where this is.
        """
        semantics = read(
            _evidence(subjects=("car", "street", "player", "npc")),
            inside=_Projection(),
            after=_Projection(),
        )
        assert semantics.purpose is ShotPurpose.SETUP
        assert semantics.dead_weight == 0.0


class TestEachComponentReadsItsOwnStore:
    """Five components, five stores. Starve four, check the fifth still fires."""

    def test_context_comes_from_vision_labels(self) -> None:
        semantics = read(
            _evidence(subjects=tuple(f"thing-{n}" for n in range(ESTABLISHED_LABELS))),
            inside=_Projection(),
            after=_Projection(),
        )
        assert semantics.value.context == 1.0
        assert semantics.value.progression == 0.0

    def test_progression_comes_from_named_events_inside(self) -> None:
        semantics = read(
            _evidence(),
            inside=_Projection(events=_events(BUSY_EVENTS)),
            after=_Projection(),
        )
        assert semantics.value.progression == 1.0
        assert semantics.purpose is ShotPurpose.ACTION

    def test_anticipation_comes_from_events_that_follow(self) -> None:
        """A quiet stretch that runs up to something is the one quiet an edit
        must not cut, and it is invisible to anything reading only the span."""
        semantics = read(
            _evidence(),
            inside=_Projection(),
            after=_Projection(events=_events(BUSY_EVENTS)),
        )
        assert semantics.value.anticipation == 1.0
        assert semantics.purpose is ShotPurpose.ANTICIPATION

    def test_a_shot_that_builds_is_anticipating_its_own_event(self) -> None:
        """The reading that actually fires on this footage.

        A moment is formed *around* an event, so "more events after than
        inside" is nearly impossible by construction -- measured across 293
        shots here, it fired once. A shot whose event lands some way in is
        the common case and the one an editor means by a run-up.
        """
        opens_on_it = read(
            _evidence(),
            inside=_Projection(events=(_Event(start_seconds=40.0),)),
            after=_Projection(),
        )
        builds = read(
            _evidence(),
            inside=_Projection(events=(_Event(start_seconds=49.0),)),
            after=_Projection(),
        )
        assert opens_on_it.value.anticipation == 0.0
        assert builds.value.anticipation == 1.0

    def test_the_run_up_is_a_share_of_the_shot_not_a_number_of_seconds(self) -> None:
        """So a two-second reaction and a ninety-second fight are comparable."""
        short = read(
            _evidence(source_start=0.0, source_end=10.0),
            inside=_Projection(events=(_Event(start_seconds=2.5),)),
            after=_Projection(),
        )
        long = read(
            _evidence(source_start=0.0, source_end=100.0),
            inside=_Projection(events=(_Event(start_seconds=25.0),)),
            after=_Projection(),
        )
        assert short.value.anticipation == long.value.anticipation

    def test_payoff_is_a_resolution_not_merely_activity_ending(self) -> None:
        """Things happening and then stopping is not a payoff.

        The first version read it that way and made three quarters of the
        shots on this machine payoffs, including ones where the action simply
        moved elsewhere. What the event store can actually say is that
        something *concluded*.
        """
        busy = read(
            _evidence(),
            inside=_Projection(events=_events(4)),
            after=_Projection(),
        )
        assert busy.value.payoff == 0.0

        resolved = read(
            _evidence(),
            inside=_Projection(events=(_Event(event_type=GameEventType.VICTORY),)),
            after=_Projection(),
        )
        assert resolved.value.payoff > 0.0
        assert resolved.purpose is ShotPurpose.PAYOFF

    def test_reaction_comes_from_speech_that_starts_afterwards(self) -> None:
        semantics = read(
            _evidence(),
            inside=_Projection(),
            after=_Projection(said=" ".join(["word"] * REACTION_WORDS)),
        )
        assert semantics.value.reaction == 1.0
        assert semantics.purpose is ShotPurpose.REACTION

    def test_speech_running_through_the_shot_is_not_a_reaction_to_it(self) -> None:
        """Otherwise every shot in a talkative recording is a punchline."""
        semantics = read(
            _evidence(speech=" ".join(["talking"] * (REACTION_WORDS + 2))),
            inside=_Projection(),
            after=_Projection(said=" ".join(["word"] * REACTION_WORDS)),
        )
        assert semantics.value.reaction == 0.0


class TestTheLanesRefineButAreNotRequired:
    """A reading must still work when the lanes will not load.

    An earlier version of this docstring said fifteen of this machine's
    seventeen projects have no semantic lanes, which was wrong and was
    repeated into `semantics.py`. Only two have them *cached*:
    `load_timeline` says "stored when they are current, built when not", so
    every project gets lanes and most pay for them at read time. What these
    tests actually protect is the failure path -- a recording whose lanes
    cannot be built at all, which is a real state and the one the reading
    reports as `unknown` rather than as calm footage.
    """

    def test_a_reading_without_lanes_still_names_a_purpose(self) -> None:
        semantics = read(
            _evidence(unknown=("before", "during", "after")),
            inside=_Projection(events=_events(BUSY_EVENTS)),
            after=_Projection(),
        )
        assert semantics.purpose is not ShotPurpose.DEAD
        assert "lanes" in semantics.value.unobserved

    def test_lanes_raise_a_weak_event_reading(self) -> None:
        """One event is not much; one event in a stretch the intensity lane
        also calls hot is more than the event store alone can say."""
        without = read(_evidence(), inside=_Projection(events=_events(1)), after=_Projection())
        with_lanes = read(
            _evidence(during=State(intensity=0.9)),
            inside=_Projection(events=_events(1)),
            after=_Projection(),
        )
        assert with_lanes.value.progression > without.value.progression

    def test_the_lanes_can_answer_payoff_on_their_own(self) -> None:
        semantics = read(
            _evidence(during=State(tension=0.9), after=State(tension=0.1)),
            inside=_Projection(),
            after=_Projection(),
        )
        assert semantics.value.payoff > 0.0


class TestNamingThePurpose:
    def test_a_claim_below_the_floor_is_not_named(self) -> None:
        """DEAD rather than the least bad of five poor options."""
        semantics = read(
            _evidence(subjects=("one",)),
            inside=_Projection(),
            after=_Projection(),
        )
        assert semantics.value.context < NAMED_FLOOR
        assert semantics.purpose is ShotPurpose.DEAD

    def test_a_payoff_that_is_also_an_action_is_called_a_payoff(self) -> None:
        """Calling it merely an action loses the part that made it worth keeping."""
        semantics = read(
            _evidence(),
            inside=_Projection(
                events=tuple(
                    _Event(event_type=GameEventType.VICTORY) for _ in range(BUSY_EVENTS)
                )
            ),
            after=_Projection(),
        )
        assert semantics.value.progression == semantics.value.payoff == 1.0
        assert semantics.purpose is ShotPurpose.PAYOFF

    def test_the_reason_says_which_store_was_empty(self) -> None:
        semantics = read(_evidence(observed=False), inside=None, after=None)
        assert semantics.purpose is ShotPurpose.DEAD
        assert "nothing was recorded" in semantics.why

    def test_a_reading_resting_on_nothing_says_so(self) -> None:
        semantics = read(_evidence(observed=False), inside=None, after=None)
        assert semantics.value.is_blind


class TestTheRealMomentClass:
    """The stub was more capable than the class, and hid a live defect.

    Every fixture in this suite declares `id` on its fake moment. The real
    `Moment` did not have one, so `getattr(moment, "id", "")` returned the
    default everywhere the editorial layer looked -- and the reading came back
    empty on every project for the whole of V2-P11 without a single test going
    red. These use the real class.
    """

    @staticmethod
    def _moment(**metadata) -> Moment:
        return Moment(
            media_id="media-1",
            moment_type=MomentType.EPIC,
            start_seconds=10.0,
            end_seconds=20.0,
            events=(),
            context_start=8.0,
            context_end=22.0,
            metadata=dict(metadata),
        )

    def test_a_stored_moment_knows_its_own_id(self) -> None:
        assert self._moment(id="mom-abc").id == "mom-abc"

    def test_an_unsaved_moment_has_no_id_rather_than_a_wrong_one(self) -> None:
        assert self._moment().id == ""

    def test_the_id_is_the_one_the_rest_of_the_pipeline_writes(self) -> None:
        """`timeline.builder` and `story_worker` both read `metadata["id"]`.

        If this ever diverges, a clip's `moment_id` stops matching the shot the
        editorial layer read for it, and every join between them silently
        returns nothing -- which is the failure this whole class exists for.
        """
        moment = self._moment(id="mom-xyz")
        assert moment.id == str(moment.metadata.get("id"))


class TestHowLongTheResponseRuns:
    """`response_seconds` is what `ReactionPolicy` holds a shot for.

    It has to be a measurement. The first version took the latest end of every
    transcript segment overlapping the look-ahead window, and on this machine's
    coarser recordings those segments run to 392 seconds -- so every reaction
    shot asked for the policy's bound and the hold became a flat three seconds
    wearing a measurement's clothes. Every one of the 33 holds it produced was
    exactly 3.00s, which is what gave it away. These tests hold it to being a
    number that varies with the footage.
    """

    def test_it_measures_the_first_response_not_the_last_segment(self) -> None:
        semantics = read(
            _evidence(source_start=40.0, source_end=58.0),
            inside=_Projection(),
            after=_Segments((58.5, 60.0), (58.5, 300.0)),
        )
        assert semantics.purpose is ShotPurpose.REACTION
        assert semantics.response_seconds == 2.0

    def test_speech_already_running_is_not_a_response_to_hold_for(self) -> None:
        """A segment that began before the shot ended is commentary carrying
        on, and holding the shot for it would follow the transcriber."""
        semantics = read(
            _evidence(source_start=40.0, source_end=58.0),
            inside=_Projection(),
            after=_Segments((10.0, 300.0)),
        )
        assert semantics.response_seconds == 0.0

    def test_it_is_bounded_by_the_reading_window(self) -> None:
        semantics = read(
            _evidence(source_start=40.0, source_end=58.0),
            inside=_Projection(),
            after=_Segments((58.5, 400.0)),
        )
        assert semantics.response_seconds == RESPONSE_WINDOW

    def test_a_shot_nobody_responded_to_carries_no_response(self) -> None:
        semantics = read(_evidence(), inside=_Projection(), after=_Projection())
        assert semantics.response_seconds == 0.0


class _Segments:
    """A projection whose `said` records carry real start and end times."""

    def __init__(self, *spans):
        self.said = tuple(_Said(start, end) for start, end in spans)
        self.events = ()

    @property
    def named_events(self) -> tuple:
        return ()

    def words(self, limit: int = 0) -> str:
        return "word " * REACTION_WORDS
