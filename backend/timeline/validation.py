"""Timeline validation (SPEC §40–§42).

A timeline is a promise about a video that does not exist yet, and the render
is the first thing that would notice the promise is false — three minutes into
an encode, as a seek error with no useful message. So the checks live here and
run before anything is written or rendered.

What is checked, and why each one is a real failure rather than a preference:

**Source bounds.** A clip that reads past the end of its recording cannot be
rendered at all. This is the only check that needs to know something outside
the timeline — the length of each source file — so it degrades to a warning
when that is unknown rather than pretending.

**Gaps and overlaps on the video track.** A gap renders as black frames nobody
asked for; an overlap means two clips claim the same instant, and the renderer
picks one arbitrarily. Both are silent in the output, which is precisely why
they need catching here.

**Index and ordering.** ``clip_index`` is the persisted order, and the database
enforces its uniqueness; the timeline must agree with it, or a reload produces
a different video from the one that was checked.

**Duration band.** §6 is a product rule, and the builder's clamp is the last
resort. If a timeline still sits outside the band after that, the clamp did not
work and the video should not be rendered under the impression it did.

Findings are returned rather than raised, because a caller assembling an EDL
wants all of them at once; :func:`require_valid` raises for the callers that
just need the timeline to be sound.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.core.duration import DurationPolicy
from backend.core.errors import ErrorCode, ValidationError
from backend.core.models.enums import TrackKind
from backend.timeline.models import EPSILON, Timeline, TimelineClip

#: A gap smaller than this is floating-point noise from summing durations, not
#: a hole in the video. One millisecond is below a frame at any sane rate.
GAP_TOLERANCE_SECONDS: float = 0.001


class Severity(str, Enum):
    """How much a finding matters.

    ``ERROR`` means the timeline cannot be rendered as described. ``WARNING``
    means it can, but something about it is likely not what was intended.
    """

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing wrong with a timeline, in terms a user could act on."""

    severity: Severity
    code: str
    message: str
    clip_id: str | None = None
    timeline_seconds: float | None = None

    def __str__(self) -> str:
        where = f" at {self.timeline_seconds:.2f}s" if self.timeline_seconds is not None else ""
        return f"[{self.severity.value}] {self.message}{where}"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Everything wrong with one timeline."""

    findings: tuple[Finding, ...] = ()

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is Severity.WARNING)

    @property
    def is_valid(self) -> bool:
        """Whether the timeline can be rendered. Warnings do not block."""
        return not self.errors

    def summary(self) -> dict[str, object]:
        return {
            "valid": self.is_valid,
            "errors": [str(item) for item in self.errors],
            "warnings": [str(item) for item in self.warnings],
        }


def validate(
    timeline: Timeline,
    *,
    media_durations: Mapping[str, float] | None = None,
    policy: DurationPolicy | None = None,
) -> ValidationReport:
    """Check a timeline against everything that would break the render.

    Args:
        timeline: the timeline to check.
        media_durations: source lengths by media id. Omitting them downgrades
            the bounds check to a warning rather than skipping it silently.
        policy: the §6 band. Omitted when the caller is checking a fragment
            rather than a finished edit.
    """
    findings: list[Finding] = []
    if timeline.is_empty:
        return ValidationReport(
            (Finding(Severity.ERROR, "empty_timeline", "the timeline contains no clips"),)
        )

    for track in timeline.tracks:
        findings.extend(_check_track(track, media_durations))

    if policy is not None:
        findings.extend(_check_duration(timeline, policy))

    return ValidationReport(tuple(findings))


def require_valid(
    timeline: Timeline,
    *,
    media_durations: Mapping[str, float] | None = None,
    policy: DurationPolicy | None = None,
) -> ValidationReport:
    """Validate, and raise :class:`ValidationError` if the timeline cannot render.

    Raises:
        ValidationError: with every error in ``details``, not just the first —
            fixing them one round-trip at a time is the failure mode this
            avoids.
    """
    report = validate(timeline, media_durations=media_durations, policy=policy)
    if not report.is_valid:
        raise ValidationError(
            "The timeline is not renderable: " + "; ".join(str(item) for item in report.errors),
            code=ErrorCode.INVALID_EDL,
            details={"findings": [str(item) for item in report.findings]},
        )
    return report


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _check_track(track, durations: Mapping[str, float] | None) -> list[Finding]:
    clips = track.in_order()
    findings: list[Finding] = []
    findings.extend(_check_indices(clips))
    findings.extend(_check_bounds(clips, durations))
    # Position checks see the render's view: a disabled clip draws nothing, so
    # it can neither leave a gap nor overlap anything. Its stored position is a
    # placeholder that a re-flow will overwrite the moment it is restored.
    visible = tuple(clip for clip in clips if clip.enabled)
    if track.is_contiguous_by_design:
        findings.extend(_check_contiguity(visible))
    else:
        findings.extend(_check_overlaps(visible))
    return findings


def _check_indices(clips: Sequence[TimelineClip]) -> list[Finding]:
    """``clip_index`` is the persisted order and must match the timeline order."""
    findings: list[Finding] = []
    seen: dict[int, str] = {}
    for position, clip in enumerate(clips):
        if clip.clip_index in seen:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "duplicate_index",
                    f"clip_index {clip.clip_index} is used by two clips on the "
                    f"{clip.track.value} track",
                    clip_id=clip.id,
                )
            )
        seen[clip.clip_index] = clip.id
        if clip.clip_index != position:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "index_out_of_order",
                    f"clip at position {position} carries clip_index {clip.clip_index}; "
                    "a reload would produce a different video",
                    clip_id=clip.id,
                    timeline_seconds=clip.timeline_start,
                )
            )
    return findings


def _check_bounds(
    clips: Sequence[TimelineClip], durations: Mapping[str, float] | None
) -> list[Finding]:
    """No clip may read past the end of its recording."""
    findings: list[Finding] = []
    unverified: list[str] = []
    for clip in clips:
        limit = durations.get(clip.media_id) if durations else None
        # A `None` length is a recording nobody has probed yet, not a zero-length
        # one. Callers pass the probe's metadata straight through, and that field
        # is optional; treating it as a number is how this crashes on the one
        # path where it matters.
        if not limit or limit <= 0:
            # One warning per recording, not per clip: twenty clips from one
            # unmeasured file is one thing the caller does not know, not twenty.
            if clip.media_id not in unverified:
                unverified.append(clip.media_id)
            continue
        if clip.source_out > limit + EPSILON:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "source_out_of_range",
                    f"the clip reads to {clip.source_out:.3f}s of a {limit:.3f}s recording",
                    clip_id=clip.id,
                    timeline_seconds=clip.timeline_start,
                )
            )
    findings.extend(
        Finding(
            Severity.WARNING,
            "unverified_bounds",
            f"the length of {media_id} is unknown, so its clips' source ranges "
            "could not be checked",
        )
        for media_id in unverified
    )
    return findings


def _check_contiguity(clips: Sequence[TimelineClip]) -> list[Finding]:
    """A video track must be a continuous run: no gaps, no overlaps."""
    findings: list[Finding] = []
    cursor = 0.0
    for clip in clips:
        delta = clip.timeline_start - cursor
        if delta > GAP_TOLERANCE_SECONDS:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "gap",
                    f"a {delta:.3f}s gap would render as black frames",
                    clip_id=clip.id,
                    timeline_seconds=cursor,
                )
            )
        elif delta < -GAP_TOLERANCE_SECONDS:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "overlap",
                    f"this clip starts {-delta:.3f}s before the previous one ends",
                    clip_id=clip.id,
                    timeline_seconds=clip.timeline_start,
                )
            )
        cursor = max(cursor, clip.timeline_end)
    return findings


def _check_overlaps(clips: Sequence[TimelineClip]) -> list[Finding]:
    """Non-contiguous tracks may have gaps, but never two clips at once."""
    findings: list[Finding] = []
    previous: TimelineClip | None = None
    for clip in clips:
        if previous is not None and clip.timeline_start < previous.timeline_end - EPSILON:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "overlap",
                    f"two {clip.track.value} clips claim {clip.timeline_start:.3f}s",
                    clip_id=clip.id,
                    timeline_seconds=clip.timeline_start,
                )
            )
        previous = clip
    return findings


def _check_duration(timeline: Timeline, policy: DurationPolicy) -> list[Finding]:
    """§6's band, checked after the builder's clamp has had its chance."""
    duration = timeline.duration
    if duration > policy.max_seconds + EPSILON:
        return [
            Finding(
                Severity.ERROR,
                "over_maximum",
                f"the edit is {duration / 60:.1f} minutes, above the "
                f"{policy.max_seconds // 60}-minute maximum (§6)",
            )
        ]
    if duration < policy.min_seconds - EPSILON:
        # A warning, not an error: no amount of editing makes a short recording
        # into a long video, and refusing to render is worse than saying so.
        return [
            Finding(
                Severity.WARNING,
                "under_minimum",
                f"the edit is {duration / 60:.1f} minutes, below the "
                f"{policy.min_seconds // 60}-minute minimum (§6)",
            )
        ]
    return []


def video_track_of(timeline: Timeline):
    """The video track, or ``None``. A convenience for callers that only edit it."""
    return timeline.track(TrackKind.VIDEO)


def _source_start(clip) -> float:
    """Where in the recording this clip begins.

    The rule is checked on two different shapes: a ``PlannedClip`` before the
    EDL is built (``source_start``) and a ``TimelineClip`` after (``source_in``).
    Reading only the first meant the constitution silently could not be applied
    to a finished edit -- which is exactly where V2-P7's corrections land.
    """
    for name in ("source_start", "source_in"):
        value = getattr(clip, name, None)
        if value is not None:
            return float(value)
    raise AttributeError(f"{type(clip).__name__} has no source start to check")


def ensure_chronological(clips: Sequence[Any]) -> None:
    """V2's constitutional rule: chronology is immutable after selection.

    The owner's law, verbatim: no engine may reorder events; a stronger
    moment never precedes what happened before it. The single exception is
    the cold-open hook -- a clip carrying ``role == "hook"`` may open the
    edit as a declared preview of what is coming; anywhere else it obeys
    time like everything.

    The exception is the *leading run* of hook clips, not the first clip.
    V2-P1's walking splitter cuts a shot into pieces, so a hook long enough
    to be split arrives as two or three consecutive pieces -- and stripping
    only index zero left the rest of the hook sitting in the body, where the
    first real clip legitimately precedes it. Every edit built with a hook
    since that phase failed here, correctly by the letter of the check and
    wrongly by its intent. The pieces still have to be in order among
    themselves: a split hook is one preview, not a licence to shuffle.

    Raises ``ValidationError(chronology_violated)`` rather than warning:
    a plan that rewrites time is not a worse plan, it is not a plan this
    product ships.
    """
    body = list(clips)
    opening = 0
    while opening < len(body) and getattr(body[opening], "role", "") == "hook":
        opening += 1
    _in_order(body[:opening], "the cold open")
    body = body[opening:]
    previous_key: tuple[str, float] | None = None
    for clip in body:
        key = (str(clip.media_id), _source_start(clip))
        out_of_order = (
            previous_key is not None
            and key[0] == previous_key[0]
            and key[1] < previous_key[1] - 1e-6
        )
        if out_of_order:
            raise ValidationError(
                "chronology_violated: a clip precedes footage that happened "
                f"before it ({key[1]:.1f}s after {previous_key[1]:.1f}s).",
                code=ErrorCode.INVALID_EDL,
                details={"at_source_seconds": key[1]},
            )
        previous_key = key


def _in_order(clips: Sequence[Any], what: str) -> None:
    """The pieces of one shot, still in the order the recording made them."""
    previous: tuple[str, float] | None = None
    for clip in clips:
        key = (str(clip.media_id), _source_start(clip))
        if previous is not None and key[0] == previous[0] and key[1] < previous[1] - 1e-6:
            raise ValidationError(
                f"chronology_violated: {what} runs backwards "
                f"({key[1]:.1f}s after {previous[1]:.1f}s).",
                code=ErrorCode.INVALID_EDL,
                details={"at_source_seconds": key[1]},
            )
        previous = key


__all__ = [
    "GAP_TOLERANCE_SECONDS",
    "Finding",
    "Severity",
    "ValidationReport",
    "ensure_chronological",
    "require_valid",
    "validate",
    "video_track_of",
]
