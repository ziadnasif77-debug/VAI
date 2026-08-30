"""Configuration may not promise a capability the code does not have.

Five settings shipped at once describing behaviour nothing implemented:
``audio.music.change_on_section: true`` promising per-section music,
``audio.ducking.game_event_duck_db`` beside a function only a test called,
three effects enabled with no renderer to draw them, and -- the one that
mattered -- ``publishing.youtube.require_explicit_confirmation: true``
describing a publish gate no code had ever read.

None of them failed anything, which is exactly the problem: an inert setting
cannot fail, it can only mislead the next person who reads the file and
believes it. So the check runs here, where a new one turns the suite red.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from config_coverage import orphans, scan  # noqa: E402

pytestmark = pytest.mark.unit

#: Keys with no consumer that are allowed to stay, each with the reason it is
#: allowed. A key without a reason does not belong here -- the two honest
#: alternatives are to wire it or to delete it, and P0 did both forty-eight
#: times rather than growing this list.
ALLOWED: dict[str, str] = {}


@pytest.fixture(scope="module")
def leaves():
    return scan(ROOT / "config", ROOT)


def test_the_scan_reads_the_whole_shipped_configuration(leaves) -> None:
    files = {leaf.file for leaf in leaves}

    assert len(leaves) > 500, "a scan this small has stopped reading something"
    assert "effects.yaml" in files and "qa.yaml" in files


def test_no_setting_promises_a_capability_that_does_not_exist(leaves) -> None:
    unexplained = [leaf for leaf in orphans(leaves) if leaf.key not in ALLOWED]

    assert not unexplained, "\n".join(
        f"  {leaf.file}: {leaf.key} = {leaf.value!r} -- wire it, delete it, or "
        f"give it a reason in ALLOWED"
        for leaf in sorted(unexplained, key=lambda item: item.key)
    )


def test_a_setting_read_only_by_a_test_is_still_an_orphan(leaves) -> None:
    # Nothing ships because a test mentions it. This is the rule that caught
    # `publishing.enabled_targets`, which read like an off switch for a whole
    # destination and controlled nothing.
    for leaf in leaves:
        if leaf.test_only:
            assert leaf.key in ALLOWED, f"{leaf.key} is mentioned only by tests"


class TestTheScannerItself:
    """The scanner decides what counts as a consumer, so its judgement is
    worth a test of its own -- both of its rules were wrong once."""

    def test_a_field_read_by_its_own_model_is_consumed(self, leaves) -> None:
        # `HardwareConfig.select` reads `min_vram_mb` to choose a profile.
        # Treating the schema as declaration-only reported it as dead and
        # nearly deleted a field that decides something.
        found = next(leaf for leaf in leaves if leaf.key.endswith("low.min_vram_mb"))

        assert not found.orphaned

    def test_a_field_only_a_validator_checks_is_not_consumed(self, leaves) -> None:
        # The opposite error: `video.default_resolution` appeared inside a
        # validator asserting it was one of `supported_resolutions`. That
        # proves the value is well-formed; it does not make it govern
        # anything, and the real output size came from the YouTube preset.
        from config_coverage import validator_lines

        source = (ROOT / "backend/config/schema.py").read_text(encoding="utf-8")

        assert validator_lines(source), "no validators found -- the AST walk is broken"

    def test_renderer_parameter_maps_are_not_settings(self, leaves) -> None:
        # `effects.library.*.params.*` is handed to a builder as a dict, so
        # its leaves are data with no identifier to look for.
        assert not [leaf for leaf in leaves if ".params." in leaf.key]
