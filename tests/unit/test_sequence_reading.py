"""How the shots sit together, and where the video starts and stops (V2-P1).

Two modules, one file, because they answer halves of the same question: the
first reads the joins between shots and the second reads the two joins that
have only one shot on them.

The thing most worth protecting here is the **redefinition of pacing**. The
judge used to score shot spread against an ideal of 1.2 — a constant no edit
this system makes has ever approached, the house style's own spread being
1.888 — so every edit was marked down for being what every edit is, and the
three styles that trim shots were marked down further for succeeding. The fix
was not a better constant. It was noticing that unevenness is not the defect:
*unevenness that tracks nothing* is. `TestPurposefulVersusArbitrary` is that
distinction, and it is the test that would have to fail before anyone reached
for a constant again.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.core.models.enums import MomentType
from backend.editorial.bookends import (
    MAX_TRIM_SHARE,
    WORTH_MOVING,
    BookendPolicy,
    apply_to_plan,
)
from backend.editorial.bookends import read as read_bookends
from backend.editorial.semantics import EditorialValue, ShotPurpose, ShotSemantics
from backend.editorial.sequence import FELT_LENGTH_CHANGE, read
from backend.moments.formation import Moment

pytestmark = pytest.mark.unit


def _shot(
    start: float,
    length: float,
    kind: MomentType = MomentType.EPIC,
    score: float = 0.5,
    media: str = "media-1",
    ident: str = "",
) -> Moment:
    return Moment(
        media_id=media,
        moment_type=kind,
        start_seconds=start + 1,
        end_seconds=start + length - 1,
        events=(),
        context_start=start,
        context_end=start + length,
        score=score,
        metadata={"id": ident or f"mom-{start:.0f}"},
    )


@dataclass
class _Cuts:
    out_of: tuple = ()

    def best_out(self, default: float) -> float:
        return default


@dataclass
class _Shot:
    cuts: _Cuts
    situation_id: str = ""


class _Reading:
    """A reading that answers for named moments and shrugs for the rest."""

    def __init__(self, purposes: dict | None = None, seams: dict | None = None,
                 situations: dict | None = None):
        self._purposes = purposes or {}
        self._seams = seams or {}
        self._situations = situations or {}

    def semantics_of(self, moment):
        purpose = self._purposes.get(moment.id)
        if purpose is None:
            return None
        return ShotSemantics(moment_id=moment.id, purpose=purpose, value=EditorialValue())

    def shot(self, moment):
        return _Shot(
            cuts=_Cuts(out_of=tuple(self._seams.get(moment.id, ()))),
            situation_id=self._situations.get(moment.id, ""),
        )


class TestThereIsNothingToReadInOneShot:
    def test_a_single_shot_edit_has_no_seams(self) -> None:
        reading = read([_shot(0, 30)])
        assert reading.is_empty
        assert reading.rhythm == 0.0

    def test_empty_is_not_the_same_as_perfect(self) -> None:
        """A caller that averages the zeros in concludes the wrong thing, so
        the type has to make the distinction available."""
        assert read([]).is_empty
        assert not read([_shot(0, 30), _shot(40, 60)]).is_empty


class TestPurposefulVersusArbitrary:
    """The distinction the pacing axis could not make."""

    def test_a_length_change_between_different_kinds_is_purposeful(self) -> None:
        reading = read(
            [_shot(0, 60, MomentType.EPIC), _shot(70, 20, MomentType.REACTION)]
        )
        assert reading.seams[0].length_changed
        assert reading.seams[0].purposeful
        assert not reading.seams[0].arbitrary
        assert reading.purposeful_rhythm == 1.0

    def test_a_length_change_between_the_same_kind_is_arbitrary(self) -> None:
        reading = read(
            [_shot(0, 60, MomentType.EPIC), _shot(70, 20, MomentType.EPIC)]
        )
        assert reading.seams[0].arbitrary
        assert not reading.seams[0].purposeful
        assert reading.purposeful_rhythm == 0.0

    def test_purpose_separates_two_shots_of_one_type(self) -> None:
        """Type is not the only way two shots differ. A payoff followed by a
        reaction is a change of kind even when both are logged as `epic`."""
        before, after = _shot(0, 60, ident="a"), _shot(70, 20, ident="b")
        flat = read([before, after])
        assert flat.seams[0].arbitrary

        told = read(
            [before, after],
            _Reading({"a": ShotPurpose.PAYOFF, "b": ShotPurpose.REACTION}),
        )
        assert told.seams[0].purposeful

    def test_a_steady_edit_has_no_arbitrary_cuts_to_be_marked_down_for(self) -> None:
        """A deliberately even edit is not a defect, and the old axis called
        it one -- 'none is a metronome' was the comment above the constant."""
        steady = read([_shot(index * 70, 60, MomentType.EPIC) for index in range(5)])
        assert not any(seam.length_changed for seam in steady.seams)
        assert not any(seam.arbitrary for seam in steady.seams)

    def test_a_change_below_what_is_felt_is_not_a_change(self) -> None:
        barely = 60.0 * (1.0 + FELT_LENGTH_CHANGE / 2.0)
        assert not read([_shot(0, 60), _shot(70, barely)]).seams[0].length_changed


class TestTheFiveRelations:
    def test_contrast_counts_joins_between_different_kinds(self) -> None:
        mixed = read(
            [
                _shot(0, 30, MomentType.EPIC),
                _shot(40, 30, MomentType.FUNNY),
                _shot(80, 30, MomentType.EPIC),
            ]
        )
        assert mixed.contrast == 1.0
        assert mixed.repetition == 0.0

    def test_repetition_counts_joins_between_the_same_kind(self) -> None:
        same = read([_shot(index * 40, 30, MomentType.EPIC) for index in range(4)])
        assert same.repetition == 1.0
        assert same.longest_same_type_run == 4

    def test_continuity_is_broken_by_a_change_of_recording(self) -> None:
        across = read([_shot(0, 30, media="a"), _shot(40, 30, media="b")])
        assert across.continuity == 0.0
        assert across.seams[0].gap_seconds == float("inf")

    def test_continuity_is_broken_by_a_long_jump_within_one_recording(self) -> None:
        far = read([_shot(0, 30), _shot(3000, 30)])
        assert far.continuity == 0.0

    def test_transition_quality_is_not_claimed_without_a_reading(self) -> None:
        """Zero would be indistinguishable from badly cut. The `why` says
        which, because the number cannot."""
        blind = read([_shot(0, 30), _shot(40, 30)])
        assert blind.transition_quality == 0.0
        assert any("not measured" in line for line in blind.why)

    def test_transition_quality_counts_cuts_landing_on_a_seam(self) -> None:
        told = read(
            [_shot(0, 30, ident="a"), _shot(40, 30, ident="b")],
            _Reading(seams={"a": (30.0,)}),
        )
        assert told.transition_quality == 1.0


class TestWhereTheVideoStarts:
    """The one opening decision a chronological edit can still make."""

    @staticmethod
    def _slow_start():
        return [
            _shot(0, 40, MomentType.TENSION, score=0.2, ident="weak"),
            _shot(50, 40, MomentType.EPIC, score=0.9, ident="strong"),
            _shot(100, 40, MomentType.SKILL, score=0.5, ident="c"),
            _shot(150, 40, MomentType.SKILL, score=0.5, ident="d"),
            _shot(200, 40, MomentType.SKILL, score=0.5, ident="e"),
        ]

    def test_neutral_starts_where_the_edit_starts(self) -> None:
        shots = self._slow_start()
        decided = read_bookends(shots, BookendPolicy())
        assert (decided.start, decided.stop) == (0, len(shots))
        assert not decided.moved

    def test_it_opens_on_the_stronger_shot(self) -> None:
        decided = read_bookends(
            self._slow_start(), BookendPolicy(trim_weak_opening=True)
        )
        assert decided.start == 1
        assert "weak lead-in dropped" in decided.opening_reason

    def test_a_gain_too_small_to_see_is_not_worth_real_footage(self) -> None:
        shots = [
            _shot(0, 40, MomentType.EPIC, score=0.50),
            _shot(50, 40, MomentType.EPIC, score=0.50 + WORTH_MOVING / 4),
            _shot(100, 40, MomentType.EPIC, score=0.5),
        ]
        assert not read_bookends(shots, BookendPolicy(trim_weak_opening=True)).moved

    def test_it_never_opens_on_an_outcome(self) -> None:
        """Skipping the setup to open on the victory is a flash-forward
        wearing a different name, and the constitution says no."""
        shots = [
            _shot(0, 40, MomentType.TENSION, score=0.2),
            _shot(50, 40, MomentType.VICTORY, score=0.95),
            _shot(100, 40, MomentType.SKILL, score=0.3),
        ]
        assert read_bookends(shots, BookendPolicy(trim_weak_opening=True)).start != 1

    def test_it_keeps_the_setup_belonging_to_the_shot_it_would_open_on(self) -> None:
        """The walk-up to the ambush is why the ambush lands -- but only when
        it is the *same* ambush. A setup from an unrelated scene is not a
        reason to keep a slow start."""
        shots = self._slow_start()
        related = _Reading(
            purposes={"weak": ShotPurpose.SETUP},
            situations={"weak": "sit-1", "strong": "sit-1"},
        )
        unrelated = _Reading(
            purposes={"weak": ShotPurpose.SETUP},
            situations={"weak": "sit-1", "strong": "sit-2"},
        )
        policy = BookendPolicy(trim_weak_opening=True)
        assert read_bookends(shots, policy, related).start == 0
        assert read_bookends(shots, policy, unrelated).start == 1

    def test_it_cannot_trim_more_than_its_share(self) -> None:
        shots = [_shot(index * 50, 40, MomentType.SKILL, score=0.1) for index in range(10)]
        shots.append(_shot(600, 40, MomentType.EPIC, score=1.0))
        decided = read_bookends(shots, BookendPolicy(trim_weak_opening=True))
        assert decided.start <= int(len(shots) * MAX_TRIM_SHARE)


class TestWhereTheVideoStops:
    @staticmethod
    def _trails_off():
        return [
            _shot(0, 40, MomentType.SKILL, score=0.5, ident="a"),
            _shot(50, 40, MomentType.SKILL, score=0.5, ident="b"),
            _shot(100, 40, MomentType.SKILL, score=0.5, ident="c"),
            _shot(150, 40, MomentType.EPIC, score=0.9, ident="strong"),
            _shot(200, 40, MomentType.TENSION, score=0.1, ident="trailing"),
        ]

    def test_it_stops_on_strength_rather_than_trailing_off(self) -> None:
        decided = read_bookends(
            self._trails_off(), BookendPolicy(trim_weak_ending=True)
        )
        assert decided.stop == 4
        assert "trailing-off dropped" in decided.ending_reason

    def test_a_reaction_is_never_trimmed_for_being_quiet(self) -> None:
        """A reaction is how an edit stops feeling like it ran out of footage."""
        decided = read_bookends(
            self._trails_off(),
            BookendPolicy(trim_weak_ending=True),
            _Reading({"trailing": ShotPurpose.REACTION}),
        )
        assert decided.stop == 5


class TestSlicingThePlan:
    @dataclass
    class _Plan:
        moments: tuple
        beats: tuple = ()
        notes: tuple = ()

        @property
        def is_empty(self) -> bool:
            return not self.moments

    def test_an_unmoved_plan_is_the_caller_s_own_object(self) -> None:
        plan = self._Plan(moments=(_shot(0, 30), _shot(40, 30)))
        decided = read_bookends(plan.moments, BookendPolicy())
        assert apply_to_plan(plan, decided) is plan

    def test_beats_are_sliced_with_the_moments_they_index(self) -> None:
        """They are positional. A plan whose beats outlive their moments
        labels the wrong shots, silently."""
        shots = TestWhereTheVideoStarts._slow_start()
        plan = self._Plan(moments=tuple(shots), beats=tuple("abcde"))
        decided = read_bookends(shots, BookendPolicy(trim_weak_opening=True))
        trimmed = apply_to_plan(plan, decided)
        assert len(trimmed.beats) == len(trimmed.moments)
        assert trimmed.beats == ("b", "c", "d", "e")

    def test_it_says_the_optimiser_s_deviation_is_now_stale(self) -> None:
        shots = TestWhereTheVideoStarts._slow_start()
        plan = self._Plan(moments=tuple(shots))
        decided = read_bookends(shots, BookendPolicy(trim_weak_opening=True))
        trimmed = apply_to_plan(plan, decided)
        assert any("untrimmed selection" in note for note in trimmed.notes)
