"""Reading the story out of what the player said (SPEC §19, §21, §26, §95).

The gap this closes was measured, not guessed. A 41-minute recording with 658
seconds of speech produced 24 `surprise` moments and 3 `tension` ones and
nothing else -- a vocabulary of two for a recording where the player narrates
continuously. Every clip came from a signal spike, so no clip had a cause
attached to it, and the video read as "shots from everywhere".

What is tested here is mostly the *rejection*: a model reading a transcript
will occasionally answer with times it never saw, situations that span the
whole window, or a title in the wrong language. Each of those is a clip pointed
at footage nobody looked at, so each is dropped rather than clamped.
"""

from __future__ import annotations

import pytest

from ai.llm.fake_provider import FakeLLMProvider
from ai.providers.base import TranscriptSegment
from backend.analysis import narration
from backend.analysis.narration import (
    MAX_INCIDENTS_PER_WINDOW,
    MAX_TITLE_CHARACTERS,
    SOURCE,
    Incident,
    observations_from_narration,
    read_incidents,
)
from backend.config.loader import load_config
from backend.core.models.enums import GameEventType
from backend.core.prompts import load_prompt

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def config():
    return load_config()


def _segments(*rows: tuple[float, float, str]) -> list[TranscriptSegment]:
    return [TranscriptSegment(start=start, end=end, text=text) for start, end, text in rows]


#: A short window of real-shaped speech: a worry, the thing happening, the
#: reaction. Arabic because that is what this is used on.
SPEECH = _segments(
    (3.8, 5.0, "لا لسه ماشي من البيضة"),
    (24.8, 27.0, "بس ايه كان في العنكبوت وهيكي"),
    (39.0, 41.0, "عشان نلقي اذا بطلع بالليل"),
    (41.1, 43.0, "بيخرع علينا"),
    (76.5, 79.0, "ايه بيقتلني"),
)


def _incident(**overrides) -> dict:
    payload = {
        "title": "العنكبوت",
        "event_type": "unexpected_event",
        "start_seconds": 24.8,
        "climax_seconds": 41.1,
        "end_seconds": 79.0,
        "importance": 0.8,
        "quote": "بيخرع علينا",
    }
    payload.update(overrides)
    return payload


def _read(config, *incidents, **provider_kwargs) -> list[Incident]:
    provider = FakeLLMProvider(default={"incidents": list(incidents)}, **provider_kwargs)
    return read_incidents(SPEECH, config=config, provider=provider)


class TestWhatItReads:
    def test_a_situation_becomes_an_incident(self, config) -> None:
        found = _read(config, _incident())

        assert len(found) == 1
        assert found[0].event_type is GameEventType.UNEXPECTED_EVENT
        assert found[0].title == "العنكبوت"

    def test_the_span_is_cause_to_reaction_not_the_instant(self, config) -> None:
        # The whole point: a clip cut at the climax starts after the setup and
        # ends before the payoff.
        found = _read(config, _incident())

        assert found[0].start_seconds < found[0].climax_seconds < found[0].end_seconds

    def test_it_becomes_an_observation_not_an_event(self, config) -> None:
        # §27 decides what is an event. A model does not get to declare one on
        # its own, however sure it sounds.
        observations = observations_from_narration(_read(config, _incident()))

        assert [item.source for item in observations] == [SOURCE]
        assert observations[0].confidence == 0.8

    def test_the_observation_is_the_climax_not_the_whole_incident(self, config) -> None:
        # Handing correlation the whole cause-to-reaction span produced
        # 150-second "events", moments with a median length of 81s, and an edit
        # with six backwards jumps. An event is a thing that happened at a time.
        observations = observations_from_narration(_read(config, _incident()))

        assert observations[0].duration <= 10.0
        assert observations[0].start_seconds < 41.1 < observations[0].end_seconds

    def test_the_incident_bounds_are_carried_for_context_expansion(self, config) -> None:
        # §29 decides how much footage around the event belongs in the clip,
        # and the player's own account of where it began is the best evidence
        # available for that.
        observations = observations_from_narration(_read(config, _incident()))

        assert observations[0].detail["incident_start"] == 24.8
        assert observations[0].detail["incident_end"] == 79.0


class TestTheGrammarItIsGiven:
    """The schema is what Ollama decodes with, so its bounds are behaviour.

    An unbounded string invites the model to fill the output budget, and when
    the budget runs out mid-string the JSON never closes. Here that costs a
    whole six-minute window of speech per failure, and costs it *silently*:
    `_read_window` catches, logs one line, and returns nothing, so the
    recording is analysed as though the player said nothing for six minutes.
    It was logged repeatedly through a real 77-minute re-analysis before
    anybody read the line.
    """

    def test_every_string_the_model_writes_is_bounded(self) -> None:
        incidents = load_prompt("analysis.narration").output_schema["properties"]["incidents"]
        properties = incidents["items"]["properties"]

        assert incidents["maxItems"] == MAX_INCIDENTS_PER_WINDOW
        assert properties["title"]["maxLength"] == MAX_TITLE_CHARACTERS

    def test_the_model_is_not_asked_to_copy_the_players_words_back(self) -> None:
        """The field three windows in fifteen died inside.

        Measured against the real model on a 77-minute Arabic transcript: the
        model looped on repeated characters inside `quote` until the string
        never closed, and a `maxLength` did not stop it -- Ollama
        grammar-constrains the shape of an answer, not the length of its
        strings. Removing the field took those three windows from truncated to
        parsed, and nothing is lost: the incident carries the span the words
        were said in, and the transcript is stored.
        """
        properties = load_prompt("analysis.narration").output_schema["properties"]["incidents"][
            "items"
        ]["properties"]

        assert "quote" not in properties
        # The field stays on the type for the callers that read it.
        assert Incident("t", GameEventType.KILL, 0.0, 1.0, 2.0, 0.5).quote == ""

    def test_the_schema_that_runs_is_the_one_in_the_prompt_file(self) -> None:
        # There were two copies of this contract and they had drifted: the file
        # constrained times to be non-negative and importance to 0-1, and the
        # copy actually sent constrained neither. One source now, so the
        # documented shape and the decoded shape cannot disagree again.
        sent: list[dict] = []

        class _Recording(FakeLLMProvider):
            def complete_json(self, prompt, *, schema, prompt_id, temperature=None):
                sent.append(schema)
                return super().complete_json(
                    prompt, schema=schema, prompt_id=prompt_id, temperature=temperature
                )

        read_incidents(SPEECH, config=load_config(), provider=_Recording(default={"incidents": []}))

        assert sent
        assert sent[0] == load_prompt("analysis.narration").output_schema

    def test_a_window_that_fails_loses_only_itself(self) -> None:
        # The behaviour that made this invisible, kept deliberately: one bad
        # window must not lose the other fourteen.
        assert (
            read_incidents(SPEECH, config=load_config(), provider=FakeLLMProvider(fail_times=99))
            == []
        )


class TestTheCardAfterwards:
    """§54: whoever caused the model to load is who releases it."""

    def test_a_model_this_built_is_released(self, config, monkeypatch) -> None:
        """Measured before this existed: GAME_EVENTS finished and left
        `qwen2.5:7b-instruct` resident with 5,958 MB of an 8 GB card held --
        through MOMENTS, STORY, EDL, CRITIQUE and the render.
        """
        built = FakeLLMProvider(default={"incidents": []})
        monkeypatch.setattr(narration, "_provider", lambda _config: built)

        read_incidents(SPEECH, config=config, provider=None)

        assert built.unload_count == 1

    def test_a_model_the_caller_owns_is_left_alone(self, config) -> None:
        # The caller passed it in, so the caller decides when it goes -- and
        # in the pipeline that caller reuses it for the next window.
        theirs = FakeLLMProvider(default={"incidents": []})

        read_incidents(SPEECH, config=config, provider=theirs)

        assert theirs.unload_count == 0


class TestWhatItRefuses:
    def test_a_time_outside_the_transcript_is_dropped(self, config) -> None:
        # The dangerous failure: a plausible incident pointing at footage the
        # model never saw. Clamping it would move a clip somewhere nobody
        # looked, so it does not survive at all.
        assert (
            _read(
                config, _incident(start_seconds=9000.0, climax_seconds=9010.0, end_seconds=9020.0)
            )
            == []
        )

    def test_times_out_of_order_are_dropped(self, config) -> None:
        assert _read(config, _incident(climax_seconds=10.0, start_seconds=50.0)) == []

    def test_an_incident_longer_than_the_limit_is_dropped(self, config) -> None:
        # Not a long situation -- a model that failed to find the seam between
        # several. Keeping it would put a two-minute clip in a ten-minute video.
        limit = config.analysis.narration.max_incident_seconds
        assert _read(config, _incident(start_seconds=4.0, end_seconds=4.0 + limit + 5)) == []

    def test_an_incident_shorter_than_a_situation_is_dropped(self, config) -> None:
        assert (
            _read(config, _incident(start_seconds=41.0, climax_seconds=41.1, end_seconds=41.5))
            == []
        )

    def test_a_low_importance_incident_is_dropped(self, config) -> None:
        assert _read(config, _incident(importance=0.05)) == []

    def test_an_unknown_event_type_is_dropped(self, config) -> None:
        # §93's schema makes this rare, not impossible: a model forced onto a
        # grammar still emits the closest allowed value, and a bad one here
        # would become a mislabelled clip.
        assert _read(config, _incident(event_type="teabagged_them")) == []

    def test_a_title_in_a_script_the_speech_never_used_is_dropped(self, config) -> None:
        # qwen is a Chinese model and leaks: reading this Arabic transcript it
        # labelled three incidents "合作". The title is decorative, so it goes
        # rather than taking a sound incident with it.
        found = _read(config, _incident(title="合作"))

        assert len(found) == 1
        assert found[0].title == ""
        assert found[0].event_type is GameEventType.UNEXPECTED_EVENT

    def test_one_bad_incident_does_not_lose_the_good_ones(self, config) -> None:
        found = _read(config, _incident(start_seconds=9000.0), _incident())

        assert len(found) == 1


class TestWithoutAModel:
    def test_no_model_means_no_narration_events(self, config) -> None:
        # §95: this adds a source, it does not become one the rest depends on.
        assert (
            read_incidents(SPEECH, config=config, provider=FakeLLMProvider(available=False)) == []
        )

    def test_no_speech_costs_no_model_call(self, config) -> None:
        provider = FakeLLMProvider(default={"incidents": [_incident()]})

        assert read_incidents([], config=config, provider=provider) == []
        assert provider.calls == []

    def test_a_model_that_fails_loses_the_window_not_the_run(self, config) -> None:
        # One window failing is a window. The stage still returns what the
        # others read.
        provider = FakeLLMProvider(default={"incidents": [_incident()]}, fail_times=1)

        assert read_incidents(SPEECH, config=config, provider=provider) == []


class TestOverlappingWindows:
    def test_the_same_situation_read_twice_is_one_incident(self, config) -> None:
        # Windows overlap on purpose, so a situation on a boundary is usually
        # read twice. Keeping both would put the same footage in twice -- which
        # is a defect this project has already shipped once.
        found = _read(config, _incident(), _incident(importance=0.6, title="نفس الشيء"))

        assert len(found) == 1
        assert found[0].importance == 0.8, "the weaker reading won"

    def test_two_genuinely_different_situations_both_survive(self, config) -> None:
        found = _read(
            config,
            _incident(start_seconds=3.8, climax_seconds=5.0, end_seconds=27.0),
            _incident(start_seconds=39.0, climax_seconds=41.1, end_seconds=79.0),
        )

        assert len(found) == 2
        assert found[0].start_seconds < found[1].start_seconds, "not in order"
