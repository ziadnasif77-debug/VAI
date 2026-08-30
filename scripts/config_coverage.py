"""Config-to-Code coverage: every YAML leaf, and whether any code reads it.

A setting that reads as enabled and has no consumer is a lie the next reader
believes. This project shipped five of them at once -- ``change_on_section:
true`` promising per-section music, ``game_event_duck_db`` beside a function
only a test called, three effects planned and stored with no renderer, and
``require_explicit_confirmation: true`` describing a publish gate that no code
had ever read. None of them failed anything. That is the point: an inert
setting cannot fail, it can only mislead.

So the check runs like a test rather than like a report. A leaf counts as
consumed when its identifier appears anywhere outside the schema and the
loader -- attribute access, a ``getattr`` string, a ``model_dump`` key. That
is deliberately generous: whatever survives it is orphaned with near-certainty,
and the exit code says so.

Usage::

    python scripts/config_coverage.py            # summary + exit 1 on a new orphan
    python scripts/config_coverage.py --json     # the whole table, for tooling
    python scripts/config_coverage.py --all      # every key, not only the orphans
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):  # not a real terminal
        _stream.reconfigure(encoding="utf-8", errors="replace")

#: Where a consumer may live. Tests are searched separately: a key only a test
#: mentions is still an orphan, because nothing ships because of it.
SOURCE_DIRS: Final[tuple[str, ...]] = ("backend", "ai", "apps", "scripts")
TEST_DIRS: Final[tuple[str, ...]] = ("tests",)
SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset({".py", ".ts", ".tsx", ".js", ".sql"})

#: The schema declares and the loader maps, so a key mentioned only there is
#: the shape of the defect this hunts -- with one real exception: these models
#: carry behaviour, and a field read inside one of their own methods (as
#: ``HardwareConfig.select`` reads ``min_vram_mb``) is genuinely consumed. So a
#: schema mention counts as consumption unless it is the declaration line.
DECLARERS: Final[tuple[str, ...]] = (
    "backend/config/schema.py",
    "backend/config/loader.py",
)

#: Renderer parameter maps are open by design: ``effects.library.*.params.*``
#: is handed to a builder as a dict, so its leaves are data rather than
#: settings and have no identifier to look for.
OPEN_MAPS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^effects\.library\.[a-z_]+\.params\."),
)


@dataclass(frozen=True)
class Leaf:
    """One configuration key, and who mentions its name."""

    file: str
    key: str
    name: str
    value: Any
    product: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    schema: tuple[str, ...] = ()

    @property
    def orphaned(self) -> bool:
        return not self.product

    @property
    def test_only(self) -> bool:
        return not self.product and bool(self.tests)


@dataclass
class Corpus:
    """Every source file read once, split by the role it plays here."""

    product: dict[str, str] = field(default_factory=dict)
    tests: dict[str, str] = field(default_factory=dict)
    declarers: dict[str, str] = field(default_factory=dict)


def read_corpus(root: Path) -> Corpus:
    corpus = Corpus()
    for directory in (*SOURCE_DIRS, *TEST_DIRS):
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            if "__pycache__" in relative or "/node_modules/" in relative:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if relative.endswith(DECLARERS):
                corpus.declarers[relative] = text
            elif relative.startswith(TEST_DIRS):
                corpus.tests[relative] = text
            else:
                corpus.product[relative] = text
    return corpus


def validator_lines(source: str) -> frozenset[int]:
    """Line numbers inside a Pydantic validator.

    A validator that checks ``default_resolution`` is one of
    ``supported_resolutions`` proves the value is well-formed; it does not
    make the value govern anything. Counting that as consumption would let a
    setting hide behind its own type check -- which is how three of the keys
    in this repository looked consumed while deciding nothing.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        names = {
            getattr(getattr(d, "func", d), "id", "")
            or getattr(getattr(d, "func", d), "attr", "")
            for d in node.decorator_list
        }
        if any("validator" in name for name in names):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return frozenset(lines)


def _leaves(node: Any, prefix: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, (*prefix, str(key)))
    else:
        yield prefix, node


def scan(config_dir: Path, root: Path) -> list[Leaf]:
    """Every leaf in every shipped configuration file, with its mentions."""
    import yaml

    corpus = read_corpus(root)
    skip = {name: validator_lines(text) for name, text in corpus.declarers.items()}
    found: list[Leaf] = []
    for path in sorted(config_dir.glob("*.yaml")):
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        for keypath, value in _leaves(data):
            key = ".".join(keypath)
            name = keypath[-1]
            # A key that is not an identifier is a free-form map key or a list
            # entry -- there is no attribute anywhere to look for.
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                continue
            if any(pattern.match(key) for pattern in OPEN_MAPS):
                continue
            word = re.compile(rf"\b{re.escape(name)}\b")
            declaration = re.compile(rf"^\s*{re.escape(name)}\s*[:=]")
            # A field read inside its own model's method is consumed, however
            # it may look from the outside.
            used_in_schema = tuple(
                name_of_file
                for name_of_file, text in corpus.declarers.items()
                if any(
                    word.search(line)
                    and not declaration.match(line)
                    and number not in skip.get(name_of_file, frozenset())
                    for number, line in enumerate(text.splitlines(), start=1)
                )
            )
            found.append(
                Leaf(
                    file=path.name,
                    key=key,
                    name=name,
                    value=value,
                    product=tuple(f for f, t in corpus.product.items() if word.search(t))
                    + used_in_schema,
                    tests=tuple(f for f, t in corpus.tests.items() if word.search(t)),
                    schema=used_in_schema,
                )
            )
    return found


def orphans(leaves: list[Leaf]) -> list[Leaf]:
    """Keys the shipped configuration declares and no shipped code reads."""
    return [leaf for leaf in leaves if leaf.orphaned]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="the whole table as JSON")
    parser.add_argument("--all", action="store_true", help="print every key, not only orphans")
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    leaves = scan(root / "config", root)
    missing = orphans(leaves)

    if arguments.json:
        print(
            json.dumps(
                [
                    {
                        "file": leaf.file,
                        "key": leaf.key,
                        "value": leaf.value,
                        "product": len(leaf.product),
                        "tests": len(leaf.tests),
                        "orphaned": leaf.orphaned,
                    }
                    for leaf in leaves
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1 if missing else 0

    print(f"{len(leaves)} configuration keys, {len(missing)} with no consumer\n")
    for leaf in sorted(missing, key=lambda item: (item.file, item.key)):
        marker = "  (tests only)" if leaf.test_only else ""
        print(f"  {leaf.file:18s} {leaf.key:56s} = {leaf.value!r}{marker}")
    if arguments.all:
        print("\nconsumed:")
        for leaf in sorted(leaves, key=lambda item: (item.file, item.key)):
            if not leaf.orphaned:
                print(f"  {leaf.key:56s} {len(leaf.product)} file(s)")
    if missing:
        print(
            "\nEach of these reads as a capability. Wire it, delete it, or give it "
            "a reason in tests/unit/test_config_coverage.py::ALLOWED."
        )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
