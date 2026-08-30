"""The finished edit, described so a model can read it (Phase E).

The Critic does not watch a video. It reads the edit the way an editor reads a
timeline: a numbered list of clips, each with what is on screen, what is said,
how long it runs, what it is doing there, and what will be drawn over it.

Everything in that list has already been produced and stored. The vision pass
described these exact frames (§15), the transcript has the words (§14), the
correlator named the events (§26), and the EDL knows the spans. Nothing here
decodes a frame -- which is the same rule the content QA module states for the
same reason: a second pass would cost more than the render and would arrive
with none of the context the first one built.

What makes this an edit review rather than another analysis pass is the
question each row answers. Not "what is in this recording" but "what did the
viewer just watch, and what do they see next".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai.providers.base import StoredObservation
from backend.core.duration import format_duration
from backend.effects.models import EffectInstance
from backend.gaming.episodes import read
from backend.timeline.captions import Caption
from backend.timeline.models import Timeline, TimelineClip

#: Longest stretch of speech shown per clip. A model given four hundred words
#: about one clip answers about the words and forgets the picture.
_SPEECH_CHARACTERS: int = 220

#: How many vision descriptions to show per clip. The frames inside a clip
#: mostly agree with each other; three is enough to show a change.
_DESCRIPTIONS_PER_CLIP: int = 3

#: How much of one description to show. The vision model writes a paragraph per
#: frame -- measured on a real project, 500 characters each. Three of those on
#: each of eleven clips is sixteen thousand characters of prompt, most of it
#: the same room described again. The opening clause is what identifies the
#: shot; the rest is elaboration.
_DESCRIPTION_CHARACTERS: int = 180


@dataclass(frozen=True, slots=True)
class ClipEvidence:
    """One row of the edit, as the Critic sees it."""

    index: int
    clip: TimelineClip
    starts_at: float
    seconds: float
    labels: tuple[str, ...] = ()
    descriptions: tuple[str, ...] = ()
    speech: str = ""
    events: tuple[str, ...] = ()
    captions: int = 0
    effects: tuple[str, ...] = ()

    def line(self) -> str:
        """One numbered line, terse and entirely factual.

        Terse because a local 7B model reading twenty rich paragraphs answers
        about the first three. Factual because prose about how exciting a clip
        is would be the scorer's opinion arriving as if it were an observation
        -- and the Critic is being asked for a second opinion, not an echo.
        """
        parts = [
            f"{self.index}. [{format_duration(self.starts_at)}] "
            f"{format_duration(self.seconds)} {self.clip.role}"
        ]
        if self.clip.moment_type is not None:
            parts.append(self.clip.moment_type.value)
        if self.labels:
            parts.append("on screen: " + ", ".join(self.labels))
        if self.events:
            parts.append("events: " + ", ".join(self.events))
        if self.descriptions:
            parts.append("seen: " + " | ".join(self.descriptions))
        elif not self.labels and not self.speech:
            # Not a formatting nicety. A clip with no observations, no words
            # and no events is one nobody looked at -- the Phase 0 gap arriving
            # in the edit -- and "nothing is recorded here" is a different
            # statement from "nothing happens here". The Critic should be able
            # to tell them apart, so it is said rather than left as a silence
            # in the middle of a numbered list.
            parts.append("nothing was analysed inside this clip")
        if self.speech:
            parts.append(f'said: "{self.speech}"')
        if self.captions:
            parts.append(f"{self.captions} caption(s)")
        if self.effects:
            parts.append("effects: " + ", ".join(self.effects))
        return "\n   ".join(parts)


@dataclass(frozen=True, slots=True)
class EditEvidence:
    """The whole edit: the rows, and the numbers that describe the shape."""

    clips: tuple[ClipEvidence, ...]
    total_seconds: float
    target_seconds: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.clips

    def render(self) -> str:
        return "\n".join(evidence.line() for evidence in self.clips)

    def summary(self) -> dict[str, Any]:
        return {
            "clips": len(self.clips),
            "total_seconds": round(self.total_seconds, 2),
            "target_seconds": round(self.target_seconds, 2),
            "silent_clips": sum(1 for clip in self.clips if not clip.speech),
        }


def gather(
    timeline: Timeline,
    *,
    target_seconds: float,
    observations: Mapping[str, Sequence[StoredObservation]] | None = None,
    speech: Mapping[str, Sequence[Any]] | None = None,
    events: Mapping[str, Sequence[Any]] | None = None,
    captions: Sequence[Caption] = (),
    effects: Sequence[EffectInstance] = (),
    notes: Sequence[str] = (),
) -> EditEvidence:
    """Describe ``timeline`` from what the analysis already stored.

    Args:
        timeline: the finished EDL. Disabled clips are left out -- the viewer
            will not see them, so they are not part of the edit being reviewed.
        observations: vision observations **keyed by recording**. A mapping
            rather than a flat list because a session recorded in three parts
            has three second-40s, and an observation attributed to the wrong
            recording puts a description on footage it never saw. The stored
            observation does not carry its own media id, so the only place that
            knows is the caller that fetched it.
        speech: transcript segments, keyed the same way.
        events: correlated game events, keyed the same way.
        captions: placed captions, matched by *timeline* position -- they were
            laid out against the finished video, not the recording.
        effects: placed effects, matched by clip id.
    """
    enabled = [clip for clip in timeline.video_clips() if clip.enabled]
    by_clip: list[ClipEvidence] = []
    for index, clip in enumerate(enabled):
        by_clip.append(
            ClipEvidence(
                index=index,
                clip=clip,
                starts_at=clip.timeline_start,
                seconds=clip.timeline_end - clip.timeline_start,
                labels=_labels(clip, _for(observations, clip)),
                descriptions=_descriptions(clip, _for(observations, clip)),
                speech=_speech(clip, _for(speech, clip)),
                events=_events(clip, _for(events, clip)),
                captions=_captions(clip, captions),
                effects=_effects(clip, effects),
            )
        )
    return EditEvidence(
        clips=tuple(by_clip),
        total_seconds=sum(evidence.seconds for evidence in by_clip),
        target_seconds=target_seconds,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _for(source: Mapping[str, Sequence[Any]] | None, clip: TimelineClip) -> Sequence[Any]:
    """Only what was recorded on this clip's own recording."""
    return (source or {}).get(clip.media_id, ())


def _inside(clip: TimelineClip, seconds: float | None) -> bool:
    """Whether a source-timed thing falls inside this clip's span."""
    return seconds is not None and clip.source_in <= seconds < clip.source_out


def _labels(clip: TimelineClip, observations: Sequence[StoredObservation]) -> tuple[str, ...]:
    """Every distinct vision label seen inside the clip, commonest first."""
    counts: dict[str, int] = {}
    for stored in observations:
        if not _inside(clip, stored.timestamp):
            continue
        for label in stored.labels:
            counts[label] = counts.get(label, 0) + 1
    return tuple(sorted(counts, key=lambda label: (-counts[label], label)))


def _descriptions(clip: TimelineClip, observations: Sequence[StoredObservation]) -> tuple[str, ...]:
    inside = sorted(
        (stored for stored in observations if _inside(clip, stored.timestamp)),
        key=lambda stored: stored.timestamp,
    )
    seen: list[str] = []
    for stored in inside:
        text = _shortened(stored.description.strip())
        # Consecutive frames of the same thing produce the same sentence, and
        # three identical lines say less than one.
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= _DESCRIPTIONS_PER_CLIP:
            break
    return tuple(seen)


def _shortened(description: str) -> str:
    """The part of a frame description that identifies the shot."""
    if len(description) <= _DESCRIPTION_CHARACTERS:
        return description
    cut = description.rfind(" ", 0, _DESCRIPTION_CHARACTERS)
    return description[: cut if cut > 0 else _DESCRIPTION_CHARACTERS].rstrip(" ,;") + "..."


def _speech(clip: TimelineClip, segments: Sequence[Any]) -> str:
    words: list[str] = []
    for segment in segments:
        if not _inside(clip, getattr(segment, "start", None)):
            continue
        text = (getattr(segment, "text", "") or "").strip()
        if text:
            words.append(text)
    joined = " ".join(words)
    return joined if len(joined) <= _SPEECH_CHARACTERS else joined[:_SPEECH_CHARACTERS] + "..."


def _events(clip: TimelineClip, events: Sequence[Any]) -> tuple[str, ...]:
    """What happened inside the clip, read as situations rather than reports.

    Phase B's reason for existing, applied where it shows: measured across
    three real recordings, 37-38% of named events are another report of the
    situation before them -- `combat, combat, combat` at ten-second intervals
    is one fight. Listing all three tells a model that three things happened,
    and a model told that writes a review of an edit that does not exist.

    `unknown_event` never appears either way. It is the correlator saying it
    could not name this, and showing it invites a story about something nobody
    identified.
    """
    inside = [event for event in events if _inside(clip, getattr(event, "start_seconds", None))]
    reading = read(inside, media_id=clip.media_id)
    described: list[str] = []
    for episode in reading.episodes:
        value = episode.event_type.value
        # A situation that took several reports says so, because "one long
        # fight" and "a fight" are different things to cut.
        text = f"{value} x{episode.parts}" if episode.is_merged else value
        if text not in described:
            described.append(text)
    return tuple(described)


def _captions(clip: TimelineClip, captions: Sequence[Caption]) -> int:
    return sum(
        1
        for caption in captions
        if clip.timeline_start <= caption.timeline_start < clip.timeline_end
    )


def _effects(clip: TimelineClip, effects: Sequence[EffectInstance]) -> tuple[str, ...]:
    """The distinct effects placed on this clip, both engines.

    Which renderer draws an effect is §68's business, not the Critic's: what
    the viewer sees is the sum of the two.
    """
    kinds: list[str] = []
    for effect in effects:
        if effect.clip_id != clip.id:
            continue
        if effect.effect.value not in kinds:
            kinds.append(effect.effect.value)
    return tuple(kinds)


__all__ = ["ClipEvidence", "EditEvidence", "gather"]
