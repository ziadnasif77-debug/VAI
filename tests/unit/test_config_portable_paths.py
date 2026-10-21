"""The shipped configuration names the owner's Windows folders, and loads anywhere.

Before 2026-10-21 ``Path("D:/Gaming 2026").is_absolute()`` was the test, which
is false on Linux and macOS: the whole configuration failed validation and the
suite could not be collected off Windows. A drive path is now absolute on every
platform; a writer asks ``usable_here`` before creating anything there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config.loader import load_config, reset_config_cache
from backend.config.schema import ApplicationConfig, DailyConfig
from backend.core.fs import usable_here

pytestmark = pytest.mark.unit

ON_WINDOWS = Path("D:/x").is_absolute()


def test_the_shipped_configuration_loads_on_this_machine() -> None:
    reset_config_cache()
    try:
        shipped = load_config()
    finally:
        reset_config_cache()
    assert shipped.daily.archive_directory is not None


def test_a_drive_path_is_an_absolute_source_root_everywhere() -> None:
    config = ApplicationConfig(media_source_roots=["D:/Gaming 2026"])
    assert config.media_source_roots == ["D:/Gaming 2026"]


def test_a_relative_source_root_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="must be absolute"):
        ApplicationConfig(media_source_roots=["Gaming 2026"])


@pytest.mark.parametrize("field", ["output_directory", "archive_directory"])
def test_a_relative_daily_folder_is_refused_by_its_own_name(field: str) -> None:
    with pytest.raises(ValidationError, match=f"daily.{field} must be an absolute path"):
        DailyConfig(**{field: "Ferdig"})


@pytest.mark.parametrize("field", ["output_directory", "archive_directory"])
def test_the_system_drive_is_refused_on_every_platform(field: str) -> None:
    with pytest.raises(ValidationError, match=f"daily.{field} refuses the system drive"):
        DailyConfig(**{field: "C:/Videos"})


def test_a_folder_on_this_machine_is_usable(tmp_path: Path) -> None:
    assert usable_here(str(tmp_path))
    assert not usable_here("Ferdig")


@pytest.mark.skipif(ON_WINDOWS, reason="a drive path is usable on Windows")
def test_a_drive_path_is_not_usable_off_windows() -> None:
    assert not usable_here("D:/Gaming 2026/Ferdig")
