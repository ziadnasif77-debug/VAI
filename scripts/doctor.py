#!/usr/bin/env python3
"""Environment check. Run before trusting the pipeline on a new machine.

    python scripts/doctor.py

Exits 1 only when a *required* dependency is missing. A missing GPU or model is
reported as a warning: the pipeline falls back to CPU and degrades gracefully.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.loader import load_config
from backend.core.models.enums import HealthStatus
from backend.services.health import HealthService

_SYMBOLS = {
    HealthStatus.OK: "OK  ",
    HealthStatus.WARNING: "WARN",
    HealthStatus.FAILED: "FAIL",
    HealthStatus.SKIPPED: "SKIP",
}


def main() -> int:
    config = load_config()
    report = HealthService(config).report()

    print(f"AI Gaming Video Editor {report.application_version}")
    print(f"Hardware profile: {report.hardware_profile.value}\n")

    for check in report.checks:
        marker = _SYMBOLS[check.status]
        requirement = "required" if check.required else "optional"
        print(f"[{marker}] {check.name:<10} ({requirement}) {check.detail}")
        if check.remediation and check.status is not HealthStatus.OK:
            print(f"         -> {check.remediation}")

    print(f"\nOverall: {report.status.value.upper()}")
    blocking = report.blocking_failures
    if blocking:
        print("\nThe pipeline cannot run until these are fixed:")
        for check in blocking:
            print(f"  - {check.name}: {check.detail}")
        return 1
    if report.warnings:
        print(f"\n{len(report.warnings)} warning(s); the pipeline will run with fallbacks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
