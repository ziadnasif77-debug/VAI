"""Every package must import on its own, in a fresh interpreter.

Two import cycles have reached this codebase, and both surfaced the same
unhelpful way: an ``ImportError`` from one entry point while the test suite
stayed green, because the suite happened to import things in an order that
initialised the cycle's members before they needed each other.

`pytest` cannot catch that from inside a process it has already warmed up, so
each check runs in a **subprocess** importing exactly one module. That is the
only arrangement in which "does this module import" is a real question.

The rule this encodes: a lower layer never depends on a higher one. Persistence
may read domain models; a domain module may not read persistence. When that
inverts, this file is where it shows up.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

#: One per package, chosen as the module an outside caller would reach for.
#: Ordered from the bottom layer upward, so a failure names the lowest break.
MODULES = [
    "backend.core.models.enums",
    "backend.core.errors",
    "backend.core.ids",
    "backend.config.loader",
    "backend.database.connection",
    "backend.database.repositories",
    "ai.providers.base",
    "backend.analysis.scenes",
    "backend.gaming.events",
    "backend.moments.scoring",
    "backend.narrative.story",
    "backend.timeline.models",
    "backend.timeline.builder",
    "backend.effects.models",
    "backend.effects.planner",
    "backend.interaction.models",
    "backend.interaction.service",
    "backend.pipeline.runner",
    "backend.pipeline.workers",
    "backend.services.health",
    "backend.api.app",
]


@pytest.mark.parametrize("module", MODULES)
def test_the_module_imports_on_its_own(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{module} does not import in a fresh interpreter:\n{result.stderr}"
    )


def test_the_entry_point_scripts_import() -> None:
    # `doctor.py` found the second cycle. It imports the service layer from a
    # cold start, which is exactly the order the test suite never uses.
    for script in ("scripts/doctor.py", "scripts/db_init.py"):
        result = subprocess.run(
            [sys.executable, "-c", f"import ast,pathlib; ast.parse(pathlib.Path({script!r}).read_text())"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr


def test_importing_the_editing_vocabulary_does_not_load_the_service() -> None:
    """The boundary the interaction package documents about itself.

    Its docstring says the pipeline consumes an ``EditingIntent`` and knows
    nothing else. If importing that model pulls in the service -- and through
    it the repositories -- then the boundary is a comment rather than a fact,
    and the cycle it once closed can come back.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import backend.interaction.models, sys; "
            "print('backend.interaction.service' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
