"""P0.6, item 5: overlapping moments are one situation, and a situation keeps
its onsets (backend/narrative/situations.py, the optimizer's selection).

The benchmark shape these guard: a strong chaos moment, a weaker one
overlapping its tail and carrying the kill, and a surprise overlapping the
weaker one's tail. The selection dropped the weak one and the kill with it.
"""

from __future__ import annotations

import pytest

from backend.core.models.enums import GameEventType, MomentType
from backend.gaming.correlation import GameEvent
from backend.moments.formation import Moment
from backend.moments.grants import METADATA_KEY, WIDENED_KEY
from backend.narrative import situations
from backend.narrative.optimizer import optimise

pytestmark = pytest.mark.unit

MEDIA = "media-000000000001"


def _event(start: float, end: float, *, importance: float = 0.9, kind=GameEventType.KILL):
    return GameEvent(
        event_type=kind,
        start_seconds=start,
        end_seconds=end,
        confidence=0.9,
        importance=importance,
        sources=("audio", "vision"),
    )


def _moment(
    context: tuple[float, float],
    core: tuple[float, float],
    *,
    events=(),
    score: float = 0.5,
    kind: MomentType = MomentType.CHAOS,
    ident: str = "",
    chain=(),
) -> Moment:
    return Moment(
        media_id=MEDIA,
        moment_type=kind,
        start_seconds=core[0],
        end_seconds=core[1],
        events=tuple(events) or (_event(core[0], min(core[0] + 2.0, core[1])),),
        context_start=context[0],
        context_end=context[1],
        score=score,
        score_breakdown={"entertainment": score, "narrative": score},
        metadata={"id": ident, METADATA_KEY: list(chain)},
    )


def _benchmark_shape():
    strong = _moment((892.2, 983.7), (917.2, 983.7), score=0.44, ident="mom-strong",
                     events=(_event(970.0, 983.7),))
    kill = _event(999.5, 1013.6, importance=0.9)
    weak = _moment((975.5, 1033.6), (999.5, 1013.6), score=0.30, ident="mom-weak",
                   events=(kill,), chain=[{"media_id": MEDIA, "start": 975.5, "end": 1033.6,
                                          "granted_by": "context_expansion", "reason": "test"}])
    surprise = _moment((1008.9, 1033.9), (1033.9, 1033.9), score=0.39, ident="mom-surprise",
                       kind=MomentType.SURPRISE, events=(_event(1033.9, 1033.9, importance=0.4),))
    return strong, weak, surprise, kill


class TestP06ASituationKeepsItsOnsets:
    def test_p0_6_a_dropped_sibling_hands_its_onset_to_the_anchor(self) -> None:
        strong, weak, surprise, kill = _benchmark_shape()
        chosen, absorbed, seconds = situations.absorb_onsets(
            [strong, surprise], [weak], min_importance=0.5
        )
        anchor = chosen[0]
        assert absorbed and absorbed[0].moment_id == "mom-weak"
        assert absorbed[0].onsets == (999.5,)
        # The anchor covers the kill's own span, not the sibling's whole core,
        # and stops where the next chosen moment (the surprise, 1008.9 s) begins.
        assert anchor.context_end == pytest.approx(1008.9)
        assert anchor.context_start == pytest.approx(892.2)
        assert seconds == pytest.approx(1008.9 - 983.7, abs=0.01)
        assert kill in anchor.events
        assert any("kept the onset" in line for line in anchor.explanation)

    def test_p0_6_an_absorbed_onset_becomes_core_the_trim_cannot_take(self) -> None:
        strong, weak, surprise, _kill = _benchmark_shape()
        chosen, _, _ = situations.absorb_onsets([strong, surprise], [weak], min_importance=0.5)
        assert chosen[0].end_seconds == pytest.approx(1008.9)

    def test_p0_6_the_absorbed_span_is_a_marked_widening_carrying_the_siblings_chain(self) -> None:
        strong, weak, surprise, _kill = _benchmark_shape()
        chosen, _, _ = situations.absorb_onsets([strong, surprise], [weak], min_importance=0.5)
        anchor = chosen[0]
        marks = anchor.metadata[WIDENED_KEY]
        assert marks[-1]["granted_by"] == "refinement"
        assert marks[-1]["end"] == pytest.approx(1008.9)
        assert "+25.2 s" in marks[-1]["reason"]
        assert any(span["start"] == 975.5 for span in anchor.metadata[METADATA_KEY])
        assert anchor.metadata[situations.SITUATION_KEY][0]["moment_id"] == "mom-weak"

    def test_p0_6_an_onset_below_the_importance_line_is_not_absorbed(self) -> None:
        strong, weak, surprise, _kill = _benchmark_shape()
        chosen, absorbed, _ = situations.absorb_onsets(
            [strong, surprise], [weak], min_importance=0.95
        )
        assert not absorbed
        assert chosen[0].context_end == pytest.approx(983.7)

    def test_p0_6_an_onset_another_chosen_moment_contains_is_not_absorbed_twice(self) -> None:
        strong, weak, _surprise, kill = _benchmark_shape()
        # A chosen moment already holds the onset: nothing to absorb.
        holder = _moment((995.0, 1040.0), (999.5, 1013.6), ident="mom-holder", events=(kill,))
        chosen, absorbed, _ = situations.absorb_onsets([strong, holder], [weak], min_importance=0.5)
        assert not absorbed
        assert chosen[0].context_end == pytest.approx(983.7)

    def test_p0_6_with_no_neighbour_the_anchor_covers_the_whole_event(self) -> None:
        strong, weak, _surprise, _kill = _benchmark_shape()
        chosen, _absorbed, seconds = situations.absorb_onsets([strong], [weak], min_importance=0.5)
        assert chosen[0].context_end == pytest.approx(1013.6)
        assert seconds == pytest.approx(1013.6 - 983.7, abs=0.01)

    def test_p0_6_a_dropped_moment_overlapping_nothing_is_left_alone(self) -> None:
        strong, _weak, _surprise, _kill = _benchmark_shape()
        far = _moment((2000.0, 2050.0), (2020.0, 2030.0), ident="mom-far")
        chosen, absorbed, _ = situations.absorb_onsets([strong], [far], min_importance=0.5)
        assert not absorbed and chosen == [strong]


class TestP06TheSelectionShowsASituationOnce:
    @pytest.fixture
    def optimizer(self, config):
        return config.narrative.optimizer

    @pytest.fixture
    def policy(self, config):
        return config.duration_policy

    def test_p0_6_two_chosen_moments_never_overlap(self, optimizer, policy) -> None:
        strong, weak, surprise, _kill = _benchmark_shape()
        result = optimise(
            [strong, weak, surprise],
            target_seconds=140.0,
            config=optimizer,
            policy=policy,
        )
        chosen = list(result.moments)
        assert chosen
        for i, a in enumerate(chosen):
            for b in chosen[i + 1:]:
                assert not situations.overlaps(a, b), (a.id, b.id)

    def test_p0_6_the_selection_keeps_the_kill_the_dropped_sibling_carried(
        self, optimizer, policy
    ) -> None:
        strong, weak, surprise, _kill = _benchmark_shape()
        result = optimise(
            [strong, weak, surprise],
            target_seconds=140.0,
            config=optimizer,
            policy=policy,
        )
        assert any(m.context_start <= 999.5 <= m.context_end for m in result.moments), [
            (m.id, m.context_start, m.context_end) for m in result.moments
        ]
        assert result.metadata["onsets_kept"] >= 1 or any(m.id == "mom-weak" for m in result.moments)

    def test_p0_6_disjoint_moments_select_as_before(self, optimizer, policy) -> None:
        a = _moment((0.0, 30.0), (10.0, 20.0), ident="a", score=0.6)
        b = _moment((100.0, 130.0), (110.0, 120.0), ident="b", score=0.6)
        c = _moment((200.0, 230.0), (210.0, 220.0), ident="c", score=0.6)
        result = optimise([a, b, c], target_seconds=90.0, config=optimizer, policy=policy)
        assert [m.id for m in result.moments] == ["a", "b", "c"]
        assert result.metadata["onsets_kept"] == 0


class TestP06TheKnapsackPricesWhatItCommitsTo:
    def test_p0_6_a_moment_costs_the_onsets_it_will_absorb(self) -> None:
        strong, weak, surprise, _kill = _benchmark_shape()
        pool = [strong, weak, surprise]
        # Strong alone is 91.5 s; choosing it drops weak, whose kill (999.5-1013.6)
        # then comes with it: 892.2-1013.6.
        assert situations.committed_duration(strong, pool, min_importance=0.5) == pytest.approx(
            1013.6 - 892.2
        )
        assert situations.committed_duration(strong, pool, min_importance=0.95) == pytest.approx(
            strong.context_duration
        )
        far = _moment((2000.0, 2050.0), (2020.0, 2030.0), ident="far")
        assert situations.committed_duration(far, pool, min_importance=0.5) == pytest.approx(50.0)

    def test_p0_6_absorption_is_idempotent(self) -> None:
        strong, weak, surprise, _kill = _benchmark_shape()
        once, absorbed, _ = situations.absorb_onsets([strong, surprise], [weak], min_importance=0.5)
        twice, again, seconds = situations.absorb_onsets(once, [weak], min_importance=0.5)
        assert absorbed and not again and seconds == 0.0
        assert [m.context_end for m in twice] == [m.context_end for m in once]
