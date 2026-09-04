"""Gaming intelligence without FFmpeg or models (Phase 5, SPEC §21-§27).

The two claims worth testing here are the ones the spec makes hardest:

* **§23** — the application must not require a game profile. Every detector
  runs on the generic profile, and a profile only improves what comes out.
* **§27** — detectors that agree become *one* event with higher confidence,
  never several events. Three records of one explosion make it look like three
  explosions to everything downstream.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ai.ocr.fake_provider import FakeOcrProvider
from ai.providers.base import TextDetection, VisionObservation
from backend.analysis import frame_state
from backend.analysis.audio_events import GAMEPLAY, MICROPHONE, AudioEvent
from backend.analysis.reactions import ReactionCandidate
from backend.analysis.scenes import Scene
from backend.core.errors import ConfigurationError
from backend.core.models.enums import AudioEventType, GameEventType, ReactionType
from backend.database.repositories.vision import StoredObservation
from backend.gaming import events as detectors
from backend.gaming.correlation import GENERIC_TYPES, correlate
from backend.gaming.ocr import FULL_FRAME, FrameText
from backend.gaming.profiles import (
    GENERIC_PROFILE,
    EventRule,
    GameProfile,
    Region,
    available_profiles,
    clear_profile_cache,
    load_profile,
)

pytestmark = pytest.mark.unit


def _observation(timestamp: float, labels: tuple[str, ...], confidence: float = 0.8):
    return StoredObservation(
        observation=VisionObservation(
            timestamp=timestamp, description="x", labels=labels, confidence=confidence
        ),
        region_start=None,
        region_end=None,
        sources=(),
        model_name="fake",
        model_version="1",
        prompt_id=None,
        prompt_version=None,
    )


def _text(timestamp: float, text: str, *, region: str | None = None, confidence: float = 0.9):
    return FrameText(
        timestamp=timestamp,
        frame_path=Path(),
        detections=(
            TextDetection(text=text, confidence=confidence, timestamp=timestamp, region=region),
        ),
    )


class TestProfiles:
    """§23: a profile is additive, and never required."""

    def test_an_unknown_game_gets_the_generic_profile(self, tmp_path: Path) -> None:
        resolution = load_profile("some-game-nobody-wrote", tmp_path)
        assert resolution.profile is GENERIC_PROFILE
        assert resolution.exact is False
        assert resolution.profile.is_generic

    def test_auto_and_blank_mean_no_specific_game(self, tmp_path: Path) -> None:
        for value in ("auto", "", "  ", "generic", "unknown"):
            assert load_profile(value, tmp_path).profile.is_generic

    def test_the_shipped_generic_profile_declares_nothing(self) -> None:
        assert GENERIC_PROFILE.is_generic
        assert not GENERIC_PROFILE.has_ocr_regions
        assert GENERIC_PROFILE.event_rules == ()

    def test_a_real_profile_loads(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "testgame")
        clear_profile_cache()
        resolution = load_profile("testgame", tmp_path)
        assert resolution.exact is True
        assert not resolution.profile.is_generic
        assert resolution.profile.has_ocr_regions
        assert "kill_feed" in resolution.profile.reading_regions()

    def test_a_missing_profiles_directory_is_a_configuration_error(
        self, tmp_path: Path
    ) -> None:
        # P0.2.1. "No profile for this game" and "no profiles directory" read
        # the same from outside -- both end in the generic table -- and are
        # not the same thing. The second is a broken install, and it was
        # swallowed by a catch-all until a log line gave it away.
        with pytest.raises(ConfigurationError, match="profiles directory is missing"):
            load_profile("hitman", tmp_path / "not-there")

    def test_the_generic_names_never_need_the_directory(self, tmp_path: Path) -> None:
        # The unspecified names resolve before the directory is looked at: a
        # recording with no OCR must stay generic and quiet, not become an
        # install error.
        for value in ("auto", "", "generic"):
            assert load_profile(value, tmp_path / "not-there").profile.is_generic

    def test_a_malformed_profile_fails_loudly(self, tmp_path: Path) -> None:
        # A broken profile that silently became generic would look like the
        # feature working.
        directory = tmp_path / "broken"
        directory.mkdir()
        (directory / "profile.json").write_text("{not json", encoding="utf-8")
        clear_profile_cache()
        with pytest.raises(ConfigurationError):
            load_profile("broken", tmp_path)

    def test_ocr_regions_must_exist(self, tmp_path: Path) -> None:
        directory = tmp_path / "bad"
        directory.mkdir()
        (directory / "profile.json").write_text(
            json.dumps({"id": "bad", "ocr_regions": ["nowhere"]}), encoding="utf-8"
        )
        clear_profile_cache()
        with pytest.raises(ConfigurationError, match="does not define"):
            load_profile("bad", tmp_path)

    def test_regions_are_fractions_so_resolution_does_not_matter(self) -> None:
        # A profile written against 1080p must keep working on the 720p proxy.
        region = Region(x=0.5, y=0.0, width=0.5, height=0.25)
        assert region.to_pixels(1280, 720) == (640, 0, 1280, 180)
        assert region.to_pixels(1920, 1080) == (960, 0, 1920, 270)

    def test_a_region_cannot_leave_the_frame(self) -> None:
        with pytest.raises(ValueError, match="fit inside the frame"):
            Region(x=0.8, y=0.0, width=0.5, height=0.5)

    def test_ignore_patterns_filter_interface_furniture(self) -> None:
        profile = GameProfile(id="p", ignore_patterns=(r"^press\s", r"subscribe"))
        assert profile.should_ignore("Press F to interact")
        assert profile.should_ignore("  ")
        assert not profile.should_ignore("ELIMINATED")

    def test_a_rule_can_be_restricted_to_a_region(self) -> None:
        rule = EventRule(
            event_type=GameEventType.KILL, patterns=(r"eliminated",), regions=("kill_feed",)
        )
        assert rule.matches("ELIMINATED", region="kill_feed")
        assert not rule.matches("ELIMINATED", region="chat")
        assert not rule.matches("ELIMINATED")

    def test_available_profiles_always_includes_generic(self, tmp_path: Path) -> None:
        assert available_profiles(tmp_path) == ("generic",)
        _write_profile(tmp_path, "testgame")
        assert set(available_profiles(tmp_path)) == {"generic", "testgame"}


class TestDetectorsWithoutAProfile:
    """§23's acceptance, at the unit level: every detector works generic."""

    def test_vision_labels_become_events(self) -> None:
        found = detectors.observations_from_vision(
            [_observation(10.0, ("victory_screen",)), _observation(20.0, ("low_health",))]
        )
        assert [item.event_type for item in found] == [
            GameEventType.VICTORY,
            GameEventType.LOW_HEALTH,
        ]

    def test_an_unmapped_label_claims_nothing(self) -> None:
        # A label the taxonomy has no event for must not become one. It now
        # travels as context so Phase 0.2 can read it beside a signal -- but
        # it claims nothing on its own and correlation must never promote it.
        found = detectors.observations_from_vision([_observation(5.0, ("driving",))])

        assert [item.event_type for item in found] == [GameEventType.UNKNOWN_EVENT]
        assert all(item.context_only for item in found)
        assert correlate(found) == [], "a description of the screen is not an event"

    def test_a_screen_state_label_is_not_even_context(self) -> None:
        # `menu` and `loading` are frame_state's business. Letting a menu
        # corroborate an event would be the opposite of what it means.
        assert detectors.observations_from_vision([_observation(5.0, ("menu",))]) == []

    def test_prose_is_never_parsed_for_decisions(self) -> None:
        # §93: pipeline decisions never come from uncontrolled prose.
        rich = StoredObservation(
            observation=VisionObservation(
                timestamp=1.0,
                description="The player gets an incredible triple kill and wins the round",
                labels=(),
                confidence=0.95,
            ),
            region_start=None,
            region_end=None,
            sources=(),
            model_name="m",
            model_version="1",
            prompt_id=None,
            prompt_version=None,
        )
        assert detectors.observations_from_vision([rich]) == []

    def test_generic_text_patterns_work_without_a_profile(self) -> None:
        found = detectors.observations_from_ocr(
            [_text(12.0, "VICTORY", region=FULL_FRAME), _text(30.0, "ELIMINATED")],
            GENERIC_PROFILE,
        )
        assert {item.event_type for item in found} == {
            GameEventType.VICTORY,
            GameEventType.KILL,
        }
        assert all(item.source == detectors.OCR for item in found)

    def test_a_profile_rule_outranks_the_generic_pattern(self) -> None:
        profile = GameProfile(
            id="testgame",
            regions={"kill_feed": Region(x=0.7, y=0.1, width=0.3, height=0.2)},
            ocr_regions=("kill_feed",),
            event_rules=(
                EventRule(
                    event_type=GameEventType.MULTI_KILL,
                    patterns=(r"double\s+kill",),
                    regions=("kill_feed",),
                    confidence=0.9,
                ),
            ),
        )
        found = detectors.observations_from_ocr(
            [_text(50.0, "DOUBLE KILL", region="kill_feed")], profile
        )
        assert [item.event_type for item in found] == [GameEventType.MULTI_KILL]
        assert found[0].source == detectors.PROFILE
        assert found[0].confidence > 0.7

    def test_audio_never_names_what_it_heard(self) -> None:
        # A loud transient is a fact about the waveform. Calling it a gunshot
        # would be a claim about the game.
        found = detectors.observations_from_audio(
            [
                AudioEvent(
                    event_type=AudioEventType.TRANSIENT,
                    start_seconds=5.0,
                    end_seconds=5.4,
                    confidence=0.9,
                    track_role=GAMEPLAY,
                )
            ]
        )
        assert [item.event_type for item in found] == [GameEventType.UNKNOWN_EVENT]
        assert found[0].source == detectors.AUDIO

    def test_microphone_audio_is_a_distinct_source(self) -> None:
        found = detectors.observations_from_audio(
            [
                AudioEvent(
                    event_type=AudioEventType.SPIKE,
                    start_seconds=5.0,
                    end_seconds=5.4,
                    track_role=MICROPHONE,
                )
            ]
        )
        assert found[0].source == detectors.MICROPHONE_SOURCE

    def test_a_laugh_names_a_moment_without_knowing_the_game(self) -> None:
        found = detectors.observations_from_reactions(
            [
                ReactionCandidate(
                    reaction_type=ReactionType.LAUGH,
                    start_seconds=100.0,
                    end_seconds=102.0,
                    confidence=0.8,
                    intensity_db=12.0,
                )
            ]
        )
        assert [item.event_type for item in found] == [GameEventType.FUNNY_MOMENT]

    def test_scene_changes_are_weak_on_purpose(self) -> None:
        # §17: boundaries are supporting information. A scene change alone must
        # never become an event.
        found = detectors.observations_from_scenes(
            [
                Scene(index=0, start_seconds=0.0, end_seconds=10.0),
                Scene(index=1, start_seconds=10.0, end_seconds=20.0, change_score=50.0),
            ],
            min_change=27.0,
        )
        assert len(found) == 1
        assert found[0].confidence < 0.3

    def test_silence_and_speech_are_not_events(self) -> None:
        assert (
            detectors.observations_from_audio(
                [
                    AudioEvent(
                        event_type=AudioEventType.SILENCE,
                        start_seconds=0.0,
                        end_seconds=10.0,
                    ),
                    AudioEvent(
                        event_type=AudioEventType.SPEECH,
                        start_seconds=0.0,
                        end_seconds=10.0,
                    ),
                ]
            )
            == []
        )


class TestCorrelation:
    """§27: agreeing detectors become one event, not several."""

    @staticmethod
    def _observation(event_type, at, source, confidence=0.7, duration=1.0):
        return detectors.EventObservation(
            event_type=event_type,
            start_seconds=at,
            end_seconds=at + duration,
            source=source,
            confidence=confidence,
        )

    def test_an_unnamed_instant_on_a_menu_is_not_an_event(self) -> None:
        """§77's accidental menu, caught a stage earlier.

        Measured across ten real projects: of 389 events nobody could name,
        104 had a frame within two seconds of them, and ``menu``, ``inventory``
        and ``loading`` outnumbered every gameplay label those frames reported,
        together. A scene boundary and an audio spike are exactly what opening
        a menu produces.
        """
        screens = frame_state.spans(
            [
                _observation(99.0, ("menu",)),
                _observation(101.0, ("menu",)),
            ]
        )
        events = correlate(
            [
                self._observation(GameEventType.UNKNOWN_EVENT, 100.0, detectors.SCENE),
                self._observation(GameEventType.UNKNOWN_EVENT, 100.2, detectors.AUDIO),
            ],
            screen_states=screens,
        )
        assert events == []

    def test_a_named_event_keeps_its_name_wherever_it_was_read(self) -> None:
        # `defeat` is read off a defeat screen. A rule that dropped events on
        # screens would delete the clearest evidence this pipeline has.
        screens = frame_state.spans([_observation(99.0, ("menu",)), _observation(101.0, ("menu",))])
        events = correlate(
            [self._observation(GameEventType.DEFEAT, 100.0, detectors.OCR)],
            screen_states=screens,
        )
        assert [event.event_type for event in events] == [GameEventType.DEFEAT]

    def test_a_hud_reading_is_not_a_screen(self) -> None:
        # The vision model calls a health bar over a firefight `inventory` --
        # 181 observations on one real recording, more than any other label.
        # Treating that as a menu would delete ordinary gameplay.
        screens = frame_state.spans(
            [_observation(99.0, ("inventory",)), _observation(101.0, ("inventory",))]
        )
        events = correlate(
            [
                self._observation(GameEventType.UNKNOWN_EVENT, 100.0, detectors.SCENE),
                self._observation(GameEventType.UNKNOWN_EVENT, 100.2, detectors.AUDIO),
            ],
            screen_states=screens,
        )
        assert len(events) == 1

    def test_without_screen_states_nothing_is_dropped(self) -> None:
        # A project analysed before this existed, or one where vision found
        # nothing, keeps every event it had.
        events = correlate(
            [self._observation(GameEventType.UNKNOWN_EVENT, 100.0, detectors.SCENE)]
        )
        assert len(events) == 1

    def test_the_spec_example_becomes_one_event(self) -> None:
        # "Kill-feed change + weapon sound + NO WAY becomes one high-confidence
        # gameplay moment."
        events = correlate(
            [
                self._observation(GameEventType.KILL, 100.0, detectors.OCR, 0.6),
                self._observation(GameEventType.UNKNOWN_EVENT, 100.3, detectors.AUDIO, 0.8, 0.5),
                self._observation(
                    GameEventType.UNKNOWN_EVENT, 101.2, detectors.MICROPHONE_SOURCE, 0.7
                ),
            ]
        )
        assert len(events) == 1
        event = events[0]
        assert event.event_type is GameEventType.KILL
        assert set(event.sources) == {"ocr", "audio", "microphone"}
        # High confidence, as §27 asks -- higher than any single detector had.
        assert event.confidence > 0.75

    def test_agreement_raises_confidence(self) -> None:
        alone = correlate([self._observation(GameEventType.KILL, 10.0, detectors.OCR, 0.6)])
        together = correlate(
            [
                self._observation(GameEventType.KILL, 10.0, detectors.OCR, 0.6),
                self._observation(GameEventType.KILL, 10.4, detectors.VISION, 0.6),
            ]
        )
        assert together[0].confidence > alone[0].confidence

    def test_confidence_never_reaches_certainty(self) -> None:
        # No amount of agreement between inferring detectors makes a fact.
        events = correlate(
            [
                self._observation(GameEventType.KILL, 10.0, source, 0.95)
                for source in ("ocr", "vision", "audio", "microphone", "scene", "profile")
            ]
        )
        assert events[0].confidence < 1.0

    def test_repeated_observations_from_one_source_do_not_count_as_agreement(self) -> None:
        # Three OCR lines off one frame are one detector, not three.
        one_source = correlate(
            [self._observation(GameEventType.KILL, 10.0, detectors.OCR, 0.6) for _ in range(3)]
        )
        two_sources = correlate(
            [
                self._observation(GameEventType.KILL, 10.0, detectors.OCR, 0.6),
                self._observation(GameEventType.KILL, 10.1, detectors.VISION, 0.6),
            ]
        )
        assert one_source[0].confidence < two_sources[0].confidence

    def test_a_specific_type_beats_a_generic_one(self) -> None:
        events = correlate(
            [
                self._observation(GameEventType.UNKNOWN_EVENT, 10.0, detectors.AUDIO, 0.95),
                self._observation(GameEventType.VICTORY, 10.2, detectors.OCR, 0.5),
            ]
        )
        # Audio was more confident, but only OCR could know what it was.
        assert events[0].event_type is GameEventType.VICTORY

    def test_distant_events_stay_separate(self) -> None:
        events = correlate(
            [
                self._observation(GameEventType.KILL, 10.0, detectors.OCR),
                self._observation(GameEventType.KILL, 500.0, detectors.OCR),
            ]
        )
        assert len(events) == 2

    def test_a_chain_of_close_observations_stays_one_event(self) -> None:
        # Clustering against the group's span, not its first member: otherwise
        # a run of observations a second apart fragments into several events.
        events = correlate(
            [
                self._observation(GameEventType.UNKNOWN_EVENT, at, detectors.AUDIO, 0.7, 0.5)
                for at in (10.0, 11.0, 12.0, 13.0, 14.0)
            ]
        )
        assert len(events) == 1
        assert events[0].duration >= 4.0

    def test_the_event_span_covers_every_contributor(self) -> None:
        events = correlate(
            [
                self._observation(GameEventType.KILL, 100.0, detectors.OCR, 0.6, 1.0),
                self._observation(GameEventType.UNKNOWN_EVENT, 101.5, detectors.AUDIO, 0.6, 2.0),
            ]
        )
        assert events[0].start_seconds == 100.0
        assert events[0].end_seconds == 103.5

    def test_every_event_records_which_profile_was_in_force(self) -> None:
        # §49: "detected with the generic profile" is a different claim.
        events = correlate(
            [self._observation(GameEventType.KILL, 10.0, detectors.OCR)],
            game_profile="generic",
        )
        assert events[0].game_profile == "generic"

    def test_the_confidence_floor_drops_noise(self) -> None:
        events = correlate(
            [self._observation(GameEventType.UNKNOWN_EVENT, 10.0, detectors.SCENE, 0.1)],
            min_confidence=0.5,
        )
        assert events == []

    def test_generic_events_are_marked_as_unnamed(self) -> None:
        events = correlate(
            [self._observation(GameEventType.UNKNOWN_EVENT, 10.0, detectors.AUDIO, 0.8)]
        )
        assert not events[0].is_named
        assert events[0].event_type in GENERIC_TYPES

    def test_the_event_shape_matches_the_spec(self) -> None:
        # §26's schema.
        payload = correlate([self._observation(GameEventType.CLUTCH, 812.4, detectors.OCR, 0.9)])[
            0
        ].as_dict()
        assert set(payload) >= {
            "type",
            "start",
            "end",
            "confidence",
            "importance",
            "sources",
            "metadata",
        }
        assert 0.0 <= payload["confidence"] <= 1.0
        assert 0.0 <= payload["importance"] <= 1.0

    def test_nothing_in_produces_nothing_out(self) -> None:
        assert correlate([]) == []


class TestQuestTrackersAreNotEvents:
    """A quest tracker is an instruction, not a result.

    ``\\bdefeat\\b`` read *"Defeat the O.R.C. guards at the Milk Molar stash"*
    as a defeat nineteen times in one recording — the most common named event
    in the whole project, and every one of them the objective list sitting on
    screen. Both words are imperative verbs a quest tracker uses constantly;
    only a banner says them alone.
    """

    def test_an_objective_is_not_a_defeat(self) -> None:
        found = detectors.observations_from_ocr(
            [_text(10.0, "Defeat the O.RC guards at the Milk Molar stash")],
            GENERIC_PROFILE,
        )

        assert found == []

    def test_a_banner_still_reads_as_one(self) -> None:
        found = detectors.observations_from_ocr([_text(10.0, "DEFEAT")], GENERIC_PROFILE)

        assert [item.event_type for item in found] == [GameEventType.DEFEAT]

    def test_a_sentence_about_dying_still_reads(self) -> None:
        # Anchoring the bare word must not lose the phrasings that are
        # unambiguous however much text surrounds them.
        found = detectors.observations_from_ocr(
            [_text(10.0, "You died. Respawn at the nearest outpost.")], GENERIC_PROFILE
        )

        assert [item.event_type for item in found] == [GameEventType.DEFEAT]

    def test_victory_conditions_are_not_victories(self) -> None:
        found = detectors.observations_from_ocr(
            [_text(10.0, "Victory requires defeating all three bosses")], GENERIC_PROFILE
        )

        assert found == []


class TestRecognisingTheGame:
    """Phase 0.3: ``detected_game`` was a column nothing ever wrote.

    Every real project carries ``game: auto``, so the profile on disk — with
    its death-screen wording and its HUD indicator — had never once been
    loaded. A profile nobody selects is a profile that does not exist.
    """

    @staticmethod
    def _profile(root: Path, game: str, signatures: list[str]) -> None:
        directory = root / game
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "profile.json").write_text(
            json.dumps({"id": game, "signature_patterns": signatures}), encoding="utf-8"
        )

    def test_a_game_is_recognised_from_what_the_screen_says(self, tmp_path) -> None:
        from backend.gaming.detection import detect_game

        clear_profile_cache()
        self._profile(tmp_path, "grounded", [r"\bMilk Molar\b", r"\bLean-To\b", r"\bMUTATIONS\b"])

        guess = detect_game(
            ["Set your respawn point at your Lean-To", "MUTATIONS", "Milk Molar stash"],
            tmp_path,
        )

        assert guess.game == "grounded"
        assert guess.recognised

    def test_one_shared_word_recognises_nothing(self, tmp_path) -> None:
        # "Craft" and "Analyze" are vocabulary half the genre uses. Claiming a
        # game from one of them would read another game's screen with this
        # game's rules.
        from backend.gaming.detection import detect_game

        clear_profile_cache()
        self._profile(tmp_path, "grounded", [r"\bMilk Molar\b", r"\bLean-To\b", r"\bMUTATIONS\b"])

        guess = detect_game(["Craft", "Analyze", "Milk Molar"], tmp_path)

        assert guess.game is None
        assert guess.hits == 1

    def test_two_close_profiles_recognise_nothing(self, tmp_path) -> None:
        # Games that share wording is exactly when a guess does damage.
        from backend.gaming.detection import detect_game

        clear_profile_cache()
        self._profile(tmp_path, "one", [r"\balpha\b", r"\bbeta\b", r"\bgamma\b"])
        self._profile(tmp_path, "two", [r"\balpha\b", r"\bbeta\b", r"\bdelta\b"])

        guess = detect_game(["alpha", "beta", "gamma", "delta"], tmp_path)

        assert guess.game is None

    def test_no_text_recognises_nothing(self, tmp_path) -> None:
        from backend.gaming.detection import detect_game

        clear_profile_cache()
        self._profile(tmp_path, "grounded", [r"\bMilk Molar\b"])

        assert detect_game([], tmp_path).game is None

    def test_a_signature_held_on_screen_counts_once(self, tmp_path) -> None:
        # A quest tracker holds one recognisable word for four minutes. That
        # is one piece of evidence about which game this is, not two hundred.
        from backend.gaming.detection import detect_game

        clear_profile_cache()
        self._profile(tmp_path, "grounded", [r"\bMilk Molar\b", r"\bLean-To\b", r"\bMUTATIONS\b"])

        guess = detect_game(["Milk Molar"] * 200, tmp_path)

        assert guess.hits == 1
        assert guess.game is None


class TestTheShippedGroundedProfile:
    """§111: a profile is written from footage, not from documentation.

    Every pattern in it appeared in the OCR of two real recordings this
    project analysed.
    """

    @staticmethod
    def _profile():
        from backend.config.paths import find_repository_root

        clear_profile_cache()
        return load_profile("grounded", find_repository_root() / "profiles").profile

    def test_it_loads_and_is_not_generic(self) -> None:
        profile = self._profile()

        assert not profile.is_generic or profile.event_rules
        assert profile.signature_patterns

    def test_the_death_screen_is_a_death(self) -> None:
        found = detectors.observations_from_ocr([_text(10.0, "DEATH")], self._profile())

        assert GameEventType.DEATH in {item.event_type for item in found}

    def test_the_objective_list_is_ignored_outright(self) -> None:
        profile = self._profile()

        assert profile.should_ignore("Defeat the O.RC guards at the Milk Molar stash")
        assert profile.should_ignore("Set your respawn point at your Lean-To:")
        assert not profile.should_ignore("DEATH")

    def test_its_fusion_rules_convert(self) -> None:
        rules = self._profile().fusion()

        assert rules
        assert all(rule.name.startswith("grounded:") for rule in rules)


class TestEvidenceFusion:
    """Phase 0.2: naming an instant no single detector could name.

    Measured before this existed: 61% and 70% of correlated events on two real
    recordings were ``unknown_event``, and 63 of one recording's 116 events
    were ``["audio", "scene"]`` clusters — a waveform transient beside a shot
    change, neither of which may claim anything on its own, and correctly so.
    The same transient *while the vision model reports ``combat``* is something
    a person names without hesitating.
    """

    @staticmethod
    def _observation(at, source, confidence=0.7, event_type=None, **detail):
        return detectors.EventObservation(
            event_type=event_type or GameEventType.UNKNOWN_EVENT,
            start_seconds=at,
            end_seconds=at + 0.5,
            source=source,
            confidence=confidence,
            detail=detail,
        )

    def test_combat_seen_and_heard_is_named(self) -> None:
        events = correlate(
            [
                self._observation(100.0, detectors.AUDIO, 0.8),
                self._observation(100.3, detectors.SCENE, 0.25),
                self._observation(100.1, detectors.VISION, 0.7, label="combat"),
            ]
        )

        assert len(events) == 1
        assert events[0].event_type is GameEventType.COMBAT
        assert events[0].metadata["named_by"] == "fusion:combat_seen_and_heard"

    def test_a_spike_beside_a_shot_change_stays_unnamed(self) -> None:
        # The honest answer, and still a common one. Nothing looked at the
        # screen, so nothing may say what was on it.
        events = correlate(
            [
                self._observation(200.0, detectors.AUDIO, 0.8),
                self._observation(200.4, detectors.SCENE, 0.25),
            ]
        )

        assert events[0].event_type is GameEventType.UNKNOWN_EVENT
        assert "named_by" not in events[0].metadata

    def test_a_detector_that_could_see_is_never_overridden(self) -> None:
        # A victory banner read off the screen outranks an inference from a
        # label and a spike, however much evidence the inference has.
        events = correlate(
            [
                self._observation(300.0, detectors.OCR, 0.7, event_type=GameEventType.VICTORY),
                self._observation(300.2, detectors.AUDIO, 0.9),
                self._observation(300.1, detectors.VISION, 0.9, label="combat"),
            ]
        )

        assert events[0].event_type is GameEventType.VICTORY
        assert "named_by" not in events[0].metadata

    def test_a_rule_records_what_it_read(self) -> None:
        # §21: an event nobody detected has to explain itself from the row.
        events = correlate(
            [
                self._observation(400.0, detectors.AUDIO, 0.8),
                self._observation(400.2, detectors.SCENE, 0.3),
                self._observation(400.1, detectors.VISION, 0.75, label="driving"),
            ]
        )

        assert events[0].event_type is GameEventType.COLLISION
        assert events[0].metadata["fusion_evidence"] == {"driving": 0.75}

    def test_an_uncertain_label_does_not_name_anything(self) -> None:
        # A label the model reported at 0.2 is the model saying it does not
        # know; naming a collision from it would be inventing evidence.
        events = correlate(
            [
                self._observation(500.0, detectors.AUDIO, 0.8),
                self._observation(500.2, detectors.SCENE, 0.3),
                self._observation(500.1, detectors.VISION, 0.2, label="driving"),
            ]
        )

        assert events[0].event_type is GameEventType.UNKNOWN_EVENT

    def test_a_missing_source_does_not_name_anything(self) -> None:
        # Driving and a shot change with nothing heard is a camera cut in a
        # car, which happens constantly in an open-world recording.
        events = correlate(
            [
                self._observation(600.2, detectors.SCENE, 0.3),
                self._observation(600.1, detectors.VISION, 0.8, label="driving"),
            ]
        )

        assert events[0].event_type is GameEventType.UNKNOWN_EVENT

    def test_rules_can_be_turned_off_entirely(self) -> None:
        events = correlate(
            [
                self._observation(700.0, detectors.AUDIO, 0.8),
                self._observation(700.1, detectors.VISION, 0.7, label="combat"),
            ],
            fusion_rules=(),
        )

        assert events[0].event_type is GameEventType.UNKNOWN_EVENT


class TestEndToEndDetection:
    """§23's headline claim, exercised through the whole detector chain."""

    def test_events_are_detected_with_no_profile_at_all(self) -> None:
        observations = detectors.detect(
            vision=[_observation(100.0, ("victory_screen",), 0.85)],
            ocr_frames=[_text(100.5, "VICTORY", region=FULL_FRAME)],
            audio=[
                AudioEvent(
                    event_type=AudioEventType.SPIKE,
                    start_seconds=100.2,
                    end_seconds=100.9,
                    confidence=0.8,
                )
            ],
            profile=GENERIC_PROFILE,
        )
        events = correlate(observations, game_profile="generic")

        assert len(events) == 1
        assert events[0].event_type is GameEventType.VICTORY
        assert events[0].is_named
        assert set(events[0].sources) == {"vision", "ocr", "audio"}
        assert events[0].confidence > 0.85

    def test_a_profile_improves_the_result_it_does_not_enable_it(self) -> None:
        profile = GameProfile(
            id="testgame",
            regions={"banner": Region(x=0.25, y=0.4, width=0.5, height=0.2)},
            ocr_regions=("banner",),
            event_rules=(
                EventRule(
                    event_type=GameEventType.VICTORY,
                    patterns=(r"round\s+won",),
                    regions=("banner",),
                    confidence=0.95,
                ),
            ),
        )
        frames = [_text(100.0, "ROUND WON", region="banner")]

        without = correlate(
            detectors.detect(ocr_frames=frames, profile=GENERIC_PROFILE),
            game_profile="generic",
        )
        with_profile = correlate(
            detectors.detect(ocr_frames=frames, profile=profile), game_profile="testgame"
        )

        # Generic wording does not know "ROUND WON": nothing is claimed.
        assert without == []
        # The profile does, and says so with high confidence.
        assert len(with_profile) == 1
        assert with_profile[0].event_type is GameEventType.VICTORY
        assert with_profile[0].confidence > 0.8


class TestSuppressedGenericRules:
    @staticmethod
    def _profile(game):
        from backend.config.paths import find_repository_root

        clear_profile_cache()
        return load_profile(game, find_repository_root() / "profiles").profile

    def test_a_profile_can_veto_a_generic_rule_by_name(self) -> None:
        from backend.gaming.fusion import GENERIC_RULES

        names = [rule.name for rule in self._profile("grounded").rules_with(GENERIC_RULES)]

        assert "combat_seen_and_heard" not in names
        assert "driving_impact" not in names
        assert "visible_destruction" in names
        assert any(name.endswith("creature_fight") for name in names)

    def test_a_profile_that_vetoes_nothing_keeps_the_whole_table(self) -> None:
        from backend.gaming.fusion import GENERIC_RULES

        names = [rule.name for rule in self._profile("gta_v").rules_with(GENERIC_RULES)]

        assert {rule.name for rule in GENERIC_RULES} <= set(names)
        assert names.index("gta_v:burning_wreck") < names.index("gta_v:shootout")


class TestClusterDiscipline:
    """The 2.5-second window means an instant, and chaining must not unmean it.

    Measured on the Grounded golden window: audio transients every few seconds
    and a screen description with every analysed frame chained clusters of 59
    to 96 seconds, one narration observation named a whole minute "outplay",
    and the benchmark scored an invented event on footage a person marked
    boring.
    """

    @staticmethod
    def _claim(at, duration=1.0, source=detectors.AUDIO, confidence=0.6):
        return detectors.EventObservation(
            event_type=GameEventType.UNKNOWN_EVENT,
            start_seconds=at,
            end_seconds=at + duration,
            source=source,
            confidence=confidence,
        )

    @staticmethod
    def _context(at, duration=1.0, **detail):
        return detectors.EventObservation(
            event_type=GameEventType.UNKNOWN_EVENT,
            start_seconds=at,
            end_seconds=at + duration,
            source=detectors.VISION,
            confidence=0.9,
            context_only=True,
            detail=detail,
        )

    def test_a_chain_of_transients_cannot_become_a_minute(self) -> None:
        observations = [self._claim(100.0 + 3.0 * step) for step in range(20)]

        events = correlate(observations)

        assert len(events) >= 2
        for event in events:
            assert event.end_seconds - event.start_seconds <= 16.0

    def test_context_does_not_bridge_two_instants(self) -> None:
        # A claim, a description two seconds later, and a second claim close
        # to the description but far from the first claim. Before the frontier
        # rule the description glued them into one event.
        events = correlate(
            [
                self._claim(100.0),
                self._context(103.0, description="the player walks"),
                self._claim(105.5),
            ]
        )

        assert len(events) == 2

    def test_a_description_still_attaches_to_the_instant_beside_it(self) -> None:
        # Fusion reads descriptions alongside signals (§ Phase 0.2); the
        # frontier rule must not orphan them.
        events = correlate(
            [
                self._claim(100.0),
                self._context(100.4, description="a vehicle engulfed in flames"),
            ]
        )

        assert len(events) == 1
        assert events[0].event_type is GameEventType.HIGH_DAMAGE

    def test_low_health_read_by_vision_alone_is_not_an_event(self) -> None:
        # The vision model reads Grounded's always-on hunger dials as "low
        # health" while the player walks. One kind of sensor claiming a state
        # is context; with audio beside it, fusion may still name it.
        alone = correlate(
            [
                detectors.EventObservation(
                    event_type=GameEventType.LOW_HEALTH,
                    start_seconds=100.0,
                    end_seconds=101.0,
                    source=detectors.VISION,
                    confidence=0.8,
                    detail={"label": "low_health"},
                )
            ]
        )

        assert all(event.event_type is not GameEventType.LOW_HEALTH for event in alone)

    def test_low_health_with_audio_under_it_becomes_near_death(self) -> None:
        events = correlate(
            [
                detectors.EventObservation(
                    event_type=GameEventType.LOW_HEALTH,
                    start_seconds=100.0,
                    end_seconds=101.0,
                    source=detectors.VISION,
                    confidence=0.8,
                    detail={"label": "low_health"},
                ),
                self._claim(100.3, confidence=0.7),
            ]
        )

        assert len(events) == 1
        assert events[0].event_type is GameEventType.NEAR_DEATH

    def test_low_health_from_a_hud_reading_survives(self) -> None:
        events = correlate(
            [
                detectors.EventObservation(
                    event_type=GameEventType.LOW_HEALTH,
                    start_seconds=100.0,
                    end_seconds=101.0,
                    source=detectors.OCR,
                    confidence=0.8,
                )
            ]
        )

        assert len(events) == 1
        assert events[0].event_type is GameEventType.LOW_HEALTH


class TestDescriptionPatternRules:
    """Rules that read what the vision model wrote, not only how it labelled."""

    @staticmethod
    def _observation(at, source, confidence=0.7, event_type=None, **detail):
        return detectors.EventObservation(
            event_type=event_type or GameEventType.UNKNOWN_EVENT,
            start_seconds=at,
            end_seconds=at + 0.5,
            source=source,
            confidence=confidence,
            detail=detail,
        )

    def test_a_fire_in_the_prose_is_high_damage(self) -> None:
        # "engulfed in flames" was in the description at every fire the golden
        # set marked, while the label stayed `combat`.
        events = correlate(
            [
                self._observation(100.0, detectors.AUDIO, 0.6),
                self._observation(
                    100.2,
                    detectors.VISION,
                    0.9,
                    label="combat",
                    description="The vehicle is engulfed in flames beside the road.",
                ),
            ]
        )

        assert len(events) == 1
        assert events[0].event_type is GameEventType.HIGH_DAMAGE
        assert events[0].metadata["named_by"] == "fusion:visible_destruction"

    def test_a_label_quorum_refuses_a_single_sighting(self) -> None:
        from backend.gaming.fusion import FusionRule, bundle_of

        rule = FusionRule(
            event_type=GameEventType.COMBAT,
            name="two_frames_or_nothing",
            labels=("combat",),
            min_label_count=2,
        )
        one = bundle_of([self._observation(100.0, detectors.VISION, 0.9, label="combat")])
        two = bundle_of(
            [
                self._observation(100.0, detectors.VISION, 0.9, label="combat"),
                self._observation(101.0, detectors.VISION, 0.9, label="combat"),
            ]
        )

        assert not rule.matches(one)
        assert rule.matches(two)

    def test_a_rule_of_only_a_description_pattern_is_a_valid_rule(self) -> None:
        from backend.gaming.fusion import FusionRule, bundle_of

        rule = FusionRule(
            event_type=GameEventType.HIGH_DAMAGE,
            name="prose_only",
            description_pattern="explod",
        )
        bundle = bundle_of(
            [
                self._observation(
                    100.0, detectors.VISION, 0.9, description="The car explodes."
                )
            ]
        )

        assert rule.matches(bundle)

    def test_a_profile_refuses_a_broken_description_pattern(self) -> None:
        import pytest as _pytest

        from backend.gaming.profiles import ProfileFusionRule

        with _pytest.raises(Exception, match="not a regular expression"):
            ProfileFusionRule(
                event_type=GameEventType.CHASE,
                name="broken",
                description_pattern="police(",
            )


class TestFakeOcrProvider:
    def test_it_satisfies_the_provider_protocol(self) -> None:
        from ai.providers.base import OcrProvider

        assert isinstance(FakeOcrProvider(), OcrProvider)

    def test_it_scripts_text_per_frame(self, tmp_path: Path) -> None:
        provider = FakeOcrProvider(
            by_filename={"t000000012_000.jpg": [("ELIMINATED", 0.9)]},
            default=[("HUD", 0.5)],
        )
        scripted = provider.read(tmp_path / "t000000012_000.jpg")
        assert [item.text for item in scripted] == ["ELIMINATED"]
        assert [item.text for item in provider.read(tmp_path / "other.jpg")] == ["HUD"]

    def test_it_honours_the_confidence_floor(self, tmp_path: Path) -> None:
        provider = FakeOcrProvider(default=[("faint", 0.2), ("clear", 0.9)])
        kept = provider.read(tmp_path / "a.jpg", min_confidence=0.5)
        assert [item.text for item in kept] == ["clear"]


def _write_profile(root: Path, game: str) -> Path:
    directory = root / game
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.json").write_text(
        json.dumps(
            {
                "id": game,
                "name": "Test Game",
                "regions": {
                    "kill_feed": {"x": 0.7, "y": 0.1, "width": 0.28, "height": 0.2},
                    "score": {"x": 0.4, "y": 0.02, "width": 0.2, "height": 0.06},
                },
                "ocr_regions": ["kill_feed", "score"],
                "event_rules": [
                    {
                        "event_type": "kill",
                        "patterns": ["eliminated", "knocked"],
                        "regions": ["kill_feed"],
                        "confidence": 0.85,
                    }
                ],
                "ignore_patterns": ["^press "],
            }
        ),
        encoding="utf-8",
    )
    return directory


class TestTheShippedGtaProfile:
    """§23's bargain, for the second real game on this machine.

    Written from on-screen text read out of a 96-minute recording rather than
    from documentation, which is why the signatures are what that footage
    shows -- Director Mode scene-building -- and why the OCR-tolerant spellings
    are there: `DIRECTOR MODE` came back as `DIFIECTOR MODE`, `DINECTOH MODE`
    and `DIPECTOR MODE` across 161 readings of the same phrase.
    """

    @staticmethod
    def _profile():
        from backend.config.paths import find_repository_root

        clear_profile_cache()
        return load_profile("gta_v", find_repository_root() / "profiles").profile

    def test_it_can_be_detected_at_all(self) -> None:
        # It shipped with zero signature patterns, so detection -- which
        # matches on signatures -- could never identify it, and every GTA
        # recording fell back to `generic`. That cost a real project 62
        # unnamed driving events.
        assert self._profile().signature_patterns

    @pytest.mark.parametrize(
        "reading",
        ["DIRECTOR MODE", "DIFIECTOR MODE", "DINECTOH MODE", "DIPECTOR MODE"],
    )
    def test_the_ocr_spellings_of_one_phrase_all_match(self, reading: str) -> None:
        # All four are the same two words, read 161 times off one recording.
        # A signature that only matches the correct spelling matches a
        # minority of the readings it was written for.
        profile = self._profile()
        assert any(
            re.search(pattern, reading, re.IGNORECASE) for pattern in profile.signature_patterns
        ), reading

    def test_the_two_shipped_games_do_not_match_each_other(self) -> None:
        # A signature that fires on the wrong game is worse than none: it
        # applies rules written for footage this is not.
        from backend.config.paths import find_repository_root
        from backend.gaming.detection import detect_game

        clear_profile_cache()
        profiles = find_repository_root() / "profiles"
        gta = detect_game(
            ["DIRECTOR MODE", "SCENE CREATOR", "STUNT RAMPS", "PROPS PLACED"], profiles
        )
        grounded = detect_game(["Milk Molar", "Lean-To", "MUTATIONS", "Dandelion Tuft"], profiles)
        assert gta is not None and gta.game == "gta_v" and gta.runner_up_hits == 0
        assert grounded is not None and grounded.game == "grounded"
        assert grounded.runner_up_hits == 0

    def test_the_death_banner_is_a_death(self) -> None:
        # Region-scoped on purpose: "WASTED" anywhere on screen is a word, and
        # "WASTED" in the centre banner is the death screen.
        found = detectors.observations_from_ocr(
            [_text(10.0, "WASTED", region="centre_banner")], self._profile()
        )

        assert GameEventType.DEATH in {item.event_type for item in found}

    def test_the_editor_chrome_is_ignored_outright(self) -> None:
        profile = self._profile()

        # Director Mode's menus are on screen for minutes at a time.
        assert profile.should_ignore("SCENE")
        assert profile.should_ignore("CATEGORY")
        assert profile.should_ignore("12/25")
        assert profile.should_ignore("CLEAR SCENE")
        assert not profile.should_ignore("WASTED")

    def test_its_fusion_rules_convert(self) -> None:
        assert self._profile().fusion()
