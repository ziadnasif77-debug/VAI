"""The timeline: clips, tracks, transitions, markers (SPEC §40, §41).

§40 says every editing decision becomes structured data, and §42 says the
original video is never modified. Together those give one rule that shapes
everything here: **a clip is a reference, not footage.** It names a source file
and two timestamps in it, plus where it sits in the finished video. Nothing in
this module reads, decodes or writes a frame.

The models are engine-neutral on purpose (§67). A clip does not know whether
FFmpeg or Remotion will realise it, and neither does a transition -- the
renderer is chosen in Phase 10 and asks the timeline what to draw. If a field
here ever needs a renderer's name to be understood, it belongs in the renderer.

Two coordinate systems appear throughout, and confusing them is the defect this
module is arranged to prevent:

``source_in`` / ``source_out``
    Seconds **inside the original recording**. Never shifted by editing.
``timeline_start`` / ``timeline_end``
    Seconds **inside the finished video**. Re-flowed whenever clips are added,
    removed or reordered.

Both are stored, rather than one derived from the other, because both are asked
about: "which part of the recording is this" and "when does the viewer see it".
The invariant that ties them together -- equal durations, modulo ``speed`` -- is
checked by the model rather than assumed.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.models.enums import MomentType, TrackKind, TransitionType

#: Floating-point slack for timeline arithmetic. Positions are seconds with
#: millisecond meaning, so anything below this is rounding, not a gap.
EPSILON: float = 1e-6


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TimelineClip(_Base):
    """One span of one recording, placed in the finished video (§40, §42).

    The clip carries the identity of what it came from -- the media, and the
    moment that justified it -- so every second of output can be traced back to
    the analysis that put it there (§80). A clip with no ``moment_id`` is one a
    user added by hand, which is a legitimate state and not an error.
    """

    id: str
    media_id: str
    moment_id: str | None = None

    track: TrackKind = TrackKind.VIDEO
    clip_index: int = Field(ge=0)

    source_in: float = Field(ge=0)
    source_out: float = Field(gt=0)
    timeline_start: float = Field(ge=0)
    timeline_end: float = Field(gt=0)

    #: 1.0 is real time. Slow motion is < 1.0 and stretches the clip on the
    #: timeline; the source span it reads is unchanged.
    speed: float = Field(default=1.0, gt=0)
    transition_in: TransitionType = TransitionType.CUT
    transition_out: TransitionType = TransitionType.CUT

    #: A disabled clip stays in the EDL and is skipped by the renderer, which
    #: is what makes "remove that bit" undoable (§78).
    enabled: bool = True

    moment_type: MomentType | None = None
    score: float = 0.0
    #: Where the narrative put it: ``hook``, ``body``, or a §35 beat name.
    role: str = "body"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_spans(self) -> TimelineClip:
        if self.source_out <= self.source_in:
            raise ValueError(
                f"clip {self.id}: source_out ({self.source_out}) must exceed "
                f"source_in ({self.source_in})"
            )
        if self.timeline_end <= self.timeline_start:
            raise ValueError(
                f"clip {self.id}: timeline_end ({self.timeline_end}) must exceed "
                f"timeline_start ({self.timeline_start})"
            )
        expected = (self.source_out - self.source_in) / self.speed
        span = self.timeline_end - self.timeline_start
        # A time-warping effect adds timeline seconds a linear speed cannot
        # express: a freeze holds one frame, a ramp slows a window. The re-lay
        # (backend.timeline.retime) records the added seconds in metadata, and
        # the invariant widens to a band rather than vanishing -- the span may
        # sit anywhere between "no warp survived" and "the whole warp is here",
        # because a §127 split copies the metadata to both halves while only
        # one of them keeps the warp, and a strict equality made the *load* of
        # such an edit fail rather than the edit itself.
        extra = _retime_extra(self.metadata)
        if not (expected - 1e-3 <= span <= expected + extra + 1e-3):
            # The two coordinate systems have drifted apart, which means the
            # renderer would either run out of source or repeat frames. Catching
            # it here is the whole reason both spans are stored.
            raise ValueError(
                f"clip {self.id}: {self.source_duration:.3f}s of source at speed "
                f"{self.speed} cannot fill {self.duration:.3f}s of timeline"
            )
        return self

    @property
    def source_duration(self) -> float:
        """Seconds read from the recording."""
        return self.source_out - self.source_in

    @property
    def duration(self) -> float:
        """Seconds occupied in the finished video."""
        return self.timeline_end - self.timeline_start

    def contains(self, timeline_seconds: float) -> bool:
        return self.timeline_start - EPSILON <= timeline_seconds <= self.timeline_end + EPSILON

    def source_at(self, timeline_seconds: float) -> float:
        """The source timestamp shown at a timeline position.

        The mapping the renderer needs, and the mapping a question like "what
        was happening at 4:12 of the video" needs (§59). A time-warped clip
        (``metadata["retime"]``, written by :mod:`backend.timeline.retime`)
        maps piecewise: during a freeze the answer is the held instant, after
        it everything is later by the hold. The linear form here would return
        source positions past ``source_out`` for the warp-extended tail --
        and a §127 split at such a position wrote a clip whose source span
        ran backwards, which the model then refused to load.
        """
        offset = max(0.0, min(self.duration, timeline_seconds - self.timeline_start))
        raw = self.metadata.get("retime")
        if isinstance(raw, dict) and _retime_extra(self.metadata) > 0.0:
            offset = _source_offset_of_warped(raw, offset, self.source_duration)
        return min(self.source_in + offset * self.speed, self.source_out)

    def moved_to(self, timeline_start: float) -> TimelineClip:
        """The same source span, placed at a different timeline position."""
        return self.model_copy(
            update={
                "timeline_start": timeline_start,
                "timeline_end": timeline_start + self.duration,
            }
        )


def _source_offset_of_warped(raw: dict[str, Any], offset: float, source_duration: float) -> float:
    """Output-clock clip offset to source-clock clip offset, under a warp.

    The inverse of ``backend.timeline.retime.output_offset``, expressed on the
    stored metadata directly because that module imports this one. During a
    freeze the source clock stands still at the anchor; inside a ramp's slow
    window it advances at ``factor``; past either, it lags by the added time.
    """

    def _number(key: str, fallback: float) -> float:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return fallback

    at = min(max(_number("at", source_duration), 0.0), source_duration)
    extra = max(_number("extra_seconds", 0.0), 0.0)
    if offset <= at:
        return offset
    if str(raw.get("effect")) == "speed_ramp":
        factor = _number("factor", 0.0)
        window = _number("window_seconds", 0.0)
        if 0.0 < factor < 1.0 and window > 0.0:
            stretched_end = at + window / factor
            if offset < stretched_end:
                return at + (offset - at) * factor
            return offset - (stretched_end - (at + window))
    if offset < at + extra:
        return at
    return offset - extra


def _retime_extra(metadata: dict[str, Any]) -> float:
    """The timeline seconds a stored warp is allowed to add. Zero without one.

    Read directly rather than through :mod:`backend.timeline.retime` because
    that module imports this one; the key and shape are its contract.
    """
    raw = metadata.get("retime")
    if not isinstance(raw, dict):
        return 0.0
    value = raw.get("extra_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(float(value), 0.0)
    return 0.0


class Marker(_Base):
    """A labelled point on the timeline (§41).

    Markers are how the pipeline leaves a note for a human: the hook, the
    climax, a QA warning's location. They never affect rendering.
    """

    id: str
    timeline_seconds: float = Field(ge=0)
    label: str
    kind: str = "note"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Track(_Base):
    """One lane of the timeline (§41).

    Tracks are typed because the rules differ by type: a video track must be
    contiguous -- gaps in it are black frames nobody asked for -- while a
    caption track is expected to have silence between captions.
    """

    kind: TrackKind
    name: str = ""
    clips: tuple[TimelineClip, ...] = ()
    enabled: bool = True

    @property
    def is_contiguous_by_design(self) -> bool:
        """Whether a gap on this track is a defect rather than a pause."""
        return self.kind in {TrackKind.VIDEO, TrackKind.AUDIO}

    @property
    def duration(self) -> float:
        return max((clip.timeline_end for clip in self.clips), default=0.0)

    @property
    def active_clips(self) -> tuple[TimelineClip, ...]:
        return tuple(clip for clip in self.clips if clip.enabled)

    def in_order(self) -> tuple[TimelineClip, ...]:
        return tuple(sorted(self.clips, key=lambda clip: clip.timeline_start))

    def clip_at(self, timeline_seconds: float) -> TimelineClip | None:
        for clip in self.active_clips:
            if clip.contains(timeline_seconds):
                return clip
        return None

    def with_clips(self, clips: Iterable[TimelineClip]) -> Track:
        return self.model_copy(update={"clips": tuple(clips)})


class Timeline(_Base):
    """The finished video, described rather than rendered (§40–§42).

    This is the object Phase 9 composites over, Phase 10 encodes, and Phase 11
    checks. It is also what §127 makes re-editing cheap with: changing an edit
    rewrites this and re-renders, and never re-analyses the source.
    """

    project_id: str
    tracks: tuple[Track, ...] = ()
    markers: tuple[Marker, ...] = ()
    #: What the narrative asked for, kept so a later pass can tell whether the
    #: timeline still matches the request it came from.
    target_seconds: float = 0.0
    notes: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration(self) -> float:
        """Length of the finished video: the end of the last enabled video clip.

        Disabled clips are excluded because a disabled clip renders as nothing,
        and a duration that counts footage the viewer never sees is a lie the
        QA phase would then check against.
        """
        video = self.track(TrackKind.VIDEO)
        if video is None:
            return max((track.duration for track in self.tracks), default=0.0)
        return max((clip.timeline_end for clip in video.active_clips), default=0.0)

    @property
    def is_empty(self) -> bool:
        return not any(track.clips for track in self.tracks)

    @property
    def clips(self) -> tuple[TimelineClip, ...]:
        """Every clip on every track, in timeline order."""
        return tuple(
            sorted(
                (clip for track in self.tracks for clip in track.clips),
                key=lambda clip: (clip.timeline_start, clip.track.value),
            )
        )

    def track(self, kind: TrackKind) -> Track | None:
        for track in self.tracks:
            if track.kind is kind:
                return track
        return None

    def video_clips(self, *, enabled_only: bool = True) -> tuple[TimelineClip, ...]:
        """The video track in order -- the spine every other track hangs off."""
        video = self.track(TrackKind.VIDEO)
        if video is None:
            return ()
        clips = video.in_order()
        return tuple(clip for clip in clips if clip.enabled) if enabled_only else clips

    def clip(self, clip_id: str) -> TimelineClip | None:
        for clip in self.clips:
            if clip.id == clip_id:
                return clip
        return None

    def source_at(self, timeline_seconds: float) -> tuple[str, float] | None:
        """Which recording, and where in it, is on screen at a given moment.

        Returns ``(media_id, source_seconds)``, or ``None`` past the end.
        """
        for clip in self.video_clips():
            if clip.contains(timeline_seconds):
                return clip.media_id, clip.source_at(timeline_seconds)
        return None

    def media_ids(self) -> tuple[str, ...]:
        """Every recording the finished video draws on, first appearance first."""
        seen: list[str] = []
        for clip in self.video_clips():
            if clip.media_id not in seen:
                seen.append(clip.media_id)
        return tuple(seen)

    def with_track(self, track: Track) -> Timeline:
        """Replace the track of this kind, or add it (§42: nothing mutates)."""
        others = tuple(existing for existing in self.tracks if existing.kind is not track.kind)
        ordered = sorted((*others, track), key=lambda item: _TRACK_ORDER.index(item.kind))
        return self.model_copy(update={"tracks": tuple(ordered)})

    def with_markers(self, markers: Sequence[Marker]) -> Timeline:
        return self.model_copy(update={"markers": tuple(markers)})

    def summary(self) -> dict[str, Any]:
        video = self.video_clips()
        return {
            "clips": len(video),
            "clips_total": len(self.clips),
            "duration_seconds": round(self.duration, 3),
            "target_seconds": round(self.target_seconds, 2),
            "tracks": [track.kind.value for track in self.tracks if track.clips],
            "media": list(self.media_ids()),
            "markers": len(self.markers),
            "notes": list(self.notes),
        }

    def __iter__(self) -> Iterator[TimelineClip]:  # type: ignore[override]
        return iter(self.video_clips())


#: Render order, bottom to top. Only used to keep tracks in a stable sequence;
#: no renderer is named, and none is implied beyond "overlays draw last".
_TRACK_ORDER: tuple[TrackKind, ...] = (
    TrackKind.VIDEO,
    TrackKind.AUDIO,
    TrackKind.MUSIC,
    TrackKind.EFFECT,
    TrackKind.OVERLAY,
    TrackKind.CAPTION,
)


__all__ = [
    "EPSILON",
    "Marker",
    "Timeline",
    "TimelineClip",
    "Track",
]
