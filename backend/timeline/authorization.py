"""AuthorizedSpan: every planned clip's source bounds, and who granted them (P0.3).

The contract from ``docs/BRIEF_P0.md`` (PHASE B and AUTHORIZED SPAN IDENTITY),
built on the structures the pipeline already has rather than beside them. A
moment's core and its expanded context are the first grant; every later
widening -- the duration optimizer reaching for its target, refinement
stretching a cut out of a spoken word, the screen guard rescuing a moment to
the piece floor, a person trimming outward -- is a **new** span with its own
``granted_by`` and a reason, never an edit to an old one. Narrowing needs no
grant. The final EDL is validated against these spans, not against the
recording, and a clip outside its authorization is a hard failure.

Three rules that are not in the brief's list but follow from P0.2, and are
enforced here at issuance:

* **No grant reaches into an exclusion.** Whoever is granting, a span that
  intersects a stretch the exclusion layer refused is cut back to the largest
  gameplay piece of itself, and refused outright when nothing remains. The
  exclusions are the mirror of authorization: what may be shown is bounded by
  what was seen to be the game.
* **The granter set is closed.** ``granted_by`` is a member of
  :class:`Granter` or the span does not exist. A string that happens to spell
  a member's value is refused too, so no caller can mint one by typing it.
* **J/L audio leads are audio.** A :attr:`Granter.JL_CUT` span authorises
  sound only, never a change to the picture's bounds, and is capped by the
  configured lead the same way the planner is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from backend.core.errors import ErrorCode, ValidationError


class Granter(str, Enum):
    """Who may issue an authorized span. Closed: derived from PLAN.md / SPEC.md.

    * ``moment_core`` -- §26–§28: the events' own span, the first grant.
    * ``context_expansion`` -- §29: adaptive pre-roll and post-roll.
    * ``duration_optimizer`` -- §39: context extended towards the target.
    * ``refinement`` -- §29 "adaptive" and PLAN.md 2026-08-14: a cut moved
      out of mid-speech, stretched only in the direction that adds context.
    * ``screen_guard_rescue`` -- §77 and PLAN.md 2026-08-29 (wave 1): a
      zero-piece moment widened to the piece floor inside stillness.
    * ``human`` -- §78 and §42: a person editing the timeline.
    * ``jl_cut`` -- PLAN.md 2026-08-27 (the J/L planner): audio only.

    Not granters, by the brief: style, the Critic, jump-cut logic, and any
    caller not listed here. The exclusion layer, the clamp and the
    exclusivity guard only narrow and need no grant.
    """

    MOMENT_CORE = "moment_core"
    CONTEXT_EXPANSION = "context_expansion"
    DURATION_OPTIMIZER = "duration_optimizer"
    REFINEMENT = "refinement"
    SCREEN_GUARD_RESCUE = "screen_guard_rescue"
    HUMAN = "human"
    JL_CUT = "jl_cut"


#: Granters whose spans authorise sound only. The picture's bounds are never
#: theirs to move.
AUDIO_ONLY_GRANTERS: Final[frozenset[Granter]] = frozenset({Granter.JL_CUT})

#: Below this a span is a rounding artefact, not footage.
MIN_SPAN_SECONDS: Final[float] = 0.001


class AuthorizationError(ValidationError):
    """A span that no listed granter issued, or a clip outside its grant."""

    default_code = ErrorCode.INVALID_EDL


@dataclass(frozen=True, slots=True)
class AuthorizedSpan:
    """Source bounds one granter vouched for, and why. Immutable.

    ``start`` and ``end`` are seconds in ``media_id``'s recording. A clip may
    lie anywhere inside; a clip that reaches outside needs a new span from a
    listed granter, never a change to this one.
    """

    media_id: str
    start: float
    end: float
    granted_by: Granter
    reason: str
    audio_only: bool = False

    def __post_init__(self) -> None:
        if type(self.granted_by) is not Granter:
            raise AuthorizationError(
                f"{self.granted_by!r} is not a listed granter; the set is closed: "
                + ", ".join(member.value for member in Granter),
                details={"granted_by": repr(self.granted_by)},
                recoverable=False,
            )
        if not self.media_id:
            raise AuthorizationError("an authorized span names its recording")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise AuthorizationError(
                f"a span granted by {self.granted_by.value} must say why",
                details={"granted_by": self.granted_by.value},
            )
        if not (self.end - self.start >= MIN_SPAN_SECONDS):
            raise AuthorizationError(
                f"an authorized span must run forwards: {self.start} -> {self.end}",
                details={"start": self.start, "end": self.end},
            )
        if self.audio_only != (self.granted_by in AUDIO_ONLY_GRANTERS):
            raise AuthorizationError(
                f"{self.granted_by.value} "
                + ("authorises audio only" if self.granted_by in AUDIO_ONLY_GRANTERS
                   else "authorises picture and cannot be audio-only"),
                details={"granted_by": self.granted_by.value},
            )

    @property
    def seconds(self) -> float:
        return self.end - self.start

    def covers(self, start: float, end: float, *, tolerance: float = MIN_SPAN_SECONDS) -> bool:
        """Whether ``[start, end]`` lies inside this span (within rounding)."""
        return start >= self.start - tolerance and end <= self.end + tolerance

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "granted_by": self.granted_by.value,
            "reason": self.reason,
            **({"audio_only": True} if self.audio_only else {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AuthorizedSpan:
        """Read a stored span. A value that is not a listed granter fails here too."""
        raw = payload.get("granted_by")
        try:
            granter = Granter(raw)
        except ValueError as error:
            raise AuthorizationError(
                f"{raw!r} is not a listed granter; the set is closed: "
                + ", ".join(member.value for member in Granter),
                details={"granted_by": repr(raw)},
                recoverable=False,
            ) from error
        return cls(
            media_id=str(payload["media_id"]),
            start=float(payload["start"]),
            end=float(payload["end"]),
            granted_by=granter,
            reason=str(payload.get("reason", "")),
            audio_only=bool(payload.get("audio_only", False)),
        )


def issue(
    media_id: str,
    start: float,
    end: float,
    granted_by: Granter,
    reason: str,
    *,
    exclusions: Sequence[tuple[float, float]] = (),
    audio_only: bool = False,
) -> AuthorizedSpan:
    """Grant a span, cut back to the largest piece outside the exclusions.

    Raises:
        AuthorizationError: when the granter is not listed, or when nothing of
            the requested span lies outside the exclusions -- no one may
            authorise a menu, whoever they are.
    """
    if type(granted_by) is not Granter:
        raise AuthorizationError(
            f"{granted_by!r} is not a listed granter; the set is closed: "
            + ", ".join(member.value for member in Granter),
            details={"granted_by": repr(granted_by)},
            recoverable=False,
        )
    pieces = [(float(start), float(end))]
    for lo, hi in sorted(exclusions):
        next_pieces: list[tuple[float, float]] = []
        for piece_start, piece_end in pieces:
            if hi <= piece_start or lo >= piece_end:
                next_pieces.append((piece_start, piece_end))
                continue
            if piece_start < lo:
                next_pieces.append((piece_start, lo))
            if hi < piece_end:
                next_pieces.append((hi, piece_end))
        pieces = next_pieces
    pieces = [(a, b) for a, b in pieces if b - a >= MIN_SPAN_SECONDS]
    if not pieces:
        raise AuthorizationError(
            f"{granted_by.value} asked for [{start:.3f}, {end:.3f}] of {media_id}, and none of "
            "it is outside the excluded content; no granter may authorise into an exclusion",
            details={"granted_by": granted_by.value, "start": start, "end": end},
            recoverable=False,
        )
    best_start, best_end = max(pieces, key=lambda piece: piece[1] - piece[0])
    cut = (best_start > start + MIN_SPAN_SECONDS) or (best_end < end - MIN_SPAN_SECONDS)
    return AuthorizedSpan(
        media_id=media_id,
        start=best_start,
        end=best_end,
        granted_by=granted_by,
        reason=reason if not cut else f"{reason}; cut back to gameplay by the exclusions",
        audio_only=audio_only,
    )


def newest_for(spans: Iterable[AuthorizedSpan], media_id: str, *, audio: bool = False):
    """The span that governs ``media_id`` now: the last one granted for it.

    Picture bounds are governed by the last picture span; audio-only spans
    are consulted only when ``audio`` is asked for.
    """
    chosen = None
    for span in spans:
        if span.media_id != media_id:
            continue
        if span.audio_only and not audio:
            continue
        chosen = span
    return chosen


def check_clip(
    clip_media_id: str,
    source_in: float,
    source_out: float,
    spans: Sequence[AuthorizedSpan],
    *,
    label: str = "clip",
) -> list[str]:
    """Why this clip is not inside its authorization; empty when it is."""
    if not spans:
        return [f"{label} carries no authorized span"]
    governing = newest_for(spans, clip_media_id)
    if governing is None:
        return [f"{label} has no authorized span for {clip_media_id}"]
    if not governing.covers(source_in, source_out):
        return [
            f"{label} runs [{source_in:.3f}, {source_out:.3f}] but its authorization is "
            f"[{governing.start:.3f}, {governing.end:.3f}] granted by "
            f"{governing.granted_by.value} ({governing.reason})"
        ]
    return []


def spans_from_metadata(metadata: Mapping[str, Any] | None) -> tuple[AuthorizedSpan, ...]:
    """The spans a stored clip carries under ``metadata["authorized"]``."""
    raw = (metadata or {}).get("authorized")
    if not isinstance(raw, list):
        return ()
    return tuple(AuthorizedSpan.from_dict(item) for item in raw if isinstance(item, Mapping))


__all__ = [
    "AUDIO_ONLY_GRANTERS",
    "AuthorizationError",
    "AuthorizedSpan",
    "Granter",
    "check_clip",
    "issue",
    "newest_for",
    "spans_from_metadata",
]
