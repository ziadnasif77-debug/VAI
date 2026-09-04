"""The first grants: a moment's core and its context, authorised at formation (P0.3).

The moments stage owns the first :class:`~backend.timeline.authorization.AuthorizedSpan`
of every clip that will ever exist. Two spans, in this order:

* ``moment_core`` -- the events' own span (§26–§28). Formation already refused
  a core that touches an exclusion, so this grant is never cut.
* ``context_expansion`` -- the viewing span after §29's expansion and after
  the pull-back out of excluded stretches. Issued against the same exclusions,
  so a context that somehow still reached into one would be cut here rather
  than authorised.

The chain lives in ``Moment.metadata["authorized"]`` as plain dicts, which is
what the moments table persists and what the story stage reads back.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.moments.formation import Moment, replace_moment
from backend.timeline.authorization import AuthorizedSpan, Granter, issue

METADATA_KEY = "authorized"


def grant_first_spans(
    moments: Sequence[Moment], exclusions: Sequence[tuple[float, float]] = ()
) -> list[Moment]:
    """Attach the core and context grants to every moment."""
    granted: list[Moment] = []
    for moment in moments:
        events = ", ".join(sorted({event.event_type.value for event in moment.events})) or "none"
        core = issue(
            moment.media_id,
            moment.start_seconds,
            moment.end_seconds,
            Granter.MOMENT_CORE,
            f"events: {events}",
            exclusions=exclusions,
        )
        before = max(moment.start_seconds - moment.context_start, 0.0)
        after = max(moment.context_end - moment.end_seconds, 0.0)
        context = issue(
            moment.media_id,
            moment.context_start,
            moment.context_end,
            Granter.CONTEXT_EXPANSION,
            f"context expansion: -{before:.1f} s / +{after:.1f} s around the core",
            exclusions=exclusions,
        )
        granted.append(
            replace_moment(
                moment,
                # A context the exclusions cut is a context that reached where
                # the pull-back should have stopped it; the grant is the truth.
                context_start=context.start,
                context_end=context.end,
                metadata={
                    **moment.metadata,
                    METADATA_KEY: [core.to_dict(), context.to_dict()],
                },
            )
        )
    return granted


def spans_of(moment: Moment) -> tuple[AuthorizedSpan, ...]:
    """The chain a moment carries, as objects. Empty for a pre-P0.3 moment."""
    raw = moment.metadata.get(METADATA_KEY) or []
    return tuple(AuthorizedSpan.from_dict(item) for item in raw)


__all__ = ["METADATA_KEY", "grant_first_spans", "spans_of"]
