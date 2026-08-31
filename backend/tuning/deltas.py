"""Controlled tuning: the ledger, and the six things that must be true (V2-P10).

This is the only mechanism in the project allowed to change a decision without
a person asking. Everything here exists to make that permission small enough to
be safe, and each guard is a separate refusal with its own sentence, because
"the tuner declined" is useless and "the step was 0.31 of a range that allows
0.10" is actionable.

The six:

1. **Bounded.** A key must be one ``style.limits`` declares, and the tuned
   value must land inside it. The fence was built in P8, before the thing it
   fences existed.
2. **Small.** One adjustment may move a value by at most a tenth of its range,
   so no single decision can swing the channel.
3. **Evidenced.** A delta must name the outcomes behind it, and there must be
   at least fifteen. Below that there is nothing to be right about.
4. **Reversible.** The file is never rewritten. A delta is a row, and undoing
   it is marking that row.
5. **Documented.** ``reason`` and ``evidence`` are required. A number that
   changed for reasons nobody wrote down is indistinguishable from a bug.
6. **Fenced by a switch.** ``style.tuning.enabled`` is off, and turning it on
   is a deliberate act that still does not bypass the five above.

And one more that is really about arithmetic: a delta is always relative to the
file, never to the previous delta. Cumulative steps creep -- ten tenths of a
range would leave the fence while every single step looked reasonable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.core.errors import ErrorCode, ValidationError
from backend.core.ids import new_id
from backend.core.logging import LogChannel, get_logger
from backend.database.connection import dumps, loads

logger = get_logger("tuning.deltas", LogChannel.APPLICATION)


@dataclass(frozen=True, slots=True)
class Delta:
    """One recorded adjustment to one style value."""

    id: str
    style: str
    key: str
    delta: float
    base_value: float
    status: str
    reason: str
    videos: int
    created_at: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def value(self) -> float:
        return self.base_value + self.delta

    def describe(self) -> str:
        direction = "up" if self.delta > 0 else "down"
        return (
            f"{self.style}.{self.key} {direction} from {self.base_value:g} to "
            f"{self.value:g} on {self.videos} video(s): {self.reason}"
        )


class RefusedError(ValidationError):
    """A guard said no, and said which one."""


class TuningLedger:
    """Reads and writes ``tuning_deltas``, and refuses more often than not."""

    def __init__(self, database: Any, config: Any) -> None:
        self._db = database
        self._config = config

    # -- reading ------------------------------------------------------------

    def active(self, style: str) -> list[Delta]:
        """Adjustments currently in force for one style."""
        return [
            self._delta(row)
            for row in self._db.fetch_all(
                "SELECT * FROM tuning_deltas WHERE style = ? AND status = 'active' "
                "ORDER BY created_at",
                (style,),
            )
        ]

    def history(self, *, limit: int = 50) -> list[Delta]:
        return [
            self._delta(row)
            for row in self._db.fetch_all(
                "SELECT * FROM tuning_deltas ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def offsets(self, style: str) -> dict[str, float]:
        """``key -> delta`` for what is in force, ready for the resolver."""
        return {item.key: item.delta for item in self.active(style)}

    # -- writing ------------------------------------------------------------

    def apply(
        self,
        *,
        style: str,
        key: str,
        delta: float,
        reason: str,
        evidence: dict[str, Any],
        videos: int,
    ) -> Delta:
        """Record an adjustment, or refuse and say which guard refused.

        Every check here is a hard fence rather than a clamp. Quietly doing
        something smaller than what was asked is how a limit becomes a
        suggestion, and the caller cannot tell the difference between "applied"
        and "applied, but not what you meant".
        """
        tuning = self._config.style.tuning
        if not tuning.enabled:
            raise RefusedError(
                "Controlled tuning is switched off: style.tuning.enabled is false.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "switch"},
                recoverable=False,
            )

        limit = self._config.style.limits.get(key)
        if limit is None:
            raise RefusedError(
                f"{key!r} has no declared range, so nothing may move it. Add a "
                f"bound to style.limits first.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "bounded", "key": key},
                recoverable=False,
            )

        if videos < tuning.minimum_videos:
            raise RefusedError(
                f"{videos} measured video(s); {tuning.minimum_videos} are "
                f"required before a value may move.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "evidence", "videos": videos},
                recoverable=False,
            )

        if not reason.strip() or not evidence:
            raise RefusedError(
                "A tuning change needs a reason and the evidence behind it.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "documented"},
                recoverable=False,
            )

        base = self._base_value(style, key)
        span = float(limit.max) - float(limit.min)
        step = abs(float(delta))
        largest = span * float(tuning.max_step_fraction)
        if step > largest + 1e-9:
            raise RefusedError(
                f"A step of {step:g} is larger than the {largest:g} this key "
                f"allows ({tuning.max_step_fraction:.0%} of its range).",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "small", "step": step, "largest": largest},
                recoverable=False,
            )

        tuned = base + float(delta)
        if not (float(limit.min) - 1e-9 <= tuned <= float(limit.max) + 1e-9):
            raise RefusedError(
                f"{key} would become {tuned:g}, outside its declared range "
                f"{limit.min:g}..{limit.max:g}.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "bounded", "value": tuned},
                recoverable=False,
            )

        waiting = self._cooldown_remaining(style, key, videos)
        if waiting > 0:
            raise RefusedError(
                f"{key} moved recently; {waiting} more measured video(s) before "
                f"it may move again.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "cooldown", "remaining": waiting},
                recoverable=False,
            )

        now = datetime.now(timezone.utc).isoformat()
        delta_id = new_id("job").replace("job-", "tune-")
        with self._db.transaction():
            # One active delta per key: a new one supersedes rather than stacks.
            self._db.execute(
                "UPDATE tuning_deltas SET status = 'superseded', ended_at = ? "
                "WHERE style = ? AND key = ? AND status = 'active'",
                (now, style, key),
            )
            self._db.execute(
                "INSERT INTO tuning_deltas (id, style, key, delta, base_value, "
                "status, reason, evidence, videos, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    delta_id,
                    style,
                    key,
                    float(delta),
                    base,
                    reason.strip(),
                    dumps(evidence),
                    int(videos),
                    now,
                ),
            )
        record = Delta(
            id=delta_id,
            style=style,
            key=key,
            delta=float(delta),
            base_value=base,
            status="active",
            reason=reason.strip(),
            videos=int(videos),
            created_at=now,
            evidence=evidence,
        )
        logger.warning(
            "A style value was changed by controlled tuning",
            extra={
                "style": style,
                "key": key,
                "from": base,
                "to": record.value,
                "videos": videos,
            },
        )
        return record

    def revert(self, delta_id: str) -> bool:
        """Undo one adjustment. The file was never touched, so this is enough."""
        cursor = self._db.execute(
            "UPDATE tuning_deltas SET status = 'reverted', ended_at = ? "
            "WHERE id = ? AND status = 'active'",
            (datetime.now(timezone.utc).isoformat(), delta_id),
        )
        undone = bool(getattr(cursor, "rowcount", 0))
        if undone:
            logger.warning("A tuning change was reverted", extra={"delta": delta_id})
        return undone

    def revert_all(self, style: str | None = None) -> int:
        """Put everything back to what the file says.

        The one operation that must always work, whatever state the ledger is
        in: a mechanism that can change the channel needs a way back that takes
        one command and no thought.
        """
        now = datetime.now(timezone.utc).isoformat()
        if style:
            cursor = self._db.execute(
                "UPDATE tuning_deltas SET status = 'reverted', ended_at = ? "
                "WHERE status = 'active' AND style = ?",
                (now, style),
            )
        else:
            cursor = self._db.execute(
                "UPDATE tuning_deltas SET status = 'reverted', ended_at = ? "
                "WHERE status = 'active'",
                (now,),
            )
        undone = int(getattr(cursor, "rowcount", 0))
        if undone:
            logger.warning(
                "Every tuning change was reverted", extra={"count": undone}
            )
        return undone

    # -- internals ----------------------------------------------------------

    def _base_value(self, style: str, key: str) -> float:
        """What ``config/style.yaml`` says today for this key."""
        from backend.config.schema import _style_values

        entry = self._config.style.bible.get(style)
        if entry is None:
            raise RefusedError(
                f"There is no style named {style!r} to tune.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "bounded", "style": style},
                recoverable=False,
            )
        values = _style_values(entry)
        if key not in values:
            raise RefusedError(
                f"{style!r} has no value called {key!r}.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"guard": "bounded", "key": key},
                recoverable=False,
            )
        return float(values[key])

    def _cooldown_remaining(self, style: str, key: str, videos: int) -> int:
        """How many more measured videos before this key may move again."""
        row = self._db.fetch_one(
            "SELECT videos FROM tuning_deltas WHERE style = ? AND key = ? "
            "AND status = 'active' ORDER BY created_at DESC LIMIT 1",
            (style, key),
        )
        if row is None:
            return 0
        needed = int(row["videos"]) + int(self._config.style.tuning.cooldown_videos)
        return max(0, needed - int(videos))

    def _delta(self, row: Any) -> Delta:
        return Delta(
            id=row["id"],
            style=row["style"],
            key=row["key"],
            delta=float(row["delta"]),
            base_value=float(row["base_value"]),
            status=row["status"],
            reason=row["reason"],
            videos=int(row["videos"]),
            created_at=row["created_at"],
            evidence=loads(row["evidence"] or "{}") or {},
        )


__all__ = ["Delta", "RefusedError", "TuningLedger"]
