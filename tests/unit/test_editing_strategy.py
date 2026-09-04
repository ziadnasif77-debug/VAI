"""The brief and the style, resolved into how each layer behaves (V2-P0).

Three things are protected here, in order of how much damage each would do.

**The house edit.** A neutral strategy must return the caller's own objects,
by identity. Not "a list with the same contents" -- the same list. Every test
in `TestNeutralIsTheSameObject` asserts with `is`, because a copy that happens
to compare equal today is a copy that stops comparing equal the first time a
float moves in the sixth decimal, and the frozen contract would then fail for
a reason nobody could find.

**That each policy is actually wired.** `ContextPolicy`, `CutPolicy` and
`DeadTimePolicy` reach a video through exactly one function, `apply`. A policy
with no caller is the thing this phase was told not to build, so each is tested
through `apply` rather than in isolation.

**That the two dials finally do something.** `dead_time_policy` and
`context_preservation` have been settable since the interaction layer was
written and have never changed a frame. `TestTheDialsThatDidNothing` is the
test that would have failed for the whole of that time.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.core.models.enums import MomentType
from backend.editorial.policy import SelectionPolicy
from backend.editorial.semantics import EditorialValue, ShotPurpose, ShotSemantics
from backend.editorial.strategy import (
    MAX_SEAM_DRIFT,
    MAX_TRIM_FRACTION,
    NEUTRAL,
    ContextPolicy,
    CutPolicy,
    DeadTimePolicy,
    EditingStrategy,
    ReactionPolicy,
    apply,
    resolve,
)
from backend.moments.formation import Moment

pytestmark = pytest.mark.unit


@dataclass
class _Brief:
    """What `EditingIntent` looks like to this module: three named dials."""

    dead_time_policy: str = "balanced"
    context_preservation: str = "medium"
    chronological: bool = True


@dataclass
class _Cuts:
    """Seams at fixed places, and no speech to fall inside."""

    into: tuple = (98.0,)
    out_of: tuple = (161.0,)

    def best_in(self, default: float) -> float:
        usable = [point for point in self.into if point <= default]
        return max(usable) if usable else default

    def best_out(self, default: float) -> float:
        usable = [point for point in self.out_of if point >= default]
        return min(usable) if usable else default

    def safe(self, at: float) -> bool:
        return True


@dataclass
class _Boundary:
    seconds: float
    confidence: float = 0.9
    source: str = "events"
    reason: str = "a victory ends here"


@dataclass
class _Span:
    """What `EditorialEventSpan` looks like to the strategy layer."""

    _resolution: float | None
    _aftermath: float | None

    @property
    def resolution(self):
        return _Boundary(self._resolution) if self._resolution is not None else None

    @property
    def aftermath(self):
        return _Boundary(self._aftermath) if self._aftermath is not None else None


@dataclass
class _Shot:
    cuts: _Cuts
    span: object = None


class _Reading:
    """What `EditorialReading` gives the shaper, for one moment."""

    def __init__(
        self,
        purpose: ShotPurpose,
        dead: float = 0.0,
        cuts: _Cuts | None = None,
        response: float = 0.0,
        resolution: float | None = None,
        aftermath: float | None = None,
    ):
        self._span = _Span(resolution, aftermath) if resolution is not None else None
        self._semantics = ShotSemantics(
            moment_id="mom-1",
            purpose=purpose,
            value=EditorialValue(context=1.0 - dead),
            response_seconds=response,
        )
        self._shot = _Shot(cuts=cuts or _Cuts(into=(), out_of=()), span=self._span)

    def semantics_of(self, moment):
        return self._semantics

    def shot(self, moment):
        return self._shot


def _moment(start: float = 100.0, end: float = 160.0) -> Moment:
    return Moment(
        media_id="media-1",
        moment_type=MomentType.EPIC,
        start_seconds=start + 10,
        end_seconds=end - 10,
        events=(),
        context_start=start,
        context_end=end,
        metadata={"id": "mom-1"},
    )


class TestNeutralIsTheSameObject:
    """The house edit's guarantee, checked with `is` and not with `==`."""

    def test_a_project_with_no_brief_and_no_style_resolves_to_neutral(self) -> None:
        strategy = resolve(None, intent=None)
        assert strategy.is_neutral
        assert strategy.context is NEUTRAL.context
        assert strategy.cut is NEUTRAL.cut
        assert strategy.dead_time is NEUTRAL.dead_time

    def test_the_defaults_the_interaction_layer_ships_are_neutral(self) -> None:
        """`balanced` dead time and `medium` context are what a project gets
        when nobody says anything, so they must mean "as before"."""
        assert resolve(None, intent=_Brief()).is_neutral

    def test_applying_a_neutral_strategy_returns_the_caller_s_own_list(self) -> None:
        moments = [_moment()]
        assert apply(moments, NEUTRAL) is moments

    def test_a_style_with_only_a_selection_doctrine_shapes_nothing(self) -> None:
        """Selection is P11's seam and reaches the optimiser its own way. A
        style that asks for different weights must not also silently re-cut."""
        strategy = EditingStrategy(selection=SelectionPolicy(entertainment=1.4))
        moments = [_moment()]
        assert not strategy.is_neutral
        assert apply(moments, strategy)[0] is moments[0]


class TestTheDialsThatDidNothing:
    """`dead_time_policy` and `context_preservation`, wired for the first time."""

    def test_asking_for_less_context_shortens_the_shot(self) -> None:
        before = _moment()
        strategy = resolve(None, intent=_Brief(context_preservation="low"))
        after = apply([before], strategy, _Reading(ShotPurpose.ACTION))[0]

        assert not strategy.is_neutral
        assert after.context_start > before.context_start
        assert after.context_end < before.context_end

    def test_asking_for_no_context_shortens_it_further(self) -> None:
        low = apply(
            [_moment()],
            resolve(None, intent=_Brief(context_preservation="low")),
            _Reading(ShotPurpose.ACTION),
        )[0]
        none = apply(
            [_moment()],
            resolve(None, intent=_Brief(context_preservation="none")),
            _Reading(ShotPurpose.ACTION),
        )[0]
        assert none.context_duration < low.context_duration

    def test_asking_for_more_context_does_not_invent_any(self) -> None:
        """The moment stage decided how much context exists. This layer may
        decline some of it, never manufacture more."""
        before = _moment()
        after = apply(
            [before],
            resolve(None, intent=_Brief(context_preservation="high")),
            _Reading(ShotPurpose.ACTION),
        )[0]
        assert after is before

    def test_aggressive_dead_time_reaches_the_moment_the_optimiser_reads(self) -> None:
        """The number `optimizer.py` has multiplied by zero since migration 1."""
        before = _moment()
        assert before.dead_time_score == 0.0

        strategy = resolve(None, intent=_Brief(dead_time_policy="aggressive"))
        after = apply([before], strategy, _Reading(ShotPurpose.DEAD, dead=0.8))[0]
        assert after.dead_time_score > 0.0

    def test_keeping_dead_time_leaves_the_moment_exactly_as_it_was(self) -> None:
        before = _moment()
        strategy = resolve(None, intent=_Brief(dead_time_policy="keep"))
        assert strategy.is_neutral
        assert apply([before], strategy, _Reading(ShotPurpose.DEAD)) == [before]


class TestWhatTheContextPolicyRefusesToTouch:
    def test_a_shot_that_builds_keeps_its_run_up(self) -> None:
        """Trimming the front of an anticipatory shot turns it into an abrupt
        one -- the run-up *is* what that shot is."""
        before = _moment()
        strategy = resolve(None, intent=_Brief(context_preservation="none"))
        after = apply([before], strategy, _Reading(ShotPurpose.ANTICIPATION))[0]
        assert after.context_start == before.context_start
        assert after.context_end < before.context_end

    def test_a_shot_somebody_reacts_to_keeps_its_tail(self) -> None:
        before = _moment()
        strategy = resolve(None, intent=_Brief(context_preservation="none"))
        after = apply([before], strategy, _Reading(ShotPurpose.REACTION))[0]
        assert after.context_end == before.context_end
        assert after.context_start > before.context_start

    def test_no_policy_may_trim_more_than_the_bound(self) -> None:
        policy = ContextPolicy(trim_lead_in=0.9, trim_tail=0.9)
        start, end = policy.bounds_for(None, 0.0, 100.0)
        assert start <= MAX_TRIM_FRACTION * 100.0
        assert end >= 100.0 - MAX_TRIM_FRACTION * 100.0


class TestWhereACutLands:
    def test_snapping_moves_the_cut_onto_a_seam_the_footage_already_has(self) -> None:
        before = _moment()
        strategy = EditingStrategy(cut=CutPolicy(snap_to_seams=True))
        after = apply([before], strategy, _Reading(ShotPurpose.ACTION, cuts=_Cuts()))[0]
        assert after.context_start == 98.0
        assert after.context_end == 161.0

    def test_a_seam_further_than_the_drift_is_not_worth_the_move(self) -> None:
        """Beyond this the policy stops improving a cut and starts choosing
        different footage, which is the optimiser's job."""
        distant = _Cuts(into=(100.0 - MAX_SEAM_DRIFT - 5.0,), out_of=(200.0,))
        before = _moment()
        strategy = EditingStrategy(cut=CutPolicy(snap_to_seams=True))
        after = apply([before], strategy, _Reading(ShotPurpose.ACTION, cuts=distant))
        assert after[0] is before

    def test_a_shot_with_no_seams_is_left_where_it_was(self) -> None:
        before = _moment()
        strategy = EditingStrategy(cut=CutPolicy(snap_to_seams=True))
        assert apply([before], strategy, _Reading(ShotPurpose.ACTION))[0] is before


class TestDeadTimeReachesTheOptimiserOnlyWhenAsked:
    def test_it_is_off_by_default_because_the_house_edit_is_frozen(self) -> None:
        assert DeadTimePolicy().is_neutral
        assert DeadTimePolicy().score_for(
            ShotSemantics(moment_id="m", value=EditorialValue())
        ) == 0.0

    def test_a_blind_reading_is_never_treated_as_dead(self) -> None:
        """Nobody looked is not the same fact as nothing was there, and
        penalising the first punishes a shot for the analysis stage being quiet.
        """
        blind = ShotSemantics(
            moment_id="m",
            value=EditorialValue(unobserved=("stores", "vision", "events", "speech")),
        )
        assert blind.value.is_blind
        assert DeadTimePolicy(enabled=True).score_for(blind) == 0.0

    def test_the_weight_scales_what_the_optimiser_sees(self) -> None:
        dead = ShotSemantics(moment_id="m", value=EditorialValue(context=0.2))
        light = DeadTimePolicy(enabled=True, weight=0.5).score_for(dead)
        heavy = DeadTimePolicy(enabled=True, weight=1.0).score_for(dead)
        assert 0.0 < light < heavy <= 1.0


class TestTheBriefWinsOverTheStyle:
    @dataclass
    class _Doctrine:
        trim_lead_in: float = 0.3
        trim_tail: float = 0.3
        snap_to_seams: bool = True
        max_drift: float = 1.0
        dead_time_weight: float = 1.0

    def test_a_style_may_ask_for_what_the_brief_did_not(self) -> None:
        strategy = resolve(None, intent=_Brief(), style_context=self._Doctrine())
        assert not strategy.is_neutral
        assert strategy.cut.snap_to_seams
        assert strategy.dead_time.enabled

    def test_an_explicit_instruction_is_not_overridden_by_a_style(self) -> None:
        """The behaviour that made `chronological` a setting the owner had to
        re-defeat on every new project."""
        strategy = resolve(
            None,
            intent=_Brief(context_preservation="low"),
            style_context=self._Doctrine(trim_lead_in=0.0, trim_tail=0.0),
        )
        assert strategy.context.trim_lead_in == 0.15

    def test_a_style_cannot_exceed_the_drift_bound(self) -> None:
        strategy = resolve(
            None, intent=_Brief(), style_context=self._Doctrine(max_drift=99.0)
        )
        assert strategy.cut.max_drift == MAX_SEAM_DRIFT


class TestHoldingAShotForItsResponse:
    """The one policy here that lengthens a shot rather than trimming it.

    Measured before it existed: of 22 selected shots somebody responded to,
    **18 stopped before the response finished**, by a median of two seconds,
    with a median of 1,357 unused seconds of recording sitting after the clip.
    """

    def test_it_is_off_by_default(self) -> None:
        assert ReactionPolicy().is_neutral
        assert apply([_moment()], NEUTRAL) is not None

    def test_a_shot_is_held_until_the_response_finishes(self) -> None:
        before = _moment()
        strategy = EditingStrategy(reaction=ReactionPolicy(hold_for_response=True))
        after = apply(
            [before],
            strategy,
            _Reading(ShotPurpose.REACTION, response=2.0),
            {"media-1": 10_000.0},
        )[0]
        assert after.context_end == before.context_end + 2.0

    def test_it_never_holds_longer_than_its_bound(self) -> None:
        """Beyond the bound the "response" is a coarse transcript segment
        rather than a sentence -- they run to 392 seconds on this machine."""
        policy = ReactionPolicy(hold_for_response=True, max_extra=3.0)
        after = apply(
            [_moment()],
            EditingStrategy(reaction=policy),
            _Reading(ShotPurpose.REACTION, response=60.0),
            {"media-1": 10_000.0},
        )[0]
        assert after.context_end == _moment().context_end + 3.0

    def test_it_never_reads_past_the_end_of_the_recording(self) -> None:
        before = _moment()
        after = apply(
            [before],
            EditingStrategy(reaction=ReactionPolicy(hold_for_response=True)),
            _Reading(ShotPurpose.REACTION, response=3.0),
            {"media-1": before.context_end + 1.0},
        )[0]
        assert after.context_end == before.context_end + 1.0

    def test_a_shot_nobody_responded_to_is_left_alone(self) -> None:
        before = _moment()
        after = apply(
            [before],
            EditingStrategy(reaction=ReactionPolicy(hold_for_response=True)),
            _Reading(ShotPurpose.ACTION, response=2.0),
            {"media-1": 10_000.0},
        )
        assert after[0] is before

    def test_a_reaction_with_no_measured_response_is_left_alone(self) -> None:
        """The purpose can be set by the speech lane while the transcript has
        no edge to hold to. No number, no hold."""
        before = _moment()
        after = apply(
            [before],
            EditingStrategy(reaction=ReactionPolicy(hold_for_response=True)),
            _Reading(ShotPurpose.REACTION, response=0.0),
            {"media-1": 10_000.0},
        )
        assert after[0] is before


class TestEndingAShotOnceItIsDecided:
    """The one place the editorial event span reaches a cut.

    It is `ContextPolicy`'s and not `CutPolicy`'s, and the reason was measured
    rather than argued. Wiring it into `CutPolicy` produced no change at all,
    because moving an out-point to just after a resolution is a 54.7-second
    move against a `max_drift` of 1.5 — and that guard is right. Snapping to a
    nearby seam and ending a shot on its point are different acts; the second
    is a tail trim, fenced here by `MAX_TRIM_FRACTION`, inside which the same
    54.7 seconds is 30 % of its shot.
    """

    @staticmethod
    def _asked():
        return EditingStrategy(context=ContextPolicy(trim_at_resolution=True))

    def test_it_is_off_by_default(self) -> None:
        assert ContextPolicy().is_neutral
        assert not ContextPolicy().trim_at_resolution

    def test_a_shot_ends_shortly_after_its_resolution(self) -> None:
        """145.0 rather than something earlier, because the fence below is
        real: a 60-second shot may not lose more than 21 of them."""
        before = _moment(start=100.0, end=160.0)
        after = apply(
            [before],
            self._asked(),
            _Reading(ShotPurpose.ACTION, resolution=145.0),
            {"media-1": 10_000.0},
        )[0]

        assert after.context_end == 145.0
        assert after.context_start == before.context_start, "the front is untouched"

    def test_the_aftermath_wins_over_the_resolution(self) -> None:
        """A reaction the reading located is a fact about this shot, and
        ending on the resolution would cut it off."""
        after = apply(
            [_moment(start=100.0, end=160.0)],
            self._asked(),
            _Reading(ShotPurpose.ACTION, resolution=142.0, aftermath=150.0),
            {"media-1": 10_000.0},
        )[0]

        assert after.context_end == 150.0

    def test_it_never_trims_more_than_the_fence(self) -> None:
        before = _moment(start=100.0, end=160.0)
        after = apply(
            [before],
            self._asked(),
            _Reading(ShotPurpose.ACTION, resolution=105.0),
            {"media-1": 10_000.0},
        )[0]

        assert after.context_end >= 160.0 - 60.0 * MAX_TRIM_FRACTION

    def test_a_shot_somebody_responded_to_keeps_its_tail(self) -> None:
        before = _moment(start=100.0, end=160.0)
        after = apply(
            [before],
            self._asked(),
            _Reading(ShotPurpose.REACTION, resolution=145.0),
            {"media-1": 10_000.0},
        )

        assert after[0] is before

    def test_a_shot_with_no_located_resolution_is_left_alone(self) -> None:
        before = _moment(start=100.0, end=160.0)
        after = apply(
            [before], self._asked(), _Reading(ShotPurpose.ACTION), {"media-1": 10_000.0}
        )

        assert after[0] is before

    def test_a_resolution_outside_the_shot_is_ignored(self) -> None:
        before = _moment(start=100.0, end=160.0)
        after = apply(
            [before],
            self._asked(),
            _Reading(ShotPurpose.ACTION, resolution=400.0),
            {"media-1": 10_000.0},
        )

        assert after[0] is before


class TestP03StyleCannotWiden:
    def test_p0_3_a_style_cannot_widen_an_authorized_span(self) -> None:
        # The reaction hold is the one policy that lengthens a shot. With a
        # chain on the moment it stops at the grant, and says so in the
        # moment's own ledger; nothing widens, and nothing is silent.
        from backend.moments import grants

        (before,) = grants.grant_first_spans([_moment()])
        governing = grants.spans_of(before)[-1]
        after = apply(
            [before],
            EditingStrategy(reaction=ReactionPolicy(hold_for_response=True)),
            _Reading(ShotPurpose.REACTION, response=2.0),
            {"media-1": 10_000.0},
        )[0]
        assert after.context_end == governing.end, "held to the grant, not past it"
        assert any("refused -- a style may not widen" in line for line in after.explanation)
        # And the plan-level check agrees: no unmarked widening to report.
        assert grants.grant_widenings([after], {})[0].context_end == governing.end

    def test_p0_3_a_bare_moment_is_still_held_the_old_way(self) -> None:
        # The engine's own fixtures carry no chain; the pipeline refuses those
        # before the engine, and here the hold behaves as it always has.
        before = _moment()
        after = apply(
            [before],
            EditingStrategy(reaction=ReactionPolicy(hold_for_response=True)),
            _Reading(ShotPurpose.REACTION, response=2.0),
            {"media-1": 10_000.0},
        )[0]
        assert after.context_end == before.context_end + 2.0
