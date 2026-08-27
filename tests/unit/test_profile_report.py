"""The profile-mining helper (scripts/profile_report.py).

Only the pure parts: mining, ranking, escaping, and the snippet. The thin
``main()`` is repository calls the rest of the suite already covers, against a
database these tests deliberately never open.

The fixtures are the strings the tool was built for. ``DIRECTOR MODE`` was
read 161 times off one real GTA recording (with ``DIFIECTOR`` and ``DINECTOH``
among the spellings), ``12/25`` is Director Mode's prop counter, and
``O.R.C. guards`` is the Grounded quest text whose dots must survive escaping.
"""

from __future__ import annotations

import json
import re

import pytest

from scripts.profile_report import (
    GENERIC_STRINGS,
    escape_signature,
    mine_strings,
    normalise,
    signature_candidates,
    snippet,
)

pytestmark = pytest.mark.unit


class TestMining:
    def test_repeated_readings_rank_first(self) -> None:
        ranked = mine_strings(["DIRECTOR MODE"] * 3 + ["WASTED"])

        assert ranked[0] == ("DIRECTOR MODE", 3)
        assert ("WASTED", 1) in ranked

    def test_fragments_and_sentences_are_dropped(self) -> None:
        # Two characters is a stray glyph pair; 41 is tutorial prose that
        # never recurs verbatim. Both bounds are inclusive.
        ranked = mine_strings(["OK", "x" * 41, "abc", "y" * 40])

        texts = [text for text, _ in ranked]
        assert "OK" not in texts
        assert "x" * 41 not in texts
        assert "abc" in texts
        assert "y" * 40 in texts

    def test_whitespace_variants_are_one_string(self) -> None:
        # OCR spacing wobbles frame to frame; the words are the identity.
        ranked = mine_strings(["MILK   MOLAR", " MILK MOLAR "])

        assert ranked == [("MILK MOLAR", 2)]

    def test_case_variants_group_under_the_common_spelling(self) -> None:
        ranked = mine_strings(["Lean-To", "Lean-To", "LEAN-TO"])

        assert ranked == [("Lean-To", 3)]

    def test_blank_readings_produce_nothing(self) -> None:
        assert mine_strings(["", "   ", "\n"]) == []

    def test_ties_break_deterministically(self) -> None:
        # Same run, same report: equal counts sort alphabetically.
        ranked = mine_strings(["beta", "alpha"])

        assert ranked == [("alpha", 1), ("beta", 1)]


class TestRanking:
    def test_numbers_only_is_junk(self) -> None:
        # "12/25" was on screen for whole minutes of the real GTA recording;
        # every menu in every game draws a counter like it.
        kept = signature_candidates([("12/25", 40), ("DIRECTOR MODE", 40)])

        assert kept == [("DIRECTOR MODE", 40)]

    def test_rare_strings_are_not_proposed(self) -> None:
        assert signature_candidates([("VINEWOOD", 4)]) == []
        assert signature_candidates([("VINEWOOD", 5)]) == [("VINEWOOD", 5)]

    def test_generic_interface_vocabulary_is_not_proposed(self) -> None:
        # "Settings" is on screen in every game; frequency cannot make it
        # identify one.
        kept = signature_candidates([("Settings", 100), ("Milk Molar", 6)])

        assert kept == [("Milk Molar", 6)]

    def test_the_generic_check_ignores_case(self) -> None:
        assert signature_candidates([("SETTINGS", 50)]) == []

    def test_event_words_never_become_signatures(self) -> None:
        # VICTORY names what happened, not which game -- the generic patterns
        # in backend/gaming/events.py already read it.
        assert signature_candidates([("VICTORY", 30), ("GAME OVER", 12)]) == []

    def test_the_ignore_set_is_stored_normalised(self) -> None:
        # Membership is one casefolded lookup, so every entry must already be
        # in that form or it could never match anything.
        for entry in GENERIC_STRINGS:
            assert entry == normalise(entry).casefold()


class TestEscaping:
    def test_whitespace_becomes_s_plus(self) -> None:
        assert escape_signature("MILK MOLAR") == r"\bMILK\s+MOLAR\b"

    def test_a_run_of_spaces_is_one_gap(self) -> None:
        assert escape_signature("DIRECTOR   MODE") == r"\bDIRECTOR\s+MODE\b"

    def test_metacharacters_are_literals(self) -> None:
        pattern = escape_signature("O.R.C. guards")

        assert re.search(pattern, "Defeat the O.R.C. guards", re.IGNORECASE)
        # An unescaped dot would match anything and quietly widen the
        # signature to other games' text.
        assert not re.search(pattern, "OxRxCx guards", re.IGNORECASE)

    def test_the_anchor_does_its_job(self) -> None:
        pattern = escape_signature("WASTED")

        assert pattern == r"\bWASTED\b"
        assert re.search(pattern, "WASTED", re.IGNORECASE)
        assert not re.search(pattern, "UNWASTED", re.IGNORECASE)

    def test_a_punctuation_edge_gets_no_boundary(self) -> None:
        # \b beside a non-word character inverts its meaning, so the anchor
        # is only placed where the edge is a word character.
        pattern = escape_signature("(y)")

        assert pattern == re.escape("(y)")
        assert re.search(pattern, "press (y) to continue", re.IGNORECASE)

    def test_empty_input_escapes_to_nothing(self) -> None:
        assert escape_signature("   ") == ""

    @pytest.mark.parametrize(
        "text",
        ["DIRECTOR MODE", "O.R.C. guards", "Lean-To", "AMMU-NATION", "a+b (c) [d]"],
    )
    def test_every_pattern_compiles_the_way_profiles_are_matched(self, text: str) -> None:
        # Profile patterns are compiled with re.IGNORECASE
        # (backend/gaming/profiles.py); an escape that produced an
        # uncompilable pattern would fail profile validation on paste.
        compiled = re.compile(escape_signature(text), re.IGNORECASE)

        assert compiled.search(text)


class TestSnippet:
    def test_the_snippet_is_valid_json_in_the_shipped_shape(self) -> None:
        parsed = json.loads(snippet(["MILK MOLAR", "DIRECTOR MODE"]))

        assert parsed == {"signature_patterns": [r"\bMILK\s+MOLAR\b", r"\bDIRECTOR\s+MODE\b"]}

    def test_every_snippet_pattern_compiles(self) -> None:
        parsed = json.loads(snippet(["O.R.C. guards", "Lean-To", "(y)"]))

        for pattern in parsed["signature_patterns"]:
            re.compile(pattern, re.IGNORECASE)
