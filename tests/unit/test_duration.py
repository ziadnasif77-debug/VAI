"""Duration policy tests (SPEC sections 6, 39).

The 10-60 minute band is the product's hardest rule, so it gets the most
adversarial tests in the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.core.duration import (
    ABSOLUTE_MAX_OUTPUT_SECONDS,
    ABSOLUTE_MIN_OUTPUT_SECONDS,
    DurationPolicy,
    TargetDurationError,
    format_duration,
    parse_duration,
)

pytestmark = pytest.mark.unit


def make_policy(**overrides) -> DurationPolicy:
    defaults = {
        "min_minutes": 10,
        "max_minutes": 60,
        "presets_minutes": [10, 20, 60],
        "default_minutes": 20,
        "tolerance_seconds": 45,
        "tolerance_ratio": 0.05,
    }
    return DurationPolicy.from_minutes(**{**defaults, **overrides})


class TestAbsoluteLimits:
    def test_product_limits_are_ten_and_sixty_minutes(self) -> None:
        assert ABSOLUTE_MIN_OUTPUT_SECONDS == 10 * 60
        assert ABSOLUTE_MAX_OUTPUT_SECONDS == 60 * 60

    def test_config_may_narrow_the_band(self) -> None:
        policy = make_policy(min_minutes=15, max_minutes=45, presets_minutes=[15, 30, 45])
        assert policy.min_seconds == 900
        assert policy.max_seconds == 2700

    def test_config_cannot_go_below_ten_minutes(self) -> None:
        with pytest.raises(ValueError, match="below the product minimum"):
            make_policy(min_minutes=5, presets_minutes=[10, 20])

    def test_config_cannot_go_above_sixty_minutes(self) -> None:
        with pytest.raises(ValueError, match="above the product maximum"):
            make_policy(max_minutes=90, presets_minutes=[10, 20])

    def test_inverted_band_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be below"):
            make_policy(min_minutes=40, max_minutes=20, presets_minutes=[40], default_minutes=40)

    def test_presets_outside_the_band_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside the band"):
            make_policy(min_minutes=20, presets_minutes=[10, 20])

    def test_default_outside_the_band_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside the configured band"):
            make_policy(min_minutes=20, presets_minutes=[20, 30], default_minutes=15)


class TestValidate:
    @pytest.mark.parametrize("seconds", [600, 601, 1200, 3599, 3600])
    def test_accepts_values_inside_the_band(self, seconds: int) -> None:
        assert make_policy().validate(seconds) == seconds

    @pytest.mark.parametrize("seconds", [0, 1, 599, 599.9, 3601, 7200, -600])
    def test_rejects_values_outside_the_band(self, seconds: float) -> None:
        with pytest.raises(TargetDurationError):
            make_policy().validate(seconds)

    def test_rejects_nine_minutes_fifty_nine(self) -> None:
        # The spec calls out 9:59 explicitly as something that must never ship.
        with pytest.raises(TargetDurationError):
            make_policy().validate(599)

    @pytest.mark.parametrize("value", ["1200", None, True, [1200]])
    def test_rejects_non_numeric_input(self, value: object) -> None:
        with pytest.raises(TargetDurationError, match="must be a number"):
            make_policy().validate(value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_input(self, value: float) -> None:
        with pytest.raises(TargetDurationError, match="finite"):
            make_policy().validate(value)

    def test_rounds_to_whole_seconds(self) -> None:
        assert make_policy().validate(1200.4) == 1200


class TestClamp:
    def test_clamps_below_the_minimum(self) -> None:
        assert make_policy().clamp(10) == 600

    def test_clamps_above_the_maximum(self) -> None:
        assert make_policy().clamp(9999) == 3600

    def test_leaves_in_band_values_alone(self) -> None:
        assert make_policy().clamp(1234) == 1234


class TestTolerance:
    def test_absolute_tolerance_wins_for_short_targets(self) -> None:
        # 600s * 5% = 30s, so the 45s absolute floor applies.
        assert make_policy().tolerance_for(600) == pytest.approx(45.0)

    def test_ratio_wins_for_long_targets(self) -> None:
        # 3600s * 5% = 180s, comfortably above the 45s floor.
        assert make_policy().tolerance_for(3600) == pytest.approx(180.0)

    def test_within_tolerance_accepts_a_near_miss(self) -> None:
        assert make_policy().within_tolerance(1220, 1200) is True

    def test_within_tolerance_rejects_a_wide_miss(self) -> None:
        assert make_policy().within_tolerance(1400, 1200) is False


class TestOutputAcceptance:
    def test_rendered_file_must_be_in_band(self) -> None:
        policy = make_policy()
        assert policy.accepts_output(600) is True
        assert policy.accepts_output(599.5) is False
        assert policy.accepts_output(3600.5) is False


class TestFormatting:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0:00"), (59, "0:59"), (600, "10:00"), (3600, "1:00:00"), (3661, "1:01:01")],
    )
    def test_format(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected

    def test_format_negative(self) -> None:
        assert format_duration(-90) == "-1:30"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("90", 90.0), ("1:30", 90.0), ("1:00:00", 3600.0), ("0:10:00", 600.0)],
    )
    def test_parse(self, text: str, expected: float) -> None:
        assert parse_duration(text) == expected

    @pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "1:70", "-", "1:-2"])
    def test_parse_rejects_malformed(self, text: str) -> None:
        with pytest.raises(ValueError):
            parse_duration(text)

    def test_round_trip(self) -> None:
        assert parse_duration(format_duration(1234)) == 1234.0


class TestSqlConstraintStaysInSync:
    """The migration restates the limits because SQL cannot import Python.

    This test is the guard that keeps the two definitions from drifting apart.
    """

    def test_migration_check_matches_the_constants(self) -> None:
        migration = (
            Path(__file__).parents[2]
            / "backend"
            / "database"
            / "migrations"
            / "0001_initial.sql"
        )
        sql = migration.read_text(encoding="utf-8")
        match = re.search(
            r"CHECK\s*\(\s*target_duration_seconds\s*>=\s*(\d+)\s*AND\s*"
            r"target_duration_seconds\s*<=\s*(\d+)\s*\)",
            sql,
        )
        assert match is not None, "The projects table must CHECK the duration band."
        assert int(match.group(1)) == ABSOLUTE_MIN_OUTPUT_SECONDS
        assert int(match.group(2)) == ABSOLUTE_MAX_OUTPUT_SECONDS
