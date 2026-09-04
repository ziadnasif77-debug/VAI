"""P0.6: the jump-cut decision (backend/editorial/jump_cuts.py) and the two
places the screen guard asks it -- the sliver after a hot cut, and the dead
interior of a clip."""

from __future__ import annotations

from itertools import pairwise

import pytest

from backend.analysis.frame_state import StateSpan
from backend.core.models.enums import FrameState
from backend.editorial.jump_cuts import Budget, Evidence, decide
from backend.timeline.builder import PlannedClip
from backend.timeline.screen_guard import guard_clips

pytestmark = pytest.mark.unit

MEDIA = "media-1"
DEAD = ((100.0, 110.0),)


class TestP06TheDecision:
    def test_p0_6_cutting_inside_a_word_fails(self) -> None:
        evidence = Evidence(dead=DEAD, words=((99.8, 100.4),))
        verdict = decide(100.0, 110.0, evidence, min_gap_seconds=4.0)
        assert not verdict
        assert "inside a word" in verdict.reason

    def test_p0_6_a_clean_word_boundary_passes(self) -> None:
        # The word ends exactly where the cut begins: an edge, not an interior.
        evidence = Evidence(dead=DEAD, words=((99.2, 100.0), (110.0, 110.6)))
        verdict = decide(100.0, 110.0, evidence, min_gap_seconds=4.0)
        assert verdict, verdict.reason

    def test_p0_6_a_gap_that_is_not_dead_stays(self) -> None:
        verdict = decide(100.0, 100.35, Evidence(), min_gap_seconds=4.0)
        assert not verdict
        assert "live footage" in verdict.reason
        # Partly dead is not dead.
        verdict = decide(98.0, 110.0, Evidence(dead=DEAD), min_gap_seconds=4.0)
        assert not verdict and "live footage" in verdict.reason

    def test_p0_6_a_gap_too_short_to_matter_stays(self) -> None:
        verdict = decide(100.0, 102.0, Evidence(dead=DEAD), min_gap_seconds=4.0)
        assert not verdict
        assert "shorter than" in verdict.reason

    def test_p0_6_a_cut_that_removes_an_event_onset_is_refused(self) -> None:
        verdict = decide(100.0, 110.0, Evidence(dead=DEAD, onsets=(104.5,)), min_gap_seconds=4.0)
        assert not verdict
        assert "event onset at 104.50" in verdict.reason
        # An onset at the very end is the next piece's first frame, not removed.
        assert decide(100.0, 110.0, Evidence(dead=DEAD, onsets=(110.0,)), min_gap_seconds=4.0)

    def test_p0_6_a_cut_that_removes_a_reaction_is_refused(self) -> None:
        verdict = decide(
            100.0, 110.0, Evidence(dead=DEAD, reactions=((108.0, 112.0),)), min_gap_seconds=4.0
        )
        assert not verdict
        assert "reaction" in verdict.reason

    def test_p0_6_the_budget_is_spent_by_the_cuts_it_allows(self) -> None:
        budget = Budget(per_minute=2)
        evidence = Evidence(dead=((100.0, 110.0), (120.0, 130.0), (140.0, 150.0)))
        assert decide(100.0, 110.0, evidence, min_gap_seconds=4.0, budget=budget)
        assert decide(120.0, 130.0, evidence, min_gap_seconds=4.0, budget=budget)
        third = decide(140.0, 150.0, evidence, min_gap_seconds=4.0, budget=budget)
        assert not third and "spent" in third.reason
        # A refused cut does not spend, and the window moves on.
        assert decide(200.0, 210.0, Evidence(dead=((200.0, 210.0),)), min_gap_seconds=4.0, budget=budget)


def _clip(start: float, end: float) -> PlannedClip:
    return PlannedClip(media_id=MEDIA, source_start=start, source_end=end)


def _span(start: float, end: float, state: FrameState = FrameState.MENU) -> StateSpan:
    return StateSpan(state, start, end, observations=3)


class TestP06TheGuardAsks:
    def _guard(self, clips, states=(), **kw):
        return guard_clips(
            clips,
            states_by_media={MEDIA: list(states)},
            scenes_by_media={MEDIA: []},
            min_observations=1,
            **kw,
        )

    def test_p0_6_a_pacing_piece_resumes_where_it_cut_on_live_footage(self) -> None:
        # A hot cap splits the clip; the old walker skipped 0.35 s after
        # every cut. Live footage is not dead: the pieces are contiguous.
        pieces = self._guard(
            [_clip(0.0, 12.0)],
            cap_fn=lambda clip, previous=0.0: 2.0,
            jump_cut_gap=0.35,
            jump_cut_below=8.0,
            min_piece_seconds=0.8,
            onsets_by_media={MEDIA: []},
        )
        assert len(pieces) > 1
        for left, right in pairwise(pieces):
            assert right.source_start == pytest.approx(left.source_end), (left, right)
        assert any("jump cut refused" in s for s in pieces[0].sources)

    def test_p0_6_without_evidence_the_old_skip_stands(self) -> None:
        pieces = self._guard(
            [_clip(0.0, 12.0)],
            cap_fn=lambda clip, previous=0.0: 2.0,
            jump_cut_gap=0.35,
            jump_cut_below=8.0,
            min_piece_seconds=0.8,
        )
        # No caller gave onsets, reactions or words: guard_clips still builds
        # evidence from the dead states alone, so the sliver is live and refused.
        for left, right in pairwise(pieces):
            assert right.source_start == pytest.approx(left.source_end)

    def test_p0_6_a_dead_interior_is_cut_when_nothing_forbids_it(self) -> None:
        pieces = self._guard(
            [_clip(10.0, 40.0)],
            states=[_span(15.0, 21.0)],
            min_piece_seconds=4.0,
        )
        assert [(p.source_start, round(p.source_end, 1)) for p in pieces] == [
            (10.0, 15.0),
            (21.4, 40.0),
        ]

    def test_p0_6_a_dead_interior_holding_an_event_onset_stays(self) -> None:
        pieces = self._guard(
            [_clip(10.0, 40.0)],
            states=[_span(15.0, 21.0)],
            min_piece_seconds=4.0,
            onsets_by_media={MEDIA: [18.0]},
        )
        assert [(p.source_start, p.source_end) for p in pieces] == [(10.0, 40.0)]

    def test_p0_6_a_dead_interior_whose_edge_is_inside_a_word_stays(self) -> None:
        pieces = self._guard(
            [_clip(10.0, 40.0)],
            states=[_span(15.0, 21.0)],
            min_piece_seconds=4.0,
            no_cut_by_media={MEDIA: [(14.7, 15.3)]},
        )
        assert [(p.source_start, p.source_end) for p in pieces] == [(10.0, 40.0)]

    def test_p0_6_a_dead_interior_holding_a_reaction_stays(self) -> None:
        pieces = self._guard(
            [_clip(10.0, 40.0)],
            states=[_span(15.0, 21.0)],
            min_piece_seconds=4.0,
            reactions_by_media={MEDIA: [(20.0, 24.0)]},
        )
        assert [(p.source_start, p.source_end) for p in pieces] == [(10.0, 40.0)]
