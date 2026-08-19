"""Evidence projection (Phase A): what the pipeline recorded about a stretch.

Phase 0 named the constraint this package exists under:

    **No evidence table.** The analysis tables are the evidence; Phase A will
    project over them, not copy them.

So there is no writer, no migration and no new row -- only one read across the
stores every stage already fills, and one definition of what "near" means for
everybody who asks.
"""

from backend.evidence.projection import Evidence, Span, Stores, project

__all__ = ["Evidence", "Span", "Stores", "project"]
