"""P0.6, the owner's items 2 and 3: no shot under the floor, no band over its
budget (backend/timeline/shot_floor.py).

The numbers here are the benchmark's shape in miniature: a run of pieces the
pacing engine cut to the climax band's cap, which is what made the gate render
read as a dropped connection.
"""

from __future__ import annotations

import pytest

from backend.core.models.enums import MomentType
from backend.timeline import shot_floor
from backend.timeline.authorization import AuthorizedSpan, Granter
from backend.timeline.builder import PlannedClip

pytestmark = pytest.mark.unit

MEDIA = "media-000000000001"


def _clip(start: float, end: float, *, moment: str = "mom-1", authorized=()) -> PlannedClip:
    return PlannedClip(
        media_id=MEDIA,
        source_start=start,
        source_end=end,
        moment_id=moment,
        moment_type=MomentType.CHAOS,
        score=0.5,
        role="body",
        authorized=tuple(authorized),
    )


def _run(start: float, length: float, count: int, *, moment: str = "mom-1"):
    """``count`` contiguous pieces of ``length``, as the pacing engine cuts them."""
    return [
        _clip(start + index * length, start + (index + 1) * length, moment=moment)
        for index in range(count)
    ]


class TestP06TheShotFloor:
    def test_p0_6_no_clip_is_shorter_than_the_floor(self) -> None:
        clips = [*_run(100.0, 1.8, 5), _clip(200.0, 212.0, moment="mom-2")]
        kept, notes = shot_floor.apply_to_clips(clips, floor=3.0)
        assert kept
        assert all(clip.seconds >= 3.0 - 1e-9 for clip in kept), [c.seconds for c in kept]
        assert any("shot floor" in note for note in notes)

    def test_p0_6_the_merge_is_pairwise_not_a_chain(self) -> None:
        # Eight 1.5 s pieces, floor 3.0. The rule joins the shortest shot to
        # the shorter of its neighbours and starts again: four 3.0 s shots.
        # Folding each short piece into the group being accumulated -- the
        # first version of this, measured -- gives one 12 s shot instead, and
        # took the benchmark's climax band from a 1.8 s median to 15.7 s.
        kept, _ = shot_floor.apply_to_clips(_run(100.0, 1.5, 8), floor=3.0)
        assert [round(clip.seconds, 3) for clip in kept] == [3.0, 3.0, 3.0, 3.0]

    def test_p0_6_the_shortest_joins_the_shorter_neighbour(self) -> None:
        clips = [_clip(100.0, 105.0), _clip(105.0, 106.0), _clip(106.0, 116.0)]
        kept, _ = shot_floor.apply_to_clips(clips, floor=3.0)
        assert [(c.source_start, c.source_end) for c in kept] == [(100.0, 106.0), (106.0, 116.0)]

    def test_p0_6_a_piece_of_another_moment_is_not_a_neighbour(self) -> None:
        # Two clips that touch in the source are two decisions the story
        # made; joining them would put footage under a grant that never
        # covered it (P0.3). With nothing of its own to join, the short piece
        # is not shown.
        clips = [_clip(100.0, 101.0, moment="mom-1"), _clip(101.0, 120.0, moment="mom-2")]
        kept, notes = shot_floor.apply_to_clips(clips, floor=3.0)
        assert [(c.source_start, c.source_end) for c in kept] == [(101.0, 120.0)]
        assert any("dropped 1 piece" in note for note in notes)

    def test_p0_6_a_joined_shot_stays_inside_the_grant_its_pieces_had(self) -> None:
        grant = AuthorizedSpan(MEDIA, 95.0, 130.0, Granter.CONTEXT_EXPANSION, "test")
        clips = [
            _clip(100.0 + index * 1.5, 101.5 + index * 1.5, authorized=(grant,))
            for index in range(4)
        ]
        kept, _ = shot_floor.apply_to_clips(clips, floor=3.0)
        for clip in kept:
            assert clip.authorized == (grant,)
            assert grant.covers(clip.source_start, clip.source_end)

    def test_p0_6_a_clip_already_over_the_floor_is_untouched(self) -> None:
        clips = [_clip(100.0, 110.0), _clip(200.0, 208.0, moment="mom-2")]
        kept, notes = shot_floor.apply_to_clips(clips, floor=3.0)
        assert kept == clips
        assert notes == []

    def test_p0_6_no_floor_asked_for_changes_nothing(self) -> None:
        clips = _run(100.0, 0.5, 6)
        kept, notes = shot_floor.apply_to_clips(clips, floor=0.0)
        assert kept == clips and notes == []

