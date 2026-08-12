"""Answering in the language the question was asked in (SPEC §57, §63).

The parser has read Arabic since Phase 13; the answers were English f-strings
built at the point of use, so "ما مدة الفيديو؟" came back as "The current edit
is 9:59 across 12 clips." Half a conversation.

The test that matters most here is the dull one: every phrase exists in both
languages, with the same placeholders. A reply that starts in Arabic and ends
in English reads as broken software rather than as a partial feature, and that
is exactly what a missing key would produce at the moment someone hits it.
"""

from __future__ import annotations

import re
import string

import pytest

from backend.core.models.enums import MomentType
from backend.interaction.phrases import (
    BRIEF_VALUES,
    DIMENSIONS,
    EVENT_TYPES,
    MOMENT_TYPES,
    PHRASES,
    Phrasebook,
    language_of,
)
from backend.moments.explanation import ReasonFacts, build_reasons

pytestmark = pytest.mark.unit


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


class TestTheCatalogueIsComplete:
    @pytest.mark.parametrize("key", sorted(PHRASES))
    def test_every_phrase_has_both_languages(self, key: str) -> None:
        assert set(PHRASES[key]) == {"ar", "en"}, f"{key} is missing a language"
        assert PHRASES[key]["ar"].strip(), f"{key} has an empty Arabic phrase"

    @pytest.mark.parametrize("key", sorted(PHRASES))
    def test_both_languages_take_the_same_arguments(self, key: str) -> None:
        # A placeholder present in one language and not the other is a KeyError
        # in production and a silently dropped value in review.
        assert _placeholders(PHRASES[key]["en"]) == _placeholders(PHRASES[key]["ar"])

    @pytest.mark.parametrize("key", sorted(PHRASES))
    def test_the_arabic_is_actually_arabic(self, key: str) -> None:
        # Guards against a placeholder-only "translation" that was never done.
        arabic = PHRASES[key]["ar"]
        assert re.search(r"[؀-ۿ]", arabic), f"{key} was never translated"

    def test_every_moment_type_has_an_arabic_name(self) -> None:
        # §32's types appear in almost every answer. One left in English makes
        # the sentence around it look like a bug.
        missing = [item.value for item in MomentType if item.value not in MOMENT_TYPES]
        assert not missing, f"no Arabic name for: {missing}"


class TestChoosingTheLanguage:
    def test_arabic_text_is_answered_in_arabic(self) -> None:
        assert language_of("كم مدة الفيديو؟") == "ar"

    def test_english_text_is_answered_in_english(self) -> None:
        assert language_of("how long is the video?") == "en"

    def test_a_mixed_sentence_counts_as_arabic(self) -> None:
        # The Arabic is the part they wrote; "clip" is the part they copied.
        assert language_of("احذف clip 3") == "ar"

    def test_no_text_falls_back_rather_than_failing(self) -> None:
        # A command built by the API rather than by a sentence has no raw text.
        assert language_of(None) == "en"
        assert language_of("") == "en"


class TestSaying:
    def test_a_missing_phrase_names_itself(self) -> None:
        with pytest.raises(KeyError, match=re.escape("phrases.py")):
            Phrasebook("ar").say("no_such_phrase")

    def test_vocabulary_is_translated_not_only_the_frame(self) -> None:
        ar = Phrasebook("ar")
        assert ar.moment_type("clutch") == "حاسمة"
        assert ar.event_type("low_health") == "صحة منخفضة"
        assert ar.dimension("visual") == "الصورة"
        assert ar.brief_value("very_fast") == "سريع جداً"

    def test_an_unknown_term_survives_rather_than_vanishing(self) -> None:
        # A new moment type shipped before its translation must still appear.
        ar = Phrasebook("ar")
        assert ar.moment_type("teabag") == "teabag"
        assert ar.dimension("new_dimension") == "new dimension"

    def test_english_leaves_the_vocabulary_alone_but_unsnakes_it(self) -> None:
        en = Phrasebook("en")
        assert en.moment_type("clutch") == "clutch"
        assert en.dimension("dead_time") == "dead time"

    def test_lists_are_joined_the_way_the_language_does(self) -> None:
        assert Phrasebook("ar").join(["أ", "ب"]) == "أ، ب"
        assert Phrasebook("en").join(["a", "b"]) == "a, b"

    def test_numbers_and_timestamps_are_left_alone(self) -> None:
        # Localising the digits would make a value unrecognisable to whoever
        # types it back into the timeline.
        text = Phrasebook("ar").say("edit_duration", duration="9:59", clips=12)
        assert "9:59" in text
        assert "12" in text


class TestTheVocabularyTablesAreSane:
    def test_no_table_maps_a_term_to_itself(self) -> None:
        # A row that "translates" to the English is a row someone forgot.
        for name, table in (
            ("MOMENT_TYPES", MOMENT_TYPES),
            ("EVENT_TYPES", EVENT_TYPES),
            ("DIMENSIONS", DIMENSIONS),
            ("BRIEF_VALUES", BRIEF_VALUES),
        ):
            untranslated = [key for key, value in table.items() if key == value]
            assert not untranslated, f"{name} left untranslated: {untranslated}"


class TestTheScorerAndTheReaderAgree:
    """The rules are shared, and this is what keeps them shared.

    The scorer writes the §80 explanation once, in English, at analysis time.
    The reader re-derives it in Arabic from the same stored facts. That only
    stays honest while both go through `build_reasons` -- a second copy of the
    rules in either place would drift silently, and nobody would notice until
    the two disagreed about a clip someone was arguing with.
    """

    FACTS = ReasonFacts(
        moment_type="clutch",
        dimensions={"gameplay": 0.95, "reaction": 0.88, "audio": 0.7, "visual": 0.2},
        confidence=0.9,
        sources=("audio", "vision"),
        event_types=("multi_kill",),
        event_count=2,
        dead_time=0.4,
        repetition=0.3,
        review_threshold=0.6,
    )

    def test_both_languages_produce_the_same_reasons(self) -> None:
        english = build_reasons(self.FACTS, Phrasebook("en"))
        arabic = build_reasons(self.FACTS, Phrasebook("ar"))

        assert len(english) == len(arabic), "one language dropped a reason"

    def test_the_scorer_writes_what_the_reader_would_rebuild(self) -> None:
        # Not "they look similar" -- identical, because it is one function.
        assert build_reasons(self.FACTS, Phrasebook()) == build_reasons(
            self.FACTS, Phrasebook("en")
        )

    def test_a_weak_dimension_is_not_called_a_strength(self) -> None:
        reasons = build_reasons(self.FACTS, Phrasebook("en"))

        assert "visual" not in reasons[0], "0.2 was named as a strength"

    def test_penalties_come_last(self) -> None:
        # Someone asking "why this clip?" wants the answer before the caveats.
        reasons = build_reasons(self.FACTS, Phrasebook("en"))
        first_penalty = next(i for i, line in enumerate(reasons) if "Penalised" in line)

        assert first_penalty > 0
        assert all("Penalised" not in line for line in reasons[:first_penalty])

    def test_no_facts_means_no_invented_reasons(self) -> None:
        bare = ReasonFacts(moment_type="clutch", dimensions={}, confidence=1.0)

        assert build_reasons(bare, Phrasebook("ar")) == []
