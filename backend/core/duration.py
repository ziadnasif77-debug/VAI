"""Output duration policy -- the single definition of the 10-60 minute band.

SPEC sections 6 (limits), 39 (duration optimizer), 91 (central configuration).

Two layers, deliberately:

* :data:`ABSOLUTE_MIN_OUTPUT_SECONDS` / :data:`ABSOLUTE_MAX_OUTPUT_SECONDS` are
  the product rule. They live in code and are not configurable.
* :class:`DurationPolicy` is built from ``config/output.yaml`` and may *narrow*
  that band. A configuration that tries to widen it is rejected at load time,
  so no deployment can produce a 5-minute or 90-minute video.

Everything else -- the API, the duration optimizer, the EDL builder, the QA
pipeline -- reads the policy. The numbers appear nowhere else, except the SQL
``CHECK`` constraint in migration 0001, which a unit test keeps in agreement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

#: Absolute product limits (§6). Not configurable.
ABSOLUTE_MIN_OUTPUT_SECONDS: Final[int] = 600
ABSOLUTE_MAX_OUTPUT_SECONDS: Final[int] = 3600

SECONDS_PER_MINUTE: Final[int] = 60


class TargetDurationError(ValueError):
    """Raised when a requested target duration is outside the permitted band."""


@dataclass(frozen=True, slots=True)
class DurationPolicy:
    """Effective duration rules for one installation.

    Attributes:
        min_seconds / max_seconds: the effective band, always inside the
            absolute product limits.
        presets_seconds: durations offered in the import screen (§6).
        default_seconds: preselected target.
        tolerance_seconds / tolerance_ratio: how close the optimizer must land
            on the requested target (§39). The wider of the two applies.
    """

    min_seconds: int
    max_seconds: int
    presets_seconds: tuple[int, ...]
    default_seconds: int
    tolerance_seconds: float
    tolerance_ratio: float

    def __post_init__(self) -> None:
        if self.min_seconds < ABSOLUTE_MIN_OUTPUT_SECONDS:
            raise ValueError(
                f"output.min_minutes resolves to {self.min_seconds}s, below the product "
                f"minimum of {ABSOLUTE_MIN_OUTPUT_SECONDS}s (10 minutes, §6)."
            )
        if self.max_seconds > ABSOLUTE_MAX_OUTPUT_SECONDS:
            raise ValueError(
                f"output.max_minutes resolves to {self.max_seconds}s, above the product "
                f"maximum of {ABSOLUTE_MAX_OUTPUT_SECONDS}s (60 minutes, §6)."
            )
        if self.min_seconds >= self.max_seconds:
            raise ValueError("output.min_minutes must be below output.max_minutes.")
        if not self.contains(self.default_seconds):
            raise ValueError(
                f"output.default_duration_minutes ({self.default_seconds}s) is outside "
                f"the configured band {self.min_seconds}-{self.max_seconds}s."
            )
        outside = [value for value in self.presets_seconds if not self.contains(value)]
        if outside:
            raise ValueError(
                "output.duration_presets_minutes contains values outside the band: "
                + ", ".join(str(value) for value in outside)
            )
        if self.tolerance_seconds < 0 or self.tolerance_ratio < 0:
            raise ValueError("Duration tolerances must be non-negative.")

    def contains(self, seconds: float) -> bool:
        """Return whether ``seconds`` is inside the effective band."""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return False
        if math.isnan(seconds) or math.isinf(seconds):
            return False
        return self.min_seconds <= seconds <= self.max_seconds

    def validate(self, seconds: float) -> int:
        """Validate a requested target and return it as whole seconds.

        Raises:
            TargetDurationError: if it is not a finite number inside the band.
        """
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TargetDurationError(
                f"Target duration must be a number, got {type(seconds).__name__}."
            )
        if math.isnan(seconds) or math.isinf(seconds):
            raise TargetDurationError("Target duration must be a finite number.")
        if not self.contains(seconds):
            raise TargetDurationError(
                f"Target duration {format_duration(seconds)} is outside the permitted "
                f"range {format_duration(self.min_seconds)}-{format_duration(self.max_seconds)}."
            )
        return round(seconds)

    def clamp(self, seconds: float) -> int:
        """Clamp ``seconds`` into the band.

        The last-resort safety net at the EDL boundary. Reaching it means the
        duration optimizer produced an out-of-band plan, which is a warning-level
        event, not normal operation.
        """
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TargetDurationError(
                f"Target duration must be a number, got {type(seconds).__name__}."
            )
        if math.isnan(seconds):
            raise TargetDurationError("Target duration must be a finite number.")
        return round(min(max(seconds, self.min_seconds), self.max_seconds))

    def tolerance_for(self, target_seconds: float) -> float:
        """Return the accepted deviation around ``target_seconds`` (§39)."""
        return max(self.tolerance_seconds, abs(target_seconds) * self.tolerance_ratio)

    def within_tolerance(self, actual_seconds: float, target_seconds: float) -> bool:
        """Return whether an achieved duration is close enough to the target."""
        return abs(actual_seconds - target_seconds) <= self.tolerance_for(target_seconds)

    def accepts_output(self, seconds: float) -> bool:
        """Return whether a *rendered* file satisfies the band (QA, §76)."""
        return self.contains(seconds)

    @classmethod
    def from_minutes(
        cls,
        *,
        min_minutes: float,
        max_minutes: float,
        presets_minutes: list[int] | tuple[int, ...],
        default_minutes: float,
        tolerance_seconds: float,
        tolerance_ratio: float,
    ) -> DurationPolicy:
        """Build a policy from the minute-based values in ``config/output.yaml``."""
        return cls(
            min_seconds=round(min_minutes * SECONDS_PER_MINUTE),
            max_seconds=round(max_minutes * SECONDS_PER_MINUTE),
            presets_seconds=tuple(
                round(value * SECONDS_PER_MINUTE) for value in presets_minutes
            ),
            default_seconds=round(default_minutes * SECONDS_PER_MINUTE),
            tolerance_seconds=float(tolerance_seconds),
            tolerance_ratio=float(tolerance_ratio),
        )


def format_duration(seconds: float) -> str:
    """Format seconds as ``M:SS`` or ``H:MM:SS`` for logs and the UI."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError(f"Expected a number, got {type(seconds).__name__}.")
    if math.isnan(seconds) or math.isinf(seconds):
        raise ValueError("Cannot format a non-finite duration.")
    sign = "-" if seconds < 0 else ""
    total = round(abs(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{secs:02d}"
    return f"{sign}{minutes}:{secs:02d}"


def parse_duration(text: str) -> float:
    """Parse ``SS``, ``M:SS`` or ``H:MM:SS`` into seconds.

    For CLI arguments and user-entered timestamps only. AI output is validated
    against a schema instead (§93, §94).
    """
    if not isinstance(text, str):
        raise TypeError(f"Expected a string, got {type(text).__name__}.")
    stripped = text.strip()
    if not stripped:
        raise ValueError("Empty duration string.")
    negative = stripped.startswith("-")
    if negative:
        stripped = stripped[1:]

    parts = stripped.split(":")
    if len(parts) > 3:
        raise ValueError(f"Malformed duration {text!r}: too many ':' separated fields.")
    try:
        values = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Malformed duration {text!r}: non-numeric field.") from exc
    if any(value < 0 for value in values):
        raise ValueError(f"Malformed duration {text!r}: negative field.")
    if len(values) > 1 and any(value >= 60 for value in values[1:]):
        raise ValueError(f"Malformed duration {text!r}: minutes/seconds must be below 60.")

    total = 0.0
    for value in values:
        total = total * 60 + value
    return -total if negative else total


__all__ = [
    "ABSOLUTE_MAX_OUTPUT_SECONDS",
    "ABSOLUTE_MIN_OUTPUT_SECONDS",
    "DurationPolicy",
    "TargetDurationError",
    "format_duration",
    "parse_duration",
]
