"""Reading a moment as a shot, and episodes as a situation (V2-P11).

Both layers are readings of stores that already existed, so the interesting
failures are not arithmetic. They are:

* answering when nothing was recorded, instead of reporting calm footage;
* naming a phase or an arc that the lanes do not support;
* and, for situations, quietly doing the merge the episode reader measured and
  refused -- grouping by proximity rather than by a relation something already
  found.

Each of those has a test here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest

from backend.core.models.enums import GameEventType
from backend.editorial.evidence import CONTEXT_SECONDS, EditorialEvidence, read
from backend.editorial.situations import Situation, situation_of
from backend.editorial.situations import read as read_situations
from backend.evidence import Stores

pytestmark = pytest.mark.unit


class _Lanes:
    """A session that climbs into the moment, lets go after it, then talks.

    Written as three flat stretches rather than a curve. A stub whose values
    need arithmetic to predict is a stub that tests the arithmetic instead of
    the reading -- the first version of this file asserted things about a ramp
    I had to work out by hand, and got two of them wrong.
    """

    #: before 32-40 · during 40-58 · after 58-66
    LEVELS: ClassVar[dict[str, tuple[float, float, float]]] = {
        "intensity": (0.20, 0.80, 0.30),
        "tension": (0.20, 0.80, 0.15),
        "motion": (0.30, 0.70, 0.30),
        "audio": (0.30, 0.60, 0.40),
        "speech": (0.00, 0.00, 0.90),
    }

    def window(self, lane: str, start: float, end: float):
        if end <= start:
            return []
        low, mid, high = self.LEVELS.get(lane, (0.0, 0.0, 0.0))
        values = []
        at = start
        while at < end:
            values.append(low if at < 40.0 else mid if at < 58.0 else high)
            at += 0.5
        return values


@dataclass
class _Moment:
    media_id: str = "media-1"
    id: str = "mom-1"
    start_seconds: float = 40.0
    end_seconds: float = 58.0
    context_start: float = 36.0
    context_end: float = 62.0


@dataclass
class _Said:
    start: float
    end: float
    text: str = "that was close"


@dataclass
class _Cut:
    start_seconds: float


@dataclass
class _Event:
    event_type: GameEventType
    start_seconds: float
    end_seconds: float
    is_named: bool = True


@dataclass
class _Episode:
    event_type: GameEventType
    media_id: str
    start_seconds: float
    end_seconds: float
    events: tuple = ()


@dataclass
class _Link:
    earlier: _Episode
    later: _Episode
    gap_seconds: float = 4.0


@dataclass
class _Reading:
    episodes: tuple
    links: tuple = ()


class TestReadingAMomentAsAShot:
    def test_the_state_before_during_and_after_are_read_separately(self) -> None:
        evidence = read(_Moment(), stores=Stores(), reader=_Lanes())

        assert evidence.before.intensity < evidence.during.intensity
        assert evidence.after.intensity < evidence.during.intensity

    def test_intensity_and_tension_are_different_questions(self) -> None:
        """The distinction that makes `resolves` worth having.

        This session comes *out* of the moment busier than it went in -- 0.30
        against 0.20 -- so it is rising. And the tension it was carrying let
        go, 0.80 down to 0.15, so it also resolves. An editor needs both: one
        says the session did not go quiet, the other says the thing finished.
        """
        evidence = read(_Moment(), stores=Stores(), reader=_Lanes())

        assert evidence.rising is True
        assert evidence.resolves is True

    def test_a_moment_whose_tension_lets_go_is_read_as_resolving(self) -> None:
        evidence = read(_Moment(), stores=Stores(), reader=_Lanes())

        assert evidence.resolves

    def test_speech_starting_after_the_moment_reads_as_a_reaction(self) -> None:
        evidence = read(_Moment(), stores=Stores(), reader=_Lanes())

        assert evidence.reaction_follows

    def test_without_lanes_the_states_are_empty_and_say_so(self) -> None:
        # The distinction the whole layer turns on: an unread lane must not
        # read as a calm one.
        evidence = read(_Moment(), stores=Stores(), reader=None)

        assert evidence.during.intensity == 0.0
        assert "during" in evidence.unknown

    def test_a_stretch_nobody_looked_at_is_marked_unobserved(self) -> None:
        evidence = read(_Moment(), stores=Stores(), reader=_Lanes())

        assert evidence.observed is False

    def test_a_stretch_something_recorded_is_observed(self) -> None:
        stores = Stores(said={"media-1": [_Said(41.0, 44.0)]})

        assert read(_Moment(), stores=stores, reader=_Lanes()).observed

    def test_an_unnamed_event_is_not_reported_as_a_finding(self) -> None:
        stores = Stores(
            events={
                "media-1": [
                    _Event(GameEventType.UNKNOWN_EVENT, 42.0, 43.0, is_named=False),
                    _Event(GameEventType.COMBAT, 44.0, 46.0),
                ]
            }
        )

        assert read(_Moment(), stores=stores, reader=_Lanes()).events == ("combat",)

    def test_the_context_read_is_wider_than_the_moment(self) -> None:
        # A label eight seconds before the moment is part of what the shot is
        # about; one recorded a minute away is not.
        stores = Stores(cuts={"media-1": [_Cut(39.0), _Cut(200.0)]})

        evidence = read(_Moment(), stores=stores, reader=_Lanes())

        assert 39.0 in evidence.cuts.into
        assert 200.0 not in evidence.cuts.into
        assert CONTEXT_SECONDS == 8.0


class TestWhereAShotMayBeCut:
    def test_a_seam_the_footage_already_has_is_offered(self) -> None:
        stores = Stores(cuts={"media-1": [_Cut(38.5), _Cut(59.0)]})

        evidence = read(_Moment(), stores=stores, reader=_Lanes())

        assert evidence.cuts.best_in(40.0) == pytest.approx(38.5)
        assert evidence.cuts.best_out(58.0) == pytest.approx(59.0)

    def test_a_seam_inside_speech_is_not_offered(self) -> None:
        # V2-P3's rule, as data: zero cuts inside a spoken word.
        stores = Stores(
            cuts={"media-1": [_Cut(59.0)]},
            said={"media-1": [_Said(58.0, 61.0)]},
        )

        evidence = read(_Moment(), stores=stores, reader=_Lanes())

        assert evidence.cuts.safe(59.0) is False
        assert evidence.cuts.best_out(58.0) == pytest.approx(58.0), "fell back"

    def test_with_no_seams_the_moment_keeps_its_own_edges(self) -> None:
        evidence = read(_Moment(), stores=Stores(), reader=_Lanes())

        assert evidence.cuts.best_in(40.0) == pytest.approx(40.0)


class TestReadingEpisodesAsASituation:
    def _episodes(self):
        first = _Episode(GameEventType.COMBAT, "media-1", 40.0, 48.0)
        second = _Episode(GameEventType.LOW_HEALTH, "media-1", 50.0, 54.0)
        third = _Episode(GameEventType.VICTORY, "media-1", 56.0, 60.0)
        return first, second, third

    def test_related_episodes_become_one_situation(self) -> None:
        first, second, third = self._episodes()
        reading = _Reading(
            episodes=(first, second, third),
            links=(_Link(first, second), _Link(second, third)),
        )

        situations = read_situations(reading, media_id="media-1", reader=_Lanes())

        assert len(situations) == 1
        assert situations[0].parts == 3
        assert situations[0].is_compound

    def test_unrelated_episodes_stay_separate(self) -> None:
        """The merge the episode reader measured and refused.

        Grouping by proximity would put these together -- they are twelve
        seconds apart, inside the window that reader uses for same-type runs.
        Nothing said they are related, so nothing here says it either.
        """
        first, second, _third = self._episodes()
        reading = _Reading(episodes=(first, second), links=())

        situations = read_situations(reading, media_id="media-1", reader=_Lanes())

        assert len(situations) == 2

    def test_the_episodes_survive_inside_the_situation(self) -> None:
        first, second, third = self._episodes()
        reading = _Reading(
            episodes=(first, second, third), links=(_Link(first, second), _Link(second, third))
        )

        situation = read_situations(reading, media_id="media-1", reader=_Lanes())[0]

        assert situation.types == ("combat", "low_health", "victory")
        assert situation.episodes == (first, second, third)

    def test_the_arc_is_read_from_the_lanes_not_the_event_names(self) -> None:
        # A `victory` is not automatically a payoff. What makes an episode the
        # payoff is that the tension it carried let go afterwards.
        first, second, third = self._episodes()
        reading = _Reading(
            episodes=(first, second, third), links=(_Link(first, second), _Link(second, third))
        )

        situation = read_situations(reading, media_id="media-1", reader=_Lanes())[0]

        assert "cause" in situation.arc
        assert situation.arc["cause"] is first

    def test_without_lanes_there_is_no_arc_rather_than_a_guessed_one(self) -> None:
        first, second, _ = self._episodes()
        reading = _Reading(episodes=(first, second), links=(_Link(first, second),))

        situation = read_situations(reading, media_id="media-1", reader=None)[0]

        assert situation.arc == {}
        assert situation.is_compound

    def test_a_situation_says_why_it_is_one(self) -> None:
        first, second, _ = self._episodes()
        reading = _Reading(episodes=(first, second), links=(_Link(first, second),))

        situation = read_situations(reading, media_id="media-1", reader=_Lanes())[0]

        assert "related episodes" in situation.because
        assert "combat" in situation.because

    def test_an_empty_reading_produces_nothing(self) -> None:
        assert read_situations(_Reading(episodes=()), media_id="media-1") == ()


class TestFindingTheSituationAMomentBelongsTo:
    def _situation(self) -> Situation:
        return Situation(
            id="sit-1", media_id="media-1", start_seconds=40.0, end_seconds=60.0
        )

    def test_an_overlapping_moment_is_matched(self) -> None:
        assert situation_of([self._situation()], _Moment()) is not None

    def test_a_moment_from_another_recording_is_not(self) -> None:
        # Two recordings of one session both have a second 40.
        elsewhere = _Moment(media_id="media-2")

        assert situation_of([self._situation()], elsewhere) is None

    def test_a_moment_outside_every_situation_is_not_forced_into_one(self) -> None:
        far = _Moment(start_seconds=400.0, end_seconds=420.0)

        assert situation_of([self._situation()], far) is None


class TestEveryFieldHasAReader:
    def test_the_summary_names_what_the_reading_found(self) -> None:
        # A field nobody reads is the orphaned configuration key of a domain
        # model, and this branch has spent a week finding those.
        stored = read(_Moment(), stores=Stores(), reader=_Lanes()).as_dict()

        for key in ("before", "during", "after", "rising", "resolves", "cut_in", "unknown"):
            assert key in stored

    def test_an_evidence_with_nothing_in_it_still_answers(self) -> None:
        empty = EditorialEvidence(media_id="m", moment_id="x")

        assert empty.duration == 0.0
        assert empty.rising is False


class TestTheShotsOwnArcReachesItsLength:
    """V2-P11's addition to V2-P3's rule trace.

    Each rule defaults to what the engine assumed before it existed, so a shot
    with no editorial reading measures exactly as it always did. That is the
    same promise the selection seam makes, kept in the other engine.
    """

    def _context(self, config, **fields):
        from backend.editorial.pacing_engine import PacingContext

        return PacingContext(position=100.0, level="normal", **fields)

    def test_a_shot_with_no_reading_measures_as_it_always_did(self, config) -> None:
        from backend.editorial.pacing_engine import shot_length

        plain = shot_length(self._context(config), config)
        read = shot_length(
            self._context(config, part="", resolves=False, reaction_follows=False),
            config,
        )

        assert read.seconds == pytest.approx(plain.seconds)
        assert read.rules == plain.rules

    def test_an_unresolved_payoff_is_held_longer(self, config) -> None:
        """Longer, but never past the band.

        Sustained tension has to have tightened the shot first, because the
        rule may only give back what another rule took: V2-P3 lets exactly one
        thing exceed a band -- an unfinished sentence -- and an unfinished
        payoff is not that. A shot already at its cap stays at its cap.
        """
        from backend.editorial.pacing_engine import shot_length

        finished = shot_length(
            self._context(config, tension=0.9, part="payoff", resolves=True), config
        )
        still_going = shot_length(
            self._context(config, tension=0.9, part="payoff", resolves=False), config
        )

        assert still_going.seconds > finished.seconds
        assert any("not resolved" in rule for rule in still_going.rules)

    def test_a_shot_at_its_cap_is_not_pushed_past_the_band(self, config) -> None:
        from backend.editorial.pacing_engine import shot_length

        # Nothing tightened this one, so there is nothing to give back.
        at_cap = shot_length(self._context(config, part="payoff", resolves=False), config)
        plain = shot_length(self._context(config), config)

        assert at_cap.seconds == pytest.approx(plain.seconds)

    def test_a_shot_lands_on_a_seam_the_footage_already_has(self, config) -> None:
        from backend.editorial.pacing_engine import shot_length

        # A cut on a seam is invisible; a cut on a round number is a cut.
        decided = shot_length(self._context(config, seam_at=104.2), config)

        assert decided.seconds == pytest.approx(4.2)
        assert any("seam" in rule for rule in decided.rules)

    def test_a_seam_too_far_away_is_not_taken(self, config) -> None:
        from backend.editorial.pacing_engine import shot_length

        plain = shot_length(self._context(config), config)
        far = shot_length(self._context(config, seam_at=400.0), config)

        assert far.seconds == pytest.approx(plain.seconds)

    def test_every_length_still_carries_its_reasons(self, config) -> None:
        from backend.editorial.pacing_engine import shot_length

        decided = shot_length(
            self._context(config, part="payoff", resolves=False, reaction_follows=True),
            config,
        )

        assert len(decided.rules) >= 2, "a length with no reason cannot be reviewed"
