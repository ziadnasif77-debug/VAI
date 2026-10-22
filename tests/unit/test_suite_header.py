"""The suite says up front which analysis packages it is running without.

Without scenedetect the SCENES stage fails, and every pipeline test then
fails later on ``outcomes[JobStage.RENDER]`` with a ``KeyError`` that names
nothing; on the cloud runner that read as an untriaged suite (BL-017).
"""

from __future__ import annotations

import importlib.util

import pytest

from tests import conftest


@pytest.mark.unit
class TestAnalysisHeader:
    def test_a_missing_package_is_named_with_the_install_command(self, monkeypatch) -> None:
        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a: None if name == "scenedetect" else real(name, *a),
        )
        assert conftest.missing_analysis_packages() == ["scenedetect"]
        header = conftest.pytest_report_header(None)
        assert "scenedetect not installed" in header
        assert 'pip install -e ".[dev,ai]"' in header

    def test_a_complete_stack_adds_no_header(self, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object())
        assert conftest.missing_analysis_packages() == []
        assert conftest.pytest_report_header(None) is None
