"""J/L boundary planning and the offset audio graph (backend/rendering/jl.py).

The planner is the product rule — which boundaries earn an audio offset, and
how far the material actually allows — and it is tested without FFmpeg in the
room. The graph builder is tested as strings for the same reason every argv
builder in the rendering layer is: a wrong trim start is a desynced boundary
that no log will ever name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config.schema import JLCutsConfig
from backend.core.models.enums import TrackKind
from backend.rendering.jl import assembly_arguments, offsets, plan_boundaries
from backend.timeline.models import Timeline, TimelineClip, Track

pytestmark = pytest.mark.unit

MEDIA_A = "media-aaaaaaaaaaaa"
MEDIA_B = "media-bbbbbbbbbbbb"


def _clip(
    index: int,
    media_id: str,
    *,
    source_in: float,
    seconds: float,
    at: float,
    speed: float = 1.0,
) -> TimelineClip:
    return TimelineClip(
        id=f"clip-{index:012d}",
        media_id=media_id,
        clip_index=index,
        source_in=source_in,
        source_out=source_in + seconds * speed,
        timeline_start=at,
        timeline_end=at + seconds,
        speed=speed,
    )


def _timeline(*clips: TimelineClip) -> Timeline:
    return Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
        Track(kind=TrackKind.VIDEO, clips=tuple(clips))
    )


def _config(**overrides) -> JLCutsConfig:
    return JLCutsConfig(enabled=True, **overrides)


@pytest.fixture
def two_clips() -> Timeline:
    """A at source 10-18 filling 0-8; B at source 30-38 filling 8-16."""
    return _timeline(
        _clip(0, MEDIA_A, source_in=10.0, seconds=8.0, at=0.0),
        _clip(1, MEDIA_B, source_in=30.0, seconds=8.0, at=8.0),
    )


class TestWhichCutItIs:
    def test_speech_opening_the_incoming_clip_makes_a_j(self, two_clips) -> None:
        plans = plan_boundaries(two_clips, {MEDIA_B: [(30.2, 32.0)]}, _config())

        assert [plan.kind for plan in plans] == ["j"]
        assert plans[0].dt == pytest.approx(0.6)

    def test_speech_tailing_the_outgoing_clip_makes_an_l(self, two_clips) -> None:
        plans = plan_boundaries(two_clips, {MEDIA_A: [(16.5, 17.8)]}, _config())

        assert [plan.kind for plan in plans] == ["l"]
        assert plans[0].dt == pytest.approx(0.6)

    def test_silence_on_both_sides_stays_a_hard_cut(self, two_clips) -> None:
        # Gunfire does not need a lead. Speech far from either edge is the
        # same as no speech at all.
        plans = plan_boundaries(
            two_clips, {MEDIA_A: [(11.0, 12.0)], MEDIA_B: [(35.0, 36.0)]}, _config()
        )

        assert [plan.kind for plan in plans] == ["hard"]
        assert plans[0].dt == 0.0

    def test_a_j_wins_when_both_sides_carry_speech(self, two_clips) -> None:
        # The incoming line is the one the offset sells: hearing the next
        # thing early hooks harder than hearing the last thing late.
        plans = plan_boundaries(
            two_clips,
            {MEDIA_A: [(17.5, 18.4)], MEDIA_B: [(30.1, 31.0)]},
            _config(),
        )

        assert plans[0].kind == "j"

    def test_disabled_config_plans_every_boundary_hard(self, two_clips) -> None:
        plans = plan_boundaries(
            two_clips, {MEDIA_B: [(30.2, 32.0)]}, JLCutsConfig(enabled=False)
        )

        assert [plan.kind for plan in plans] == ["hard"]

    def test_a_speed_warped_neighbour_stays_hard(self) -> None:
        # A slow-motion clip's timeline seconds are not its source seconds; an
        # offset computed in one and extracted in the other would desync the
        # very boundary it decorates.
        timeline = _timeline(
            _clip(0, MEDIA_A, source_in=10.0, seconds=8.0, at=0.0),
            _clip(1, MEDIA_B, source_in=30.0, seconds=8.0, at=8.0, speed=0.5),
        )

        plans = plan_boundaries(timeline, {MEDIA_B: [(30.2, 32.0)]}, _config())

        assert [plan.kind for plan in plans] == ["hard"]

    def test_every_internal_boundary_is_planned_once(self) -> None:
        timeline = _timeline(
            _clip(0, MEDIA_A, source_in=10.0, seconds=4.0, at=0.0),
            _clip(1, MEDIA_B, source_in=30.0, seconds=4.0, at=4.0),
            _clip(2, MEDIA_A, source_in=50.0, seconds=4.0, at=8.0),
        )

        plans = plan_boundaries(timeline, {}, _config())

        assert [plan.index for plan in plans] == [0, 1]

    def test_a_single_clip_timeline_has_no_boundaries(self) -> None:
        timeline = _timeline(_clip(0, MEDIA_A, source_in=10.0, seconds=8.0, at=0.0))

        assert plan_boundaries(timeline, {MEDIA_A: [(10.0, 18.0)]}, _config()) == []

    def test_a_frozen_neighbour_stays_hard(self) -> None:
        # A freeze/ramp the EDL re-laid bends the source-to-timeline mapping
        # exactly the way a speed warp does; the offset arithmetic is wrong in
        # the same way, so the boundary is planned hard for the same reason.
        frozen = TimelineClip(
            id="clip-0000jlfroze",
            media_id=MEDIA_B,
            clip_index=1,
            source_in=30.0,
            source_out=38.0,
            timeline_start=8.0,
            timeline_end=17.5,
            metadata={"retime": {"effect": "freeze_frame", "at": 4.0, "extra_seconds": 1.5}},
        )
        timeline = _timeline(
            _clip(0, MEDIA_A, source_in=10.0, seconds=8.0, at=0.0), frozen
        )

        plans = plan_boundaries(timeline, {MEDIA_B: [(30.2, 32.0)]}, _config())

        assert [plan.kind for plan in plans] == ["hard"]


class TestHowFarTheAudioMayReach:
    def test_the_lead_is_clamped_to_the_material_before_the_in_point(self) -> None:
        # The lead plays sound from before source_in; a recording with only
        # 0.3 s there has only 0.3 s to give.
        timeline = _timeline(
            _clip(0, MEDIA_A, source_in=10.0, seconds=8.0, at=0.0),
            _clip(1, MEDIA_B, source_in=0.3, seconds=8.0, at=8.0),
        )

        plans = plan_boundaries(timeline, {MEDIA_B: [(0.4, 1.2)]}, _config())

        assert plans[0].kind == "j"
        assert plans[0].dt == pytest.approx(0.3)

    def test_the_lead_is_clamped_to_half_the_shorter_neighbour(self) -> None:
        # An offset that eats half a clip has replaced the cut, not softened
        # it.
        timeline = _timeline(
            _clip(0, MEDIA_A, source_in=10.0, seconds=0.8, at=0.0),
            _clip(1, MEDIA_B, source_in=30.0, seconds=8.0, at=0.8),
        )

        plans = plan_boundaries(timeline, {MEDIA_B: [(30.1, 31.0)]}, _config())

        assert plans[0].dt == pytest.approx(0.4)

    def test_the_lead_is_clamped_by_the_configured_maximum(self, two_clips) -> None:
        plans = plan_boundaries(
            two_clips, {MEDIA_B: [(30.2, 32.0)]}, _config(max_lead_seconds=0.2)
        )

        assert plans[0].dt == pytest.approx(0.2)

    def test_a_lead_too_small_to_hear_stays_hard(self) -> None:
        # 10 ms of material is shorter than the fade that would smooth it.
        timeline = _timeline(
            _clip(0, MEDIA_A, source_in=10.0, seconds=8.0, at=0.0),
            _clip(1, MEDIA_B, source_in=0.01, seconds=8.0, at=8.0),
        )

        plans = plan_boundaries(timeline, {MEDIA_B: [(0.1, 1.0)]}, _config())

        assert plans[0].kind == "hard"

    def test_a_trail_is_clamped_to_the_recordings_remaining_length(self, two_clips) -> None:
        plans = plan_boundaries(
            two_clips,
            {MEDIA_A: [(17.5, 18.4)]},
            _config(),
            source_durations={MEDIA_A: 18.25},
        )

        assert plans[0].kind == "l"
        assert plans[0].dt == pytest.approx(0.25)

    def test_a_recording_that_ends_at_the_out_point_cannot_trail(self, two_clips) -> None:
        plans = plan_boundaries(
            two_clips,
            {MEDIA_A: [(17.5, 18.4)]},
            _config(),
            source_durations={MEDIA_A: 18.0},
        )

        assert plans[0].kind == "hard"

    def test_an_unknown_source_length_allows_the_trail(self, two_clips) -> None:
        # Planning cannot infer a recording's end from the timeline; the
        # extraction clamps by running out of file, which shortens the trail
        # rather than losing the cut.
        plans = plan_boundaries(two_clips, {MEDIA_A: [(17.5, 18.4)]}, _config())

        assert plans[0].kind == "l"
        assert plans[0].dt == pytest.approx(0.6)


class TestTheTimelineEdges:
    def test_the_first_start_and_last_end_are_never_offset(self) -> None:
        # Speech everywhere, offsets at both internal boundaries — and still
        # no lead before the first clip or trail after the last.
        clips = (
            _clip(0, MEDIA_A, source_in=10.0, seconds=4.0, at=0.0),
            _clip(1, MEDIA_B, source_in=30.0, seconds=4.0, at=4.0),
            _clip(2, MEDIA_A, source_in=50.0, seconds=4.0, at=8.0),
        )
        timeline = _timeline(*clips)
        plans = plan_boundaries(
            timeline,
            {MEDIA_A: [(10.0, 12.0)], MEDIA_B: [(30.1, 31.0), (33.5, 34.0)]},
            _config(),
        )
        assert [plan.kind for plan in plans] == ["j", "l"]

        placed = offsets(clips, plans)

        assert placed[0][0] == 0.0, "the timeline's first start must not lead"
        assert placed[-1][1] == 0.0, "the timeline's last end must not trail"
        assert placed[1] == (pytest.approx(0.6), pytest.approx(0.6))

    def test_a_plan_for_the_wrong_timeline_is_refused(self, two_clips) -> None:
        clips = two_clips.video_clips()

        with pytest.raises(ValueError, match="boundaries"):
            offsets(clips, [])


class TestTheAssemblyGraph:
    def _argv(self, timeline, plans, tmp_path: Path, **config) -> list[str]:
        clips = timeline.video_clips()
        return assembly_arguments(
            clips,
            plans,
            sources={MEDIA_A: tmp_path / "a.mp4", MEDIA_B: tmp_path / "b.mp4"},
            destination=tmp_path / "programme_audio.wav",
            config=_config(**config),
        )

    def test_a_j_lead_extends_the_extract_and_advances_the_placement(
        self, two_clips, tmp_path: Path
    ) -> None:
        # Both shifted by the same dt, so at t_cut the audio reaches
        # source_in exactly and the body stays in sync.
        plans = plan_boundaries(two_clips, {MEDIA_B: [(30.2, 32.0)]}, _config())

        argv = self._argv(two_clips, plans, tmp_path)
        graph = argv[argv.index("-filter_complex") + 1]

        assert "atrim=start=29.400000:end=38.000000" in graph
        assert "adelay=7400|7400" in graph

    def test_an_l_trail_extends_the_outgoing_extract_in_place(
        self, two_clips, tmp_path: Path
    ) -> None:
        plans = plan_boundaries(two_clips, {MEDIA_A: [(17.5, 18.4)]}, _config())

        argv = self._argv(two_clips, plans, tmp_path)
        graph = argv[argv.index("-filter_complex") + 1]

        assert "atrim=start=10.000000:end=18.600000" in graph
        assert "adelay=8000|8000" in graph, "the incoming clip stays where it was"

    def test_offset_seams_get_the_crossfade_and_outer_edges_the_micro_fade(
        self, two_clips, tmp_path: Path
    ) -> None:
        plans = plan_boundaries(two_clips, {MEDIA_B: [(30.2, 32.0)]}, _config())

        argv = self._argv(two_clips, plans, tmp_path)
        graph = argv[argv.index("-filter_complex") + 1]
        outgoing = next(part for part in graph.split(";") if part.startswith("[0:a:0]"))
        incoming = next(part for part in graph.split(";") if part.startswith("[1:a:0]"))

        assert "afade=t=in:st=0:d=0.030" in outgoing, "the video's opening edge"
        assert "afade=t=out:st=7.880:d=0.120" in outgoing, "faded under the lead"
        assert "afade=t=in:st=0:d=0.120" in incoming, "the lead fades in"
        assert "afade=t=out:st=8.570:d=0.030" in incoming, "the video's closing edge"

    def test_the_mixer_sums_without_renormalising(self, two_clips, tmp_path: Path) -> None:
        plans = plan_boundaries(two_clips, {MEDIA_B: [(30.2, 32.0)]}, _config())

        argv = self._argv(two_clips, plans, tmp_path)
        graph = argv[argv.index("-filter_complex") + 1]

        assert "amix=inputs=2:normalize=0:dropout_transition=0[jl]" in graph

    def test_the_argv_is_inspectable_and_ends_at_the_destination(
        self, two_clips, tmp_path: Path
    ) -> None:
        plans = plan_boundaries(two_clips, {MEDIA_B: [(30.2, 32.0)]}, _config())

        argv = self._argv(two_clips, plans, tmp_path)

        assert argv[0] == "-i"
        assert argv[-1] == str(tmp_path / "programme_audio.wav")
        assert argv[argv.index("-c:a") + 1] == "pcm_s16le"
        assert argv[argv.index("-map") + 1] == "[jl]"

    def test_a_frozen_clip_enters_the_graph_through_the_warped_body(
        self, tmp_path: Path
    ) -> None:
        # Its boundaries are hard, but its *body* still occupies more timeline
        # than its source span: a linear extract ended 1.5 s early and every
        # second of it after the anchor played early by the hold.
        frozen = TimelineClip(
            id="clip-000jlwarped",
            media_id=MEDIA_B,
            clip_index=1,
            source_in=30.0,
            source_out=38.0,
            timeline_start=8.0,
            timeline_end=17.5,
            metadata={"retime": {"effect": "freeze_frame", "at": 4.0, "extra_seconds": 1.5}},
        )
        timeline = _timeline(
            _clip(0, MEDIA_A, source_in=10.0, seconds=8.0, at=0.0), frozen
        )
        plans = plan_boundaries(timeline, {MEDIA_B: [(30.2, 32.0)]}, _config())
        assert [plan.kind for plan in plans] == ["hard"]

        argv = self._argv(timeline, plans, tmp_path)
        graph = argv[argv.index("-filter_complex") + 1]

        assert "apad=pad_dur=1.500000" in graph, "the hold is silence, in place"
        assert "concat=n=2:v=0:a=1" in graph
        assert "adelay=8000|8000" in graph, "the body is still placed at its own start"
        assert "[c1]" in graph, "the warped chain feeds the same mixer slot"
