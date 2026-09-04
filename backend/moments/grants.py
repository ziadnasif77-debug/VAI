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

from collections.abc import Mapping, Sequence

from backend.moments.formation import Moment, replace_moment
from backend.timeline.authorization import (
    MIN_SPAN_SECONDS,
    AuthorizationError,
    AuthorizedSpan,
    Granter,
    issue,
    newest_for,
)

METADATA_KEY = "authorized"


def grant_first_spans(
    moments: Sequence[Moment], exclusions: Sequence[tuple[float, float]] = ()
) -> list[Moment]:
    """Attach the core and context grants to every moment."""
    granted: list[Moment] = []
    for moment in moments:
        events = ", ".join(sorted({event.event_type.value for event in moment.events})) or "none"
        chain = []
        if moment.end_seconds - moment.start_seconds >= MIN_SPAN_SECONDS:
            chain.append(
                issue(
                    moment.media_id,
                    moment.start_seconds,
                    moment.end_seconds,
                    Granter.MOMENT_CORE,
                    f"events: {events}",
                    exclusions=exclusions,
                ).to_dict()
            )
        # A point moment -- an event with no duration of its own, which the
        # benchmark has (a surprise at 680.7 s) -- has no core span to grant;
        # its context is the first grant, and the only one it needs.
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
                    METADATA_KEY: [*chain, context.to_dict()],
                },
            )
        )
    return granted


def spans_of(moment: Moment) -> tuple[AuthorizedSpan, ...]:
    """The chain a moment carries, as objects. Empty for a pre-P0.3 moment."""
    raw = moment.metadata.get(METADATA_KEY) or []
    return tuple(AuthorizedSpan.from_dict(item) for item in raw)


#: Where a narrative step records that it widened a context, and by whose
#: authority, for :func:`grant_widenings` to turn into a span. The steps know
#: nothing of exclusions; the grant does.
WIDENED_KEY = "widened_by"


def note_widening(
    moment: Moment, granter: Granter, *, start: float, end: float, reason: str
) -> Moment:
    """Record that a step wants ``[start, end]`` for this moment, and why.

    The moment's context is set to the wanted bounds here; the grant that
    makes them real -- or cuts them back at an exclusion -- is issued later,
    in one place, by :func:`grant_widenings`.
    """
    marks = list(moment.metadata.get(WIDENED_KEY) or [])
    marks.append(
        {
            "granted_by": granter.value,
            "start": round(float(start), 3),
            "end": round(float(end), 3),
            "reason": reason,
        }
    )
    return replace_moment(
        moment,
        context_start=start,
        context_end=end,
        metadata={**moment.metadata, WIDENED_KEY: marks},
    )


class AuthorizationChainMissingError(AuthorizationError):
    """A moment with no grant reached a stage that needs one."""


def require_chain(moments: Sequence[Moment]) -> None:
    """Every moment must carry its first grants; nothing is backfilled."""
    missing = [moment for moment in moments if not moment.metadata.get(METADATA_KEY)]
    if missing:
        raise AuthorizationChainMissingError(
            f"{len(missing)} of {len(moments)} moments predate authorization; re-run MOMENTS",
            details={"missing": len(missing), "total": len(moments)},
            recoverable=False,
        )


def grant_widenings(
    moments: Sequence[Moment],
    exclusions_by_media: Mapping[str, Sequence[tuple[float, float]]],
) -> list[Moment]:
    """Turn every recorded widening into a new span, and check the result.

    For each moment that carries a chain: every mark left by
    :func:`note_widening` becomes an :class:`AuthorizedSpan` issued against
    the recording's exclusions -- so a widening that reached into a menu is
    cut back at the grant and the context follows the grant. Then the
    governing span must cover the moment's context and its core; a context
    wider than every grant, with no mark to explain it, is a widening by an
    unlisted step and fails hard (brief, PHASE B rule 5).

    A moment with no chain passes through untouched: the pipeline refuses
    those before the engine (:func:`require_chain`); the engine stays usable
    on bare moments in its own tests.
    """
    granted: list[Moment] = []
    for moment in moments:
        chain = list(moment.metadata.get(METADATA_KEY) or [])
        if not chain:
            granted.append(moment)
            continue
        spans = [AuthorizedSpan.from_dict(item) for item in chain]
        exclusions = exclusions_by_media.get(moment.media_id, ())
        for mark in moment.metadata.get(WIDENED_KEY) or []:
            spans.append(
                issue(
                    moment.media_id,
                    float(mark["start"]),
                    float(mark["end"]),
                    Granter(str(mark["granted_by"])),
                    str(mark["reason"]),
                    exclusions=exclusions,
                )
            )
        governing = newest_for(spans, moment.media_id)
        assert governing is not None
        start, end = moment.context_start, moment.context_end
        if not governing.covers(start, end):
            if not (moment.metadata.get(WIDENED_KEY) or []):
                raise AuthorizationError(
                    f"moment {moment.metadata.get('id', '?')} runs [{start:.3f}, {end:.3f}] "
                    f"but its authorization is [{governing.start:.3f}, {governing.end:.3f}] "
                    f"granted by {governing.granted_by.value}, and no listed step widened it",
                    details={"moment": moment.metadata.get("id"), "start": start, "end": end},
                    recoverable=False,
                )
            # A grant cut back at an exclusion: the context follows the grant.
            start, end = max(start, governing.start), min(end, governing.end)
        if not governing.covers(moment.start_seconds, moment.end_seconds):
            raise AuthorizationError(
                f"moment {moment.metadata.get('id', '?')}: its core "
                f"[{moment.start_seconds:.3f}, {moment.end_seconds:.3f}] is outside the span "
                f"[{governing.start:.3f}, {governing.end:.3f}] granted by "
                f"{governing.granted_by.value}",
                details={"moment": moment.metadata.get("id")},
                recoverable=False,
            )
        metadata = {key: value for key, value in moment.metadata.items() if key != WIDENED_KEY}
        metadata[METADATA_KEY] = [span.to_dict() for span in spans]
        granted.append(
            replace_moment(moment, context_start=start, context_end=end, metadata=metadata)
        )
    return granted


__all__ = [
    "METADATA_KEY",
    "WIDENED_KEY",
    "AuthorizationChainMissingError",
    "grant_first_spans",
    "grant_widenings",
    "note_widening",
    "require_chain",
    "spans_of",
]
