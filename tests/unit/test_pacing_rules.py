"""V2-P3: the shot length as a decision with reasons, not a lookup.

Every rule here exists because its absence is visible on screen. Each test
states the defect first, because that is what the rule is for.
"""

from __future__ import annotations

import pytest

from backend.editorial.pacing_engine import (
    ON_THE_BEAT_SECONDS,
    PacingContext,
    ShotLength,
    context_at,
    describe,
    shot_length,
)

pytestmark = pytest.mark.unit


def _at(config, **kwargs):
    return shot_length(PacingContext(position=10.0, **kwargs), config)


class TestTheBandIsWhereItStarts:
    def test_the_level_sets_the_opening_number(self, config) -> None:
        calm = _at(config, level="calm")
        climax = _at(config, level="climax")

        assert calm.seconds == config.editorial.pacing.bands.calm.max
        assert climax.seconds == config.editorial.pacing.bands.climax.max
        assert climax.seconds < calm.seconds

    def test_the_reasons_are_kept(self, config) -> None:
        decision = _at(config, level="normal")

        assert decision.rules[0].startswith("normal band caps at")
        assert "normal" in describe(decision)


class TestNeverCutInsideASentence:
    """Half a sentence is a defect the viewer hears, and no pacing target is
    worth it -- this is the one rule allowed to run past the band."""

    def test_a_shot_is_held_until_the_speaker_stops(self, config) -> None:
        talking = _at(config, level="climax", speech=True, speech_ends_in=6.0)

        assert talking.seconds == pytest.approx(6.0)
        assert any("finish a sentence" in rule for rule in talking.rules)

    def test_silence_leaves_the_band_alone(self, config) -> None:
        quiet = _at(config, level="climax", speech=False, speech_ends_in=6.0)

        assert quiet.seconds == config.editorial.pacing.bands.climax.max

    def test_a_sentence_shorter_than_the_shot_changes_nothing(self, config) -> None:
        decision = _at(config, level="calm", speech=True, speech_ends_in=2.0)

        assert decision.seconds == config.editorial.pacing.bands.calm.max


class TestLandOnTheBeat:
    """Cutting a beat early is the commonest amateur tell in a gaming edit:
    the explosion happens in the next shot instead of this one."""

    def test_a_shot_stretches_to_reach_the_event(self, config) -> None:
        decision = _at(config, level="climax", next_event_in=2.0)

        assert decision.seconds == pytest.approx(2.0)
        assert any("landed on an event" in rule for rule in decision.rules)

    def test_a_shot_shortens_to_stop_on_the_event(self, config) -> None:
        decision = _at(config, level="calm", next_event_in=4.0)

        assert decision.seconds == pytest.approx(4.0)

    def test_a_distant_event_is_not_chased(self, config) -> None:
        decision = _at(config, level="climax", next_event_in=30.0)

        assert decision.seconds == config.editorial.pacing.bands.climax.max

    def test_speech_wins_over_the_beat(self, config) -> None:
        # Landing on an explosion is worth a lot; cutting a word in half is
        # worth more, and the order of the rules says so.
        decision = _at(
            config, level="climax", speech=True, speech_ends_in=5.0, next_event_in=1.0
        )

        assert decision.seconds == pytest.approx(5.0)

    def test_the_window_is_the_declared_one(self, config) -> None:
        band = config.editorial.pacing.bands.high.max
        just_outside = band + ON_THE_BEAT_SECONDS + 0.5

        assert _at(config, level="high", next_event_in=just_outside).seconds == band


class TestBreakingAStutter:
    def test_a_run_of_very_short_shots_is_relieved(self, config) -> None:
        # Two machine-gun shots are pace; five are a glitch. Both shots have
        # to be short for it to be a run -- a 1.8s climax shot after a 0.9s
        # one is not a stutter, and the rule leaves it alone.
        after_short = _at(config, level="climax", next_event_in=0.9, previous_length=0.9)
        alone = _at(config, level="climax", next_event_in=0.9, previous_length=4.0)

        assert after_short.seconds > alone.seconds
        assert any("very short shots" in rule for rule in after_short.rules)

    def test_a_normal_previous_shot_changes_nothing(self, config) -> None:
        decision = _at(config, level="climax", previous_length=4.0)

        assert decision.seconds == config.editorial.pacing.bands.climax.max

    def test_a_shot_that_is_not_short_is_not_a_stutter(self, config) -> None:
        decision = _at(config, level="calm", previous_length=0.9)

        assert decision.seconds == config.editorial.pacing.bands.calm.max


class TestCutOnMovement:
    def test_a_still_picture_holds_for_something_to_cut_on(self, config) -> None:
        # Motion masks a join; on a still frame the same cut announces itself.
        still = _at(config, level="normal", motion_at_cut=0.05)
        moving = _at(config, level="normal", motion_at_cut=0.9)

        assert still.seconds > moving.seconds
        assert any("still here" in rule for rule in still.rules)


class TestSustainedTension:
    def test_a_long_climb_cuts_tighter_than_a_spike(self, config) -> None:
        climbing = _at(config, level="tension", tension=0.95)
        spiked = _at(config, level="tension", tension=0.1)

        assert climbing.seconds < spiked.seconds
        assert climbing.seconds >= config.editorial.pacing.bands.tension.min

    def test_it_never_leaves_the_band(self, config) -> None:
        decision = _at(config, level="calm", tension=1.0)

        assert decision.seconds >= config.editorial.pacing.bands.calm.min


class TestRolesAtTheEdges:
    def test_the_hook_is_tighter(self, config) -> None:
        hook = _at(config, level="normal", role="hook")
        body = _at(config, level="normal", role="body")

        assert hook.seconds < body.seconds

    def test_the_last_shot_keeps_its_tail(self, config) -> None:
        # An ending that stops is not an ending.
        ending = _at(config, level="normal", role="ending")
        body = _at(config, level="normal", role="body")

        assert ending.seconds > body.seconds


class TestTheFloorHolds:
    def test_nothing_goes_under_the_readability_floor(self, config) -> None:
        floor = config.editorial.pacing.min_piece_seconds
        # An event five hundredths of a second away would otherwise produce a
        # shot nobody can read.
        squeezed = _at(config, level="climax", next_event_in=0.05)

        assert squeezed.seconds == pytest.approx(floor)
        assert any("readability floor" in rule for rule in squeezed.rules)

    def test_the_hook_never_goes_under_its_own_band(self, config) -> None:
        # The hook tightens, but the band's minimum is the tightening's floor.
        hook = _at(config, level="climax", role="hook")

        assert hook.seconds >= config.editorial.pacing.bands.climax.min


class TestReadingTheSession:
    """`context_at` is the half that talks to the lanes."""

    class _Lanes:
        def __init__(self, **lanes):
            self.media_id = "media-aaaaaaaaaaaa"
            self.hz = 2
            self.duration_s = 60.0
            n = 120
            self._lanes = {
                name: list(values) + [0.0] * (n - len(values))
                for name, values in lanes.items()
            }
            for name in ("intensity", "tension", "motion", "speech"):
                self._lanes.setdefault(name, [0.3] * n)

        def _index(self, t):
            return max(0, min(119, int(t * self.hz)))

        def lane(self, name):
            return self._lanes[name]

        def window(self, name, start, end):
            a, b = self._index(start), self._index(end)
            return self._lanes[name][a : b + 1] if b > a else [self._lanes[name][a]]

        def value_at(self, name, seconds):
            return self._lanes[name][self._index(seconds)]

        def intensity_between(self, start, end):
            window = self.window("intensity", start, end)
            return sum(window) / len(window)

        def level_for(self, start, end):
            return "normal"

        def shape(self, *, min_segment=None):
            return ()

        def summary(self):
            return []

    def test_it_reads_speech_and_finds_where_it_ends(self) -> None:
        lanes = self._Lanes(speech=[1.0] * 20 + [0.0] * 100)

        context = context_at(0.0, lanes)

        assert context.speech is True
        assert context.speech_ends_in == pytest.approx(10.0)

    def test_endless_speech_does_not_hold_a_shot_forever(self) -> None:
        # A commentary track would otherwise hold one shot for the whole
        # recording, which is not an editorial decision, it is a hang.
        lanes = self._Lanes(speech=[1.0] * 120)

        context = context_at(0.0, lanes)

        assert context.speech_ends_in == 0.0

    def test_the_next_event_is_the_next_one_ahead(self) -> None:
        context = context_at(10.0, self._Lanes(), events=(2.0, 14.5, 40.0))

        assert context.next_event_in == pytest.approx(4.5)

    def test_no_event_ahead_is_infinity_not_zero(self) -> None:
        context = context_at(50.0, self._Lanes(), events=(2.0, 14.5))

        assert context.next_event_in == float("inf")

    def test_density_counts_the_neighbourhood(self) -> None:
        context = context_at(10.0, self._Lanes(), events=(6.0, 9.0, 11.0, 40.0))

        assert context.event_density == 3.0

    def test_motion_is_read_where_the_cut_would_land(self) -> None:
        # Not where the shot starts: the cut is what lands on the frame.
        lanes = self._Lanes(motion=[0.9] * 4 + [0.02] * 116)

        context = context_at(0.0, lanes, probe_seconds=2.0)

        assert context.motion_at_cut == pytest.approx(0.02)


def test_a_shot_length_is_a_number_where_a_number_is_wanted() -> None:
    # The guard multiplies and compares it; the report reads its reasons.
    decision = ShotLength(seconds=2.5, level="high", rules=("because",))

    assert float(decision) == 2.5


class TestNoCutLandsInsideAWord:
    """The engine holds a shot for a speaker who is *already* talking. A shot
    that begins in silence and runs into a sentence needed the other half of
    the rule -- and the last shot of the video, whose end the plan fixes,
    needed a third.
    """

    def _walk(self, no_cut, **kwargs):
        from backend.timeline.builder import PlannedClip
        from backend.timeline.screen_guard import _split_long_clips

        return _split_long_clips(
            [PlannedClip(media_id="m", source_start=0.0, source_end=30.0)],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=lambda piece, previous=0.0: 5.0,
            no_cut_by_media={"m": no_cut},
            **kwargs,
        )

    def test_a_cut_moves_past_the_word_it_would_have_split(self) -> None:
        words = [(4.5, 6.0), (12.0, 13.0)]

        pieces = self._walk(words)

        for piece in pieces:
            assert not any(
                start + 1e-6 < piece.source_end < stop - 1e-6 for start, stop in words
            ), f"a cut landed at {piece.source_end} inside a word"

    def test_the_final_shot_snaps_back_rather_than_ending_mid_syllable(self) -> None:
        # Forward is not available: the plan fixed where the clip ends.
        pieces = self._walk([(29.5, 31.0)])

        assert pieces[-1].source_end <= 29.5

    def test_a_word_with_nowhere_legal_to_go_leaves_the_cut_alone(self) -> None:
        # A piece under the floor is worse than a cut inside a word, and
        # inventing one silently would be worse than both.
        pieces = self._walk([(0.0, 40.0)])

        assert pieces, "the walk still produces an edit"

    def test_without_zones_the_walk_is_unchanged(self) -> None:
        with_zones = self._walk([])
        from backend.timeline.builder import PlannedClip
        from backend.timeline.screen_guard import _split_long_clips

        plain = _split_long_clips(
            [PlannedClip(media_id="m", source_start=0.0, source_end=30.0)],
            scenes_by_media={},
            max_seconds=75.0,
            cap_fn=lambda piece, previous=0.0: 5.0,
        )

        assert [(p.source_start, p.source_end) for p in with_zones] == [
            (p.source_start, p.source_end) for p in plain
        ]
