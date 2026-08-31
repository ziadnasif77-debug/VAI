"""Controlled tuning (V2-P10).

The only mechanism here permitted to change a decision without a person asking,
and the most fenced thing in the project because of it. Nothing has ever been
written to its ledger: the switch is off, and even on it refuses until fifteen
videos have been measured.
"""

from backend.tuning.deltas import Delta, RefusedError, TuningLedger

__all__ = ["Delta", "RefusedError", "TuningLedger"]
