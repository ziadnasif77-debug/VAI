"""The rooted environment (scripts/rooted.py).

One rule with a price tag attached. Everything this application causes to be
written goes under the project root — except the things it does not own, and
telling those apart is what this module gets wrong when it gets anything wrong.

``OLLAMA_MODELS`` is the case that cost something real. Ollama is a shared
runtime: several projects on this machine call the same 6 GB vision model, and
one loaded copy serves all of them. This module used to fill the variable in
when it found it empty, defaulting to ``D:/Models``. The machine's own setting
was ``F:\\Models``; a launch that did not inherit it wrote ``D:/Models``
instead, Ollama downloaded every model again, and **36.4 GB sat duplicated
across two drives for two months** — same five models, same twenty-two blobs,
byte for byte.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import rooted

pytestmark = pytest.mark.unit


class TestSharedResourcesAreNotClaimed:
    """What this application does not own, it does not point anywhere."""

    def test_an_unset_model_store_is_left_unset(self) -> None:
        # The whole defect, reduced: an empty variable is the machine's
        # business, and Ollama's own default is the right answer to it.
        env = rooted.environment({"PATH": ""})

        assert "OLLAMA_MODELS" not in env

    def test_the_machines_choice_is_passed_through_untouched(self) -> None:
        env = rooted.environment({"PATH": "", "OLLAMA_MODELS": r"F:\Models"})

        assert env["OLLAMA_MODELS"] == r"F:\Models"

    def test_no_model_path_is_invented_anywhere_in_the_environment(self) -> None:
        # A default that reappears under another name is the same defect.
        env = rooted.environment({"PATH": ""})

        assert not any("Models" in value for value in env.values())

    def test_the_report_says_unset_rather_than_guessing(self, monkeypatch) -> None:
        monkeypatch.delenv("OLLAMA_MODELS", raising=False)

        line = next(line for line in rooted.report() if line.startswith("models"))

        assert "unset" in line
        assert "D:/Models" not in line

    def test_the_report_names_the_store_in_use(self, monkeypatch) -> None:
        monkeypatch.setenv("OLLAMA_MODELS", r"F:\Models")

        line = next(line for line in rooted.report() if line.startswith("models"))

        assert r"F:\Models" in line
        assert "shared" in line


class TestWhatThisApplicationDoesOwn:
    """The other half of the rule: its own writes stay under the root."""

    def test_temporary_files_land_in_the_project(self) -> None:
        env = rooted.environment({"PATH": ""})

        assert Path(env["TMP"]) == rooted.ROOT / ".tmp"
        assert Path(env["TEMP"]) == rooted.ROOT / ".tmp"

    def test_package_caches_land_in_the_project(self) -> None:
        env = rooted.environment({"PATH": ""})

        assert Path(env["PIP_CACHE_DIR"]).is_relative_to(rooted.ROOT)
        assert Path(env["NPM_CONFIG_CACHE"]).is_relative_to(rooted.ROOT)

    def test_a_cache_already_off_the_system_drive_is_left_alone(self) -> None:
        # Re-rooting a multi-gigabyte cache that is already obeying the rule
        # costs a re-download and buys nothing.
        existing = r"E:\somewhere\hf"
        env = rooted.environment({"PATH": "", "HF_HOME": existing})

        assert env["HF_HOME"] == existing

    def test_a_cache_on_the_system_drive_is_moved(self) -> None:
        system = os.environ.get("SYSTEMDRIVE", "C:")
        env = rooted.environment({"PATH": "", "HF_HOME": f"{system}\\Users\\x\\.cache"})

        assert Path(env["HF_HOME"]).is_relative_to(rooted.ROOT)

    def test_the_bundled_tools_come_first_on_the_path(self) -> None:
        env = rooted.environment({"PATH": r"C:\Windows"})

        for directory in rooted.tool_directories():
            assert str(directory) in env["PATH"].split(os.pathsep)[: len(env["PATH"])]
        assert env["PATH"].endswith(r"C:\Windows")
