r"""Build the distributable: one folder, one zip, double-click to run.

What "installer" means for this application, stated against what it actually
needs. The heavy dependencies (torch's CUDA build alone is gigabytes) cannot
be frozen into an executable without shipping a DVD image, and `launch.py`
already solved the half that matters: it finds a Python by absolute path,
installs the dependencies into it live and on screen, repairs itself, and
holds the window open when something fails. So the product of this script is
a **portable package built around that launcher**:

    dist/VAI-<version>/
        VAI.bat            <- the double-click
        scripts/           <- launch.py, rooted.py, doctor, db_init
        backend/ ai/ ...   <- the application
        apps/web/dist/     <- the interface, prebuilt (no npm on the machine)
        tools/ffmpeg/      <- bundled, ahead of PATH
        tools/node/        <- bundled, for the caption overlay
        config/ prompts/ profiles/ remotion/
        INSTALL.txt        <- the two-line story, Arabic and English

Everything the repository accumulates but a user must never receive is
excluded by name: the virtualenv, caches, test artefacts, the developer's own
database and projects, and `.credentials/` — a distribution that carried the
builder's YouTube token would be a leak shipped as a feature.

First run on a machine with no Python still needs Python once; the launcher
detects that, says exactly which installer to fetch (python.org, 3.11, "Add
to PATH" ticked), and everything after that is automatic. Honest limits are
part of the product.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: What ships. Directories copied whole (minus the global excludes below),
#: files copied as they are.
INCLUDE_DIRS = [
    "backend",
    "ai",
    "scripts",
    "config",
    "prompts",
    "profiles",
    "docs",
    "apps/web/dist",
    "remotion/src",
    "remotion/public",
    "tools/ffmpeg",
    "tools/node",
]
INCLUDE_FILES = [
    "VAI.bat",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "tsconfig.base.json",
    "remotion/package.json",
    "remotion/tsconfig.json",
    "remotion/remotion.config.ts",
]
#: Empty on arrival; the application fills them (§84 keeps everything inside).
CREATE_DIRS = ["projects", "logs", "input", "output", ".cache", ".tmp"]

#: Never ships, wherever it appears.
EXCLUDE_NAMES = {
    "__pycache__",
    ".pytest-tmp",
    ".venv",
    "node_modules",
    ".git",
    ".credentials",
    "openhands-data",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
#: Builder state that regenerates at run time. `composition.json` is whatever
#: the packaging machine last rendered -- its project ids have no business in
#: anyone else's download.
EXCLUDE_FILES = {"composition.json"}


def _version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("version"):
            return line.split("=")[1].strip().strip('"')
    return "0.0.0"


def _ignore(directory: str, names: list[str]) -> set[str]:
    skipped = {name for name in names if name in EXCLUDE_NAMES or name in EXCLUDE_FILES}
    skipped |= {name for name in names if Path(name).suffix in EXCLUDE_SUFFIXES}
    return skipped


def _build_web() -> None:
    """The interface ships prebuilt: the target machine has no npm."""
    dist = REPO / "apps" / "web" / "dist"
    if dist.is_dir() and any(dist.iterdir()):
        return
    print("building the web interface ...")
    subprocess.run(
        ["npm", "run", "build", "-w", "apps/web"], cwd=REPO, check=True, shell=True
    )


def _strip_owner_client(target: Path) -> None:
    """The builder's OAuth client id must not ship.

    The id is public by design -- but it is the *builder's* id, and a
    distribution that carried it would bill every user's uploads against the
    builder's daily quota. The person who installs this creates their own
    client, exactly as the yaml's own comments instruct.
    """
    config = target / "config" / "publishing.yaml"
    if not config.is_file():
        return
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("client_id:") and stripped != "client_id: null":
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = f"{indent}client_id: null" + "\n"
    config.write_text("".join(lines), encoding="utf-8")


def _write_install_note(target: Path, version: str) -> None:
    (target / "INSTALL.txt").write_text(
        f"""VAI {version} — AI Gaming Video Editor
=====================================

English
-------
1. You need Python 3.11 once: https://www.python.org/downloads/
   (tick "Add python.exe to PATH" in its installer.)
2. Double-click VAI.bat. The first run installs the AI dependencies
   (a few GB, one time) and then opens http://127.0.0.1:8765.
3. Everything — recordings, analysis, finished videos — stays inside
   this folder. Nothing is uploaded unless you press Publish.

العربية
-------
1. تحتاج Python 3.11 مرة واحدة: https://www.python.org/downloads/
   (فعّل "Add python.exe to PATH" في مثبّته.)
2. انقر VAI.bat نقرة مزدوجة. أول تشغيل يثبّت تبعيات الذكاء الاصطناعي
   (عدة غيغابايت، مرة واحدة) ثم يفتح http://127.0.0.1:8765.
3. كل شيء — التسجيلات والتحليل والفيديوهات — يبقى داخل هذا المجلد.
   لا يُرفع شيء إلا حين تضغط "نشر".
""",
        encoding="utf-8",
    )


def build(*, zip_output: bool) -> Path:
    version = _version()
    target = REPO / "dist" / f"VAI-{version}"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    _build_web()

    for name in INCLUDE_DIRS:
        source = REPO / name
        if not source.exists():
            print(f"  skipping missing {name}")
            continue
        print(f"  {name}/")
        shutil.copytree(source, target / name, ignore=_ignore, dirs_exist_ok=True)

    for name in INCLUDE_FILES:
        source = REPO / name
        if source.is_file():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)

    for name in CREATE_DIRS:
        (target / name).mkdir(exist_ok=True)

    _write_install_note(target, version)
    _strip_owner_client(target)

    # The distribution must not inherit the builder's state. Belt braces:
    # the excludes above should have kept these out; verify rather than trust.
    for forbidden in (".credentials", "gaming_editor.db", ".venv"):
        found = list(target.rglob(forbidden))
        if found:
            raise SystemExit(f"refusing to package: {forbidden} leaked into {found[0]}")

    if zip_output:
        # Not ``with_suffix``: on "VAI-0.1.0" it would replace ".0" and ship
        # a zip named for the wrong version.
        archive = target.parent / f"{target.name}.zip"
        print(f"  zipping -> {archive.name} ...")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    z.write(path, path.relative_to(target.parent))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        size_mb = archive.stat().st_size / 1e6
        print(f"  {archive.name}: {size_mb:.0f} MB, sha256 {digest[:16]}…")
        (target.parent / f"{target.name}.zip.sha256").write_text(
            f"{digest}  {archive.name}\n", encoding="utf-8"
        )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-zip", action="store_true", help="folder only, no archive")
    arguments = parser.parse_args()
    target = build(zip_output=not arguments.no_zip)
    print(f"\npackage ready: {target}")


if __name__ == "__main__":
    sys.exit(main())
