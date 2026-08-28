"""The distribution builder (scripts/package.py).

What ships is a product decision, and what must never ship is a leak. The
builder's rules are tested against a synthetic tree rather than the real repo,
because the test that copies 729 MB to check a filename is a test nobody runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import package

pytestmark = pytest.mark.unit


class TestWhatNeverShips:
    @pytest.mark.parametrize(
        "name",
        ["__pycache__", ".venv", ".git", ".credentials", "node_modules", ".pytest-tmp"],
    )
    def test_the_builders_state_is_excluded_by_name(self, name: str) -> None:
        assert name in package._ignore(".", [name, "keep.py"])

    def test_the_builders_last_composition_is_excluded(self) -> None:
        # Whatever the packaging machine last rendered -- its project ids have
        # no business in anyone else's download.
        assert "composition.json" in package._ignore("remotion/public", ["composition.json"])

    def test_compiled_artefacts_are_excluded_by_suffix(self) -> None:
        skipped = package._ignore(".", ["a.pyc", "b.log", "c.py"])
        assert skipped == {"a.pyc", "b.log"}

    def test_source_is_not_excluded(self) -> None:
        assert package._ignore(".", ["worker.py", "profile.json"]) == set()


class TestTheLeakGuard:
    def test_a_leaked_credential_store_refuses_the_whole_package(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Belt and braces, tested at the braces.

        The excludes should keep `.credentials` out; if anything ever routes
        around them, the build must die rather than ship a token.
        """
        fake_repo = tmp_path / "repo"
        (fake_repo / "backend").mkdir(parents=True)
        (fake_repo / "backend" / "app.py").write_text("x", encoding="utf-8")
        (fake_repo / "pyproject.toml").write_text('version = "9.9.9"', encoding="utf-8")
        (fake_repo / "apps" / "web" / "dist").mkdir(parents=True)
        (fake_repo / "apps" / "web" / "dist" / "index.html").write_text("x", encoding="utf-8")
        monkeypatch.setattr(package, "REPO", fake_repo)
        monkeypatch.setattr(package, "INCLUDE_DIRS", ["backend", "apps/web/dist"])
        monkeypatch.setattr(package, "INCLUDE_FILES", [])
        monkeypatch.setattr(package, "CREATE_DIRS", [])
        # Route around the excludes the way a future refactor might.
        monkeypatch.setattr(package, "_ignore", lambda directory, names: set())
        (fake_repo / "backend" / ".credentials").mkdir()
        (fake_repo / "backend" / ".credentials" / "youtube_oauth.json").write_text(
            "{}", encoding="utf-8"
        )

        with pytest.raises(SystemExit, match="refusing to package"):
            package.build(zip_output=False)

    def test_a_clean_tree_builds_and_carries_the_install_note(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fake_repo = tmp_path / "repo"
        (fake_repo / "backend").mkdir(parents=True)
        (fake_repo / "backend" / "app.py").write_text("x", encoding="utf-8")
        (fake_repo / "pyproject.toml").write_text('version = "9.9.9"', encoding="utf-8")
        (fake_repo / "apps" / "web" / "dist").mkdir(parents=True)
        (fake_repo / "apps" / "web" / "dist" / "index.html").write_text("x", encoding="utf-8")
        monkeypatch.setattr(package, "REPO", fake_repo)
        monkeypatch.setattr(package, "INCLUDE_DIRS", ["backend", "apps/web/dist"])
        monkeypatch.setattr(package, "INCLUDE_FILES", ["pyproject.toml"])
        monkeypatch.setattr(package, "CREATE_DIRS", ["projects"])

        target = package.build(zip_output=False)

        assert target.name == "VAI-9.9.9"
        note = (target / "INSTALL.txt").read_text(encoding="utf-8")
        assert "python.org" in note
        assert "VAI.bat" in note
        # Both languages, because the product's first market reads Arabic.
        assert "العربية" in note
        assert (target / "projects").is_dir()

    def test_the_zip_is_named_for_the_full_version(self, tmp_path: Path, monkeypatch) -> None:
        # `with_suffix` on "VAI-0.1.0" replaced ".0" and shipped a zip named
        # for the wrong version. The name is arithmetic now, not suffix games.
        fake_repo = tmp_path / "repo"
        (fake_repo / "backend").mkdir(parents=True)
        (fake_repo / "backend" / "app.py").write_text("x", encoding="utf-8")
        (fake_repo / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
        (fake_repo / "apps" / "web" / "dist").mkdir(parents=True)
        (fake_repo / "apps" / "web" / "dist" / "index.html").write_text("x", encoding="utf-8")
        monkeypatch.setattr(package, "REPO", fake_repo)
        monkeypatch.setattr(package, "INCLUDE_DIRS", ["backend"])
        monkeypatch.setattr(package, "INCLUDE_FILES", [])
        monkeypatch.setattr(package, "CREATE_DIRS", [])

        package.build(zip_output=True)

        assert (fake_repo / "dist" / "VAI-1.2.3.zip").is_file()
        assert (fake_repo / "dist" / "VAI-1.2.3.zip.sha256").is_file()


class TestTheOwnerClientNeverShips:
    def test_the_shipped_publishing_yaml_has_no_client_id(self, tmp_path) -> None:
        from scripts.package import _strip_owner_client

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "publishing.yaml").write_text(
            "publishing:\n  youtube:\n"
            "    client_id: 12345-abc.apps.googleusercontent.com\n",
            encoding="utf-8",
        )

        _strip_owner_client(tmp_path)

        text = (config_dir / "publishing.yaml").read_text(encoding="utf-8")
        assert "client_id: null" in text
        assert "googleusercontent" not in text

