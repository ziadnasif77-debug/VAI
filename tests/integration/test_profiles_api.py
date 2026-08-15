"""The Game Profile API, and the profile that justifies it (SPEC §22, §23, §111).

§111 asks for the API *after* one real game works, and the point of it is a
single claim: **a second game is a data change, not a code change.** These
tests are that claim, checked from outside the process — a profile can be
listed, fetched and validated without anyone importing a Python module.

The validate endpoint gets the most attention because it is the one that pays
for itself. Writing the GTA V profile produced two event types that read like
real ones and are not, and a two-hour analysis is an expensive place to find
that out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.gaming.profiles import GameProfile

pytestmark = pytest.mark.integration

PROFILES = "/api/profiles"


@pytest.fixture(scope="module")
def shipped_profiles() -> Path:
    return Path(__file__).resolve().parents[2] / "profiles"


class TestListing:
    def test_the_generic_profile_is_always_listed(self, api_client) -> None:
        # §23: the application must work without a profile, so the one it falls
        # back to is never absent.
        body = api_client.get(PROFILES).json()

        assert body["generic"] == "generic"
        assert "generic" in {item["id"] for item in body["items"]}

    def test_each_entry_says_what_the_profile_actually_declares(self, api_client) -> None:
        # A profile that validates and declares nothing is a common and silent
        # mistake, and the counts are what make it visible.
        items = {item["id"]: item for item in api_client.get(PROFILES).json()["items"]}
        generic = items["generic"]

        assert generic["generic"] is True
        assert generic["regions"] == generic["event_rules"] == generic["hud_indicators"] == 0


class TestFetching:
    def test_an_unknown_game_gets_the_generic_profile_rather_than_a_404(
        self, api_client
    ) -> None:
        # §23 again: asking about a game nobody has written a profile for is
        # the normal case, not an error.
        body = api_client.get(f"{PROFILES}/no_such_game").json()

        assert body["exact"] is False
        assert body["requested"] == "no_such_game"
        assert body["profile"]["id"] == "generic"

    def test_a_malformed_profile_is_reported_rather_than_silently_generic(
        self, api_client, app_state
    ) -> None:
        # Falling back to generic here would look exactly like the feature
        # working, which is the worst way for a broken profile to behave.
        broken = app_state.paths.profiles_dir / "broken"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "profile.json").write_text('{"regions": {"x": "nope"}}', encoding="utf-8")
        from backend.gaming.profiles import clear_profile_cache

        clear_profile_cache()

        response = api_client.get(f"{PROFILES}/broken")

        assert response.status_code == 422
        assert response.json()["detail"]["game"] == "broken"


class TestValidation:
    def test_a_good_profile_validates_and_reports_what_it_would_do(self, api_client) -> None:
        candidate = {
            "id": "example",
            "regions": {"banner": {"x": 0.2, "y": 0.4, "width": 0.6, "height": 0.2}},
            "ocr_regions": ["banner"],
            "event_rules": [
                {"event_type": "victory", "patterns": ["VICTORY"], "regions": ["banner"]}
            ],
        }

        body = api_client.post(f"{PROFILES}/validate", json={"profile": candidate}).json()

        assert body["valid"] is True
        assert body["summary"]["event_rules"] == 1
        assert body["summary"]["ocr_regions"] == 1

    def test_an_event_type_that_does_not_exist_is_named(self, api_client) -> None:
        # The actual mistake made writing the GTA V profile, twice.
        body = api_client.post(
            f"{PROFILES}/validate",
            json={"profile": {"id": "x", "event_rules": [{"event_type": "objective_complete"}]}},
        ).json()

        assert body["valid"] is False
        assert any("event_type" in message for message in body["errors"])

    def test_ocr_pointed_at_a_region_that_does_not_exist_is_named(self, api_client) -> None:
        body = api_client.post(
            f"{PROFILES}/validate", json={"profile": {"id": "x", "ocr_regions": ["nowhere"]}}
        ).json()

        assert body["valid"] is False
        assert any("nowhere" in message for message in body["errors"])

    def test_a_region_outside_the_frame_is_refused(self, api_client) -> None:
        body = api_client.post(
            f"{PROFILES}/validate",
            json={
                "profile": {
                    "id": "x",
                    "regions": {"far": {"x": 0.9, "y": 0.1, "width": 0.5, "height": 0.1}},
                }
            },
        ).json()

        assert body["valid"] is False

    def test_an_uncompilable_pattern_is_refused(self, api_client) -> None:
        body = api_client.post(
            f"{PROFILES}/validate",
            json={
                "profile": {
                    "id": "x",
                    "event_rules": [{"event_type": "victory", "patterns": ["(unclosed"]}],
                }
            },
        ).json()

        assert body["valid"] is False

    def test_invalid_is_a_successful_answer(self, api_client) -> None:
        # The caller asked whether this document is valid. "No, and here is
        # why" answers that question; a 422 would say the *question* was wrong.
        response = api_client.post(
            f"{PROFILES}/validate", json={"profile": {"id": "x", "ocr_regions": ["nowhere"]}}
        )

        assert response.status_code == 200

    def test_nothing_here_writes_a_profile(self, api_client, app_state) -> None:
        # An endpoint that installed a profile would be an upload path into a
        # directory the pipeline reads event rules from.
        before = sorted(p.name for p in app_state.paths.profiles_dir.glob("*"))
        api_client.post(f"{PROFILES}/validate", json={"profile": {"id": "written"}})

        assert sorted(p.name for p in app_state.paths.profiles_dir.glob("*")) == before


class TestTheShippedGtaProfile:
    """§111: every shipped profile is written from footage, never speculation.

    The rule §111 states is "do not create 10 profiles before validating the
    architecture", and what makes a profile speculative is having no recording
    behind it. Two ship, and each was read off frames this project analysed:
    ``gta_v`` from the first real run, ``grounded`` from the OCR of two 40-77
    minute recordings whose game the pipeline had been calling generic.
    """

    def test_only_profiles_written_from_real_footage_ship(
        self, shipped_profiles: Path
    ) -> None:
        games = sorted(
            entry.name
            for entry in shipped_profiles.iterdir()
            if (entry / "profile.json").is_file()
        )

        assert games == ["grounded", "gta_v"]

    def test_it_loads_and_declares_something(self, shipped_profiles: Path) -> None:
        document = json.loads((shipped_profiles / "gta_v" / "profile.json").read_text("utf-8"))
        profile = GameProfile.model_validate(document)

        assert not profile.is_generic
        assert profile.hud, "a profile with no HUD would not exercise §24 at all"
        assert profile.event_rules

    def test_the_furniture_it_ignores_is_furniture_it_will_meet(
        self, shipped_profiles: Path
    ) -> None:
        # Every one of these was read off a real frame of the test footage.
        document = json.loads((shipped_profiles / "gta_v" / "profile.json").read_text("utf-8"))
        profile = GameProfile.model_validate(document)

        for text in ("DIRECTOR MODE", "INVINCIBILITY  03:44", "Cheat activated. Give weapons."):
            assert profile.should_ignore(text), text

    def test_the_text_that_matters_is_not_ignored(self, shipped_profiles: Path) -> None:
        document = json.loads((shipped_profiles / "gta_v" / "profile.json").read_text("utf-8"))
        profile = GameProfile.model_validate(document)

        for text in ("WASTED", "BUSTED", "MISSION PASSED"):
            assert not profile.should_ignore(text), text
            assert profile.rules_for(text, region="centre_banner"), text

    def test_grounded_reads_its_death_screen_and_ignores_its_quest_tracker(
        self, shipped_profiles: Path
    ) -> None:
        # Both halves were measured. The death screen never produced an event
        # because no profile was ever loaded; the quest tracker produced
        # nineteen false defeats in one recording because the generic pattern
        # read its imperative verb as a result.
        document = json.loads(
            (shipped_profiles / "grounded" / "profile.json").read_text("utf-8")
        )
        profile = GameProfile.model_validate(document)

        assert profile.rules_for("DEATH")
        assert profile.rules_for("Hoops died by misadventure")
        assert profile.should_ignore("Defeat the O.RC guards at the Milk Molar stash")
        assert profile.should_ignore("Set your respawn point at your Lean-To:")

    def test_grounded_signatures_are_words_only_this_game_writes(
        self, shipped_profiles: Path
    ) -> None:
        # A signature another survival game would also write recognises the
        # wrong game, and a wrong profile reads one game's screen with
        # another's rules.
        document = json.loads(
            (shipped_profiles / "grounded" / "profile.json").read_text("utf-8")
        )
        profile = GameProfile.model_validate(document)

        assert profile.signature_hits(["Milk Molar stash", "Lean-To", "MUTATIONS"]) >= 3
        assert profile.signature_hits(["Craft", "Analyze", "Close", "Inventory"]) == 0

    def test_the_hud_window_is_wider_than_the_glyph_row_it_looks_for(
        self, shipped_profiles: Path
    ) -> None:
        # Deliberate: the row is right-anchored and slides as it grows, so the
        # window has to hold every position it can take.
        document = json.loads((shipped_profiles / "gta_v" / "profile.json").read_text("utf-8"))
        profile = GameProfile.model_validate(document)
        window = profile.hud[0].region
        measured = profile.regions["wanted_stars"]

        assert window.width > measured.width
        assert window.x <= measured.x
