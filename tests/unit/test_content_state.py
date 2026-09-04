"""What is on the screen when it is not the game (V2-P0.1).

Written against the recording that made this necessary. Seventeen seconds of
menus, a restart prompt and a loading screen reached a finished video, and the
OCR had read every word of them and stored it. These tests are the consumer
that was missing, and they end at the decision -- "this footage is refused" --
rather than at a parsed string.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.core.models.enums import FrameState
from backend.gaming import content
from backend.gaming.content import ContentState, GameplayState
from backend.gaming.profiles import GENERIC_PROFILE, GameProfile
from backend.gaming.profiles import ContentRule as ProfileContentRule

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Read:
    """An OCR detection, shaped as the repository returns one."""

    text: str
    timestamp: float
    region: str | None = None


@dataclass(frozen=True)
class _Span:
    """A vision span, shaped as `frame_state.non_gameplay` returns one."""

    state: FrameState
    start_seconds: float
    end_seconds: float
    observations: int = 1

    @property
    def duration(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


class TestTheEvidenceThatWasAlreadyThere:
    def test_the_lines_that_reached_the_video_are_now_refused(self) -> None:
        # Verbatim from ocr_results at source 3:51.50 of the 88-minute
        # session. Every one of these was stored, and the stretch they
        # describe played at 1:47 of the finished render.
        states = content.read(
            detections=[
                _Read("LOAD", 231.5),
                _Read("REPLAY", 231.5),
                _Read("REPLANMISSION", 231.5),
                _Read("EXIT TO MENU", 231.5),
                _Read("AGENT DOWN:", 231.5),
                _Read("MISSIONFAILED", 231.5),
            ],
            profile=GENERIC_PROFILE,
        )

        assert states, "the menu that shipped is still invisible"
        assert content.overlaps(content.excluded_spans(states), 232.9, 237.5), (
            "clip 38 of the shipped render is still allowed"
        )

    def test_a_read_becomes_a_span_not_an_instant(self) -> None:
        # The whole design. OCR samples a frame every ~7s on that recording,
        # so a menu on screen for twenty is read once. A point filter over
        # data with extent is the defect this project has now fixed three
        # times; this is the fourth place it would have appeared.
        (state,) = content.read(
            detections=[_Read("MISSION FAILED", 100.0)], profile=GENERIC_PROFILE
        )

        assert state.start < 100.0 < state.end
        assert state.duration > 5.0

    def test_one_menu_is_one_state_however_many_lines_read_it(self) -> None:
        states = content.read(
            detections=[
                _Read("EXIT TO MENU", 231.5),
                _Read("REPLANMISSION", 231.5),
                _Read("MAIN MENU", 233.0),
            ],
            profile=GENERIC_PROFILE,
        )

        menus = [item for item in states if item.state is ContentState.MENU]
        assert len(menus) == 1, "a chatty menu is still one menu"
        assert len(menus[0].evidence) == 3, "and it keeps all three readings"


class TestOcrDecidesAndVisionSupports:
    def test_a_vision_span_alone_can_refuse_footage(self) -> None:
        # The correction that cost six real menus. The first cut of this
        # module distrusted vision because the model had called a MISSION
        # FAILED screen "a combat situation" -- but that was a *description*,
        # and `frame_state` reads *labels*. At that instant the label was
        # `loading`, and it was right. Six menus with no readable text at all
        # reached the shipped render, and only the label sees them.
        states = content.read(
            detections=(),
            frame_spans=[_Span(FrameState.MENU, 100.0, 112.0, observations=3)],
            profile=GENERIC_PROFILE,
        )

        assert states
        assert states[0].excludes, "a menu with no text on it is still invisible"

    def test_more_agreeing_frames_are_worth_more_than_one(self) -> None:
        def confidence(seen: int) -> float:
            return content.read(
                frame_spans=[_Span(FrameState.MENU, 100.0, 112.0, observations=seen)],
                profile=GENERIC_PROFILE,
            )[0].confidence

        assert confidence(4) > confidence(1)
        assert confidence(40) <= content.VISION_AGREED, "and it is still capped"

    def test_text_alone_is_enough(self) -> None:
        (state,) = content.read(
            detections=[_Read("GAME PAUSED", 500.0)], profile=GENERIC_PROFILE
        )

        assert state.excludes

    def test_agreement_between_two_stores_raises_confidence(self) -> None:
        alone = content.read(
            detections=[_Read("GAME PAUSED", 500.0)], profile=GENERIC_PROFILE
        )[0]
        agreed = content.read(
            detections=[_Read("GAME PAUSED", 500.0)],
            frame_spans=[_Span(FrameState.PAUSE, 498.0, 506.0)],
            profile=GENERIC_PROFILE,
        )[0]

        assert agreed.confidence > alone.confidence
        assert {item.source for item in agreed.evidence} == {"ocr", "vision"}


class TestTheProfileVocabulary:
    def test_a_game_may_add_wording_the_generic_table_lacks(self) -> None:
        # HITMAN's loading screen names its targets and says nothing generic.
        # That wording belongs to the game, not to the common table.
        profile = GameProfile(
            id="hitman",
            content_rules=(
                ProfileContentRule(
                    state="loading",
                    name="hitman_target_briefing",
                    patterns=(r"\bDNA\s+SPECIFIC\s+VIRUS\b",),
                    confidence=0.8,
                ),
            ),
        )

        (state,) = content.read(
            detections=[_Read("DNA SPECIFIC VIRUS", 237.5)], profile=profile
        )

        assert state.state is ContentState.LOADING
        assert state.excludes

    def test_a_game_may_disable_a_generic_rule_by_name(self) -> None:
        # The escape hatch `suppressed_generic_rules` already gives the fusion
        # table, for the same reason: the common case is written once, and a
        # game that knows better says so by name.
        profile = GameProfile(id="quiet", suppressed_content_rules=("pause_screen",))

        assert not content.read(
            detections=[_Read("GAME PAUSED", 500.0)], profile=profile
        )

    def test_the_generic_table_still_applies_to_an_unknown_game(self) -> None:
        assert content.rules_for(GENERIC_PROFILE), "an unknown game gets nothing"

    def test_a_rule_that_matches_everything_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no patterns"):
            ProfileContentRule(state="menu", name="everything", patterns=())

    def test_a_rule_naming_an_unknown_state_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unknown content state"):
            ProfileContentRule(state="not_a_state", name="x", patterns=("y",))


class TestWhatIsRefusedAndWhatIsMerelyRecorded:
    def test_a_weak_reading_is_kept_and_not_acted_on(self) -> None:
        # CUTSCENE is inferred, not read: nothing stored names one. It is
        # recorded at a confidence that deliberately cannot refuse footage,
        # until somebody measures it.
        weak = GameplayState(
            state=ContentState.CUTSCENE, start=0.0, end=10.0, confidence=0.5
        )

        assert not weak.excludes
        assert content.excluded_spans([weak]) == []

    def test_gameplay_never_excludes_however_confident(self) -> None:
        state = GameplayState(
            state=ContentState.GAMEPLAY, start=0.0, end=10.0, confidence=1.0
        )

        assert not state.excludes

    def test_touching_a_menu_is_enough_to_be_refused(self) -> None:
        # Overlap, not containment. A clip that merely reaches into a menu is
        # a clip containing a menu, and the containment test is what let three
        # minutes of source through.
        spans = [(100.0, 110.0)]

        assert content.overlaps(spans, 95.0, 101.0)
        assert content.overlaps(spans, 109.0, 200.0)
        assert not content.overlaps(spans, 110.0, 200.0)

    def test_adjacent_refusals_merge_into_one_span(self) -> None:
        states = content.read(
            detections=[_Read("MISSION FAILED", 100.0), _Read("EXIT TO MENU", 104.0)],
            profile=GENERIC_PROFILE,
        )

        assert len(content.excluded_spans(states)) == 1


class TestOcrNoise:
    def test_welcome_survives_the_zero_for_o_confusion(self) -> None:
        # Measured, not imagined: the OCR read "Welcome t0" three times out of
        # four on this recording, and "Sapienza Ilaly" for Italy. Engines
        # confuse O with zero, and a rule that cannot survive it reads one
        # frame in four.
        for text in ("Welcome to Sapienza,47.", "Welcome t0 Sapienza,47."):
            states = content.read(
                detections=[_Read(text, 247.25)], profile=GENERIC_PROFILE
            )
            assert states, f"the intro went unread as {text!r}"
            assert states[0].state is ContentState.GAME_INTRO

class TestBridgingUnsampledGaps:
    """The leak the first render still carried, and why it is architectural."""

    def test_the_exact_island_that_leaked_three_frames_of_loading(self) -> None:
        # The real numbers, from the render that carried it. The OCR read the
        # menu at source 231.50 and the mission's title card at 247.25, which
        # left 241.50-243.25 -- 1.75 seconds nobody sampled -- looking like
        # gameplay. Three frames of a loading screen played there.
        #
        # This is the regression, kept in the units the recording used so the
        # next reader can find it in the file.
        states = content.read(
            detections=[
                _Read("MISSIONFAILED", 231.5),
                _Read("AGENT DOWN:", 231.5),
                _Read("EXIT TO MENU", 231.5),
                _Read("Welcome t0 Sapienza,47.", 247.25),
            ],
            profile=GENERIC_PROFILE,
        )

        spans = content.excluded_spans(states, observed_at=[231.5, 237.47, 247.25])

        assert len(spans) == 1, "the island between the menu and the intro survived"
        start, end = spans[0]
        assert start <= 232.9 and end >= 243.25, (
            "the stretch that carried the loading screen is still selectable"
        )

    def test_an_unsampled_island_between_two_refusals_is_closed(self) -> None:
        # Three frames of a loading screen survived the first build of this
        # layer, in a 1.75-second island between a menu read at 3:51.50 and a
        # title card read at 4:07.25. Nothing was sampled inside it: it was
        # called gameplay because nobody looked, not because anybody saw a
        # game. A second of play between a menu and a briefing is not a thing
        # that happens.
        states = content.read(
            detections=[_Read("MISSION FAILED", 100.0), _Read("WELCOME TO X", 118.0)],
            profile=GENERIC_PROFILE,
        )

        assert len(content.excluded_spans(states, observed_at=[100.0, 118.0])) == 1

    def test_a_gap_a_detector_looked_into_stands(self) -> None:
        # The guard `frame_state` already uses, for the reason it already
        # gives: bridging across stretches nobody watched declared footage a
        # menu that nobody had seen to be one.
        states = content.read(
            detections=[_Read("MISSION FAILED", 100.0), _Read("WELCOME TO X", 118.0)],
            profile=GENERIC_PROFILE,
        )

        spans = content.excluded_spans(states, observed_at=[100.0, 112.0, 118.0])

        assert len(spans) == 2, "a gap somebody watched was closed anyway"

    def test_a_wide_gap_is_never_bridged(self) -> None:
        states = content.read(
            detections=[_Read("MISSION FAILED", 100.0), _Read("WELCOME TO X", 200.0)],
            profile=GENERIC_PROFILE,
        )

        assert len(content.excluded_spans(states, observed_at=[])) == 2



class TestConfigurationIsNotSwallowed:
    """P0.2.1: the workers' catch-alls let a configuration error through.

    Both workers wrap the profile lookup in ``except Exception`` so a store
    that will not answer cannot stop the stage (§95). A missing profiles
    directory is not a store declining to answer: every game would silently
    become generic, and the feature would look like it was working.
    """

    @staticmethod
    def _context(tmp_path, game_profile: str | None):
        from types import SimpleNamespace

        row = {"game_profile": game_profile} if game_profile else None
        database = SimpleNamespace(fetch_one=lambda *args, **kwargs: row)
        return SimpleNamespace(database=database, profiles_dir=tmp_path / "not-there")

    def test_the_edl_worker_raises_when_the_directory_is_missing(self, tmp_path) -> None:
        from backend.core.errors import ConfigurationError
        from backend.pipeline.workers.edl_worker import EdlWorker

        with pytest.raises(ConfigurationError, match="profiles directory is missing"):
            EdlWorker()._profile(self._context(tmp_path, "hitman"), "media-1")

    def test_the_edl_worker_stays_generic_when_the_recording_has_no_ocr(
        self, tmp_path
    ) -> None:
        # No OCR row means no game name, and the directory is never consulted:
        # a recording without text is the ordinary case, not an install error.
        from backend.pipeline.workers.edl_worker import EdlWorker

        assert EdlWorker()._profile(self._context(tmp_path, None), "media-1") is GENERIC_PROFILE

    def test_the_moments_worker_raises_when_the_directory_is_missing(self, tmp_path) -> None:
        from types import SimpleNamespace

        from backend.core.errors import ConfigurationError
        from backend.pipeline.workers.moments_worker import MomentsWorker

        media = SimpleNamespace(id="media-1")
        with pytest.raises(ConfigurationError, match="profiles directory is missing"):
            MomentsWorker()._excluded_spans(
                self._context(tmp_path, "hitman"), media, vision=[], duration=60.0
            )


class TestWordsThatMeanNothingAlone:
    """P0.2.2: a pause menu's tabs, judged as a set on one frame.

    HITMAN's pause screen never says PAUSED. It says OBJECTIVES, MAP, MISSION
    STORIES, INTEL and INVENTORY, and INVENTORY alone is Grounded's hotbar
    label 79 times in the stored reads. The rule is a conjunction: three
    distinct tab words on the same frame, which on this machine has been a
    menu every one of 45 times.
    """

    def test_three_tab_words_on_one_frame_are_a_pause_menu(self) -> None:
        states = content.read(
            detections=[
                _Read("OBJECTIVES", 525.0),
                _Read("MAP", 525.0),
                _Read("INTEL", 525.0),
                _Read("INVENTORY", 525.0),
            ],
            profile=GENERIC_PROFILE,
        )
        assert [s.state for s in states] == [ContentState.PAUSE]
        assert states[0].covers(524.0, 526.0)
        assert states[0].excludes

    def test_one_tab_word_alone_is_not(self) -> None:
        # The hotbar.
        assert (
            content.read(detections=[_Read("INVENTORY", 100.0)], profile=GENERIC_PROFILE)
            == []
        )

    def test_two_are_still_not_enough(self) -> None:
        # MAP and INVENTORY are button prompts on many HUDs.
        assert (
            content.read(
                detections=[_Read("MAP", 100.0), _Read("INVENTORY", 100.0)],
                profile=GENERIC_PROFILE,
            )
            == []
        )

    def test_the_words_have_to_share_a_frame(self) -> None:
        # Three tab words across three sampled frames are three prompts, not
        # a menu: the conjunction is per screen.
        assert (
            content.read(
                detections=[
                    _Read("OBJECTIVES", 100.0),
                    _Read("MAP", 107.0),
                    _Read("INVENTORY", 114.0),
                ],
                profile=GENERIC_PROFILE,
            )
            == []
        )

    def test_the_same_word_three_times_is_one_match(self) -> None:
        assert (
            content.read(
                detections=[_Read("SAVE", 100.0), _Read("SAVE", 100.0), _Read("Save", 100.0)],
                profile=GENERIC_PROFILE,
            )
            == []
        )

    def test_pause_menu_is_pause_wording_too(self) -> None:
        # HITMAN's ESC screen is titled PAUSE MENU and never says PAUSED.
        states = content.read(detections=[_Read("PAUSE MENU", 2352.0)], profile=GENERIC_PROFILE)
        assert [s.state for s in states] == [ContentState.PAUSE]

    def test_a_profile_may_declare_a_conjunction_of_its_own(self) -> None:
        profile = GameProfile(
            id="tabbed",
            content_rules=(
                ProfileContentRule(
                    state="menu",
                    name="own_tabs",
                    patterns=(r"^\s*ALPHA\s*$", r"^\s*BETA\s*$"),
                    min_matches=2,
                ),
            ),
        )
        alone = content.read(detections=[_Read("ALPHA", 10.0)], profile=profile)
        both = content.read(
            detections=[_Read("ALPHA", 10.0), _Read("BETA", 10.0)], profile=profile
        )
        assert alone == []
        assert [s.state for s in both] == [ContentState.MENU]
