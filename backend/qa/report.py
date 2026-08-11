"""QA findings and what they mean (SPEC §76–§79).

A QA engine that only says *something is wrong* has moved the problem rather
than solved it. §79 is explicit that a low-confidence result is marked for
review, and §78 gives the human the last word — so every finding here carries
three things: what was measured, what was expected, and **what to do about it**.

The severity split is a policy, and it is the one §76 and §77 set out:

``FAILED``
    A technical fact about the file that makes it unfit to publish — it does
    not decode, it has no audio, it is thirty seconds long. These block export.

``WARNING``
    A judgement about the *content* — a menu screen in the edit, a long
    silence, a caption over the scoreboard. §78 says the system must never
    assume its own decisions are correct, so these go to a human and are never
    silently acted on.

``PASSED``
    Checked and fine. Kept in the report rather than dropped: "the audio was
    checked and is fine" and "nobody looked at the audio" are different
    statements, and a report that only lists problems cannot tell them apart.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.config.schema import QaConfig
from backend.core.models.enums import QAStatus


@dataclass(frozen=True, slots=True)
class Finding:
    """One check's result.

    ``measured`` and ``expected`` are kept apart from the message so a report
    can be rendered as a table, compared between runs, or checked by a test
    without parsing prose.
    """

    check: str
    status: QAStatus
    category: str = "technical"
    message: str = ""
    #: What to do about it. Required for anything that is not a pass -- §79.
    remedy: str = ""
    measured: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status is QAStatus.FAILED

    @property
    def warned(self) -> bool:
        return self.status is QAStatus.WARNING

    def __str__(self) -> str:
        return f"[{self.status.value}] {self.check}: {self.message}"

    def as_row(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "check_name": self.check,
            "status": self.status.value,
            "detail": self.message,
            "measured": {**self.measured, "remedy": self.remedy},
        }


@dataclass(frozen=True, slots=True)
class QaReport:
    """Everything QA found, and what it means for publishing."""

    findings: tuple[Finding, ...] = ()
    #: True when the policy says these findings should stop an export.
    blocks_export: bool = False
    #: True when §78's human review is warranted before publishing.
    needs_review: bool = False

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.failed)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.warned)

    @property
    def status(self) -> QAStatus:
        if self.failures:
            return QAStatus.FAILED
        if self.warnings:
            return QAStatus.WARNING
        return QAStatus.PASSED

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "checks": len(self.findings),
            "failures": [str(item) for item in self.failures],
            "warnings": [str(item) for item in self.warnings],
            "blocks_export": self.blocks_export,
            "needs_review": self.needs_review,
        }

    def explain(self) -> list[str]:
        """The report as lines a person can act on (§79, §80)."""
        lines: list[str] = []
        for finding in self.findings:
            if finding.status is QAStatus.PASSED:
                continue
            lines.append(str(finding))
            if finding.remedy:
                lines.append(f"    → {finding.remedy}")
        return lines


def build_report(findings: Iterable[Finding], config: QaConfig) -> QaReport:
    """Apply §76's policy to a set of findings.

    The policy is configuration rather than code because "does a warning stop
    a publish" is a deployment's decision, not this module's. What is *not*
    configurable is whether warnings are reported at all.
    """
    collected = tuple(findings)
    failures = [item for item in collected if item.failed]
    warnings = [item for item in collected if item.warned]

    blocks = bool(failures) and config.policy.fail_on_technical_error
    if warnings and config.policy.fail_on_content_warning:
        blocks = True

    # Enough warnings is itself a signal, even when none alone is serious: a
    # video with eight small oddities is more likely wrong than one with two.
    review = bool(warnings) and (
        len(warnings) >= config.policy.max_warnings_before_review
        or config.policy.max_warnings_before_review == 0
    )
    return QaReport(findings=collected, blocks_export=blocks, needs_review=review or bool(failures))


def passed(check: str, message: str, *, category: str = "technical", **measured: Any) -> Finding:
    return Finding(
        check=check,
        status=QAStatus.PASSED,
        category=category,
        message=message,
        measured=measured,
    )


def warning(
    check: str, message: str, *, remedy: str, category: str = "content", **measured: Any
) -> Finding:
    return Finding(
        check=check,
        status=QAStatus.WARNING,
        category=category,
        message=message,
        remedy=remedy,
        measured=measured,
    )


def failure(
    check: str, message: str, *, remedy: str, category: str = "technical", **measured: Any
) -> Finding:
    return Finding(
        check=check,
        status=QAStatus.FAILED,
        category=category,
        message=message,
        remedy=remedy,
        measured=measured,
    )


def enabled_checks(names: Sequence[str], configured: dict[str, bool]) -> list[str]:
    """Which of ``names`` the configuration turns on.

    A check absent from the configuration runs: a new check should take effect
    on upgrade rather than wait for someone to notice it needs enabling.
    """
    return [name for name in names if configured.get(name, True)]


__all__ = [
    "Finding",
    "QaReport",
    "build_report",
    "enabled_checks",
    "failure",
    "passed",
    "warning",
]
