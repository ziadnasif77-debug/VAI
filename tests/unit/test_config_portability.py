"""The shipped configuration loads on any machine, and the backend's
module-level imports are all declared as core dependencies."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config.loader import load_config, reset_config_cache
from backend.config.schema import ApplicationConfig, DailyConfig

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def test_the_shipped_configuration_validates_on_this_host() -> None:
    reset_config_cache()
    config = load_config()
    assert config.application.media_source_roots


@pytest.mark.parametrize("root", ["D:/Gaming 2026", "D:\\Gaming 2026", "/srv/media"])
def test_drive_and_posix_roots_are_absolute_everywhere(root: str) -> None:
    assert ApplicationConfig(media_source_roots=[root]).media_source_roots == [root]


@pytest.mark.parametrize("root", ["Gaming 2026", "D:Gaming", ""])
def test_relative_roots_are_still_refused(root: str) -> None:
    with pytest.raises(ValidationError, match="must be absolute"):
        ApplicationConfig(media_source_roots=[root])


def test_the_system_drive_is_refused_on_every_host() -> None:
    with pytest.raises(ValidationError, match=r"daily\.archive_directory refuses the system drive"):
        DailyConfig(archive_directory="C:/Ferdig")


def test_a_relative_daily_path_names_its_own_key() -> None:
    with pytest.raises(ValidationError, match=r"daily\.archive_directory must be an absolute"):
        DailyConfig(archive_directory="Ferdig")


def _module_level_imports(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


#: Import name -> distribution name, where the two differ.
_DISTRIBUTION = {"yaml": "pyyaml"}


def test_every_module_level_third_party_import_is_a_core_dependency() -> None:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+; the floor is 3.10
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        requirement.split(">")[0].split("=")[0].split("<")[0].strip().lower()
        for requirement in manifest["project"]["dependencies"]
    }
    local = {"backend", "ai", "__future__"}
    undeclared: dict[str, str] = {}
    for path in sorted((ROOT / "backend").rglob("*.py")):
        for name in _module_level_imports(path) - local - set(sys.stdlib_module_names):
            if _DISTRIBUTION.get(name, name).lower() not in declared:
                undeclared.setdefault(name, str(path.relative_to(ROOT)))
    assert not undeclared, f"imported at module level but not in [project].dependencies: {undeclared}"
