"""Captions from transcript timestamps (SPEC §71).

§71's rule is short and load-bearing: **caption timing always derives from
transcript timestamps.** Captions are never generated from text alone. That
rules out the obvious shortcut — take the words, guess a reading speed, spread
them over the clip — because a guess drifts, and a caption that drifts is worse
than no caption at all.

So this module maps, it does not invent. Every caption's timing is a transcript
timestamp translated from source time into timeline time, which is exactly the
translation the timeline exists to describe:

    caption start = clip.timeline_start + (segment.start - clip.source_in)

A segment that falls in a part of the recording the edit did not use produces
no caption, because nobody says those words in the finished video. A segment
straddling a cut is split at the cut and its halves timed independently, since
after the cut the two halves are seconds apart.

Line breaking is layout, not timing, and is done here only to the extent §71's
config asks for it — the renderer draws, and a caption that arrives pre-wrapped
to a character count the renderer disagrees with is worse than an unwrapped one.
Word timings are carried through untouched for the word-highlighting Phase 9
draws.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai.providers.base import TranscriptSegment
from backend.config.schema import CaptionsConfig
from backend.core.ids import derived_id
from backend.core.logging import LogChannel, get_logger
from backend.timeline import retime
from backend.timeline.models import Timeline, TimelineClip

logger = get_logger("timeline.captions", LogChannel.PIPELINE)

#: Strong right-to-left LETTER ranges, with the code the rtl config lists.
#: Deliberately narrower than the Unicode blocks: the Arabic block also holds
#: punctuation (U+060C the comma, U+061F the question mark) and Arabic-Indic
#: digits, all of which Whisper emits inside otherwise-Latin lines -- one of
#: those must not flip a Latin caption right-to-left. The Arabic letter
#: ranges also carry Persian and Urdu; the renderer only needs a code that
#: ``captions.languages.rtl`` lists, not a full identification.
_RTL_LETTERS: tuple[tuple[range, str], ...] = (
    (range(0x0621, 0x064B), "ar"),
    (range(0x066E, 0x06D4), "ar"),
    (range(0x0750, 0x0780), "ar"),
    (range(0x05D0, 0x05EB), "he"),
)

#: Below this much of a segment surviving a cut, the fragment is dropped rather
#: than shown: a caption flashing two words of a sentence reads as a glitch.
MIN_FRAGMENT_RATIO: float = 0.25


@dataclass(frozen=True, slots=True)
class Caption:
    """One caption, positioned on the finished timeline (§71)."""

    id: str
    index: int
    timeline_start: float
    timeline_end: float
    text: str
    language: str | None = None
    clip_id: str | None = None
    #: Word timings, already in timeline coordinates, for word highlighting.
    words: tuple[tuple[str, float, float], ...] = ()
    style: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.timeline_end - self.timeline_start

    def as_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "caption_index": self.index,
            "clip_id": self.clip_id,
            "timeline_start": round(self.timeline_start, 3),
            "timeline_end": round(self.timeline_end, 3),
            "text": self.text,
            "language": self.language,
            "words": [
                {"word": word, "start": round(start, 3), "end": round(end, 3)}
                for word, start, end in self.words
            ],
            "style": self.style,
        }


def build_captions(
    timeline: Timeline,
    segments_by_media: dict[str, Sequence[TranscriptSegment]],
    config: CaptionsConfig,
) -> list[Caption]:
    """Map transcript segments onto the finished timeline (§71).

    Args:
        timeline: the edit. Only enabled video clips carry captions, because a
            disabled clip is footage the viewer never sees.
        segments_by_media: transcript segments keyed by the recording they came
            from. Segments outside the used spans are silently absent, which is
            correct — those words are not in the video.
        config: §71's timing and layout rules.

    Returns:
        Captions in timeline order, indexed from zero.
    """
    if not config.enabled:
        return []

    captions: list[Caption] = []
    for clip in timeline.video_clips():
        segments = segments_by_media.get(clip.media_id, ())
        captions.extend(_captions_for_clip(clip, segments, config))

    captions.sort(key=lambda caption: caption.timeline_start)
    captions = _resolve_collisions(captions, config)
    indexed = [
        Caption(
            id=caption.id,
            index=index,
            timeline_start=caption.timeline_start,
            timeline_end=caption.timeline_end,
            text=caption.text,
            language=caption.language,
            clip_id=caption.clip_id,
            words=caption.words,
            style=caption.style,
        )
        for index, caption in enumerate(captions)
    ]
    logger.info(
        "Built captions from transcript timestamps",
        extra={
            "project_id": timeline.project_id,
            "captions": len(indexed),
            "spoken_seconds": round(sum(item.duration for item in indexed), 2),
        },
    )
    return indexed


def caption_track(timeline: Timeline, captions: Sequence[Caption]) -> Timeline:
    """Record the caption count on the timeline's metadata.

    Captions are not clips and do not go on a clip track: they have their own
    table (§45) and their own renderer pass (Phase 9). What the timeline keeps
    is the fact that they exist, so a caller reading only the timeline knows
    whether the video is captioned.
    """
    metadata = {**timeline.metadata, "captions": len(captions)}
    return timeline.model_copy(update={"metadata": metadata})


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _captions_for_clip(
    clip: TimelineClip, segments: Sequence[TranscriptSegment], config: CaptionsConfig
) -> list[Caption]:
    """Every segment overlapping the clip's source span, mapped onto the timeline."""
    captions: list[Caption] = []
    lead_in = config.timing.lead_in_ms / 1000.0
    for segment in segments:
        if (
            segment.confidence is not None
            and segment.confidence < config.min_confidence
        ):
            # Whisper's own belief in what it wrote. Below the floor the words
            # are as likely hallucinated as heard -- the classic "المترجم..."
            # watermark lines arrive at 0.08 -- and burning text the model did
            # not believe into the picture reads as broken. The audio still
            # plays; only the caption is withheld.
            continue
        overlap_start = max(segment.start, clip.source_in)
        overlap_end = min(segment.end, clip.source_out)
        if overlap_end - overlap_start <= 0:
            continue

        spoken = segment.end - segment.start
        if spoken > 0 and (overlap_end - overlap_start) / spoken < MIN_FRAGMENT_RATIO:
            # The cut took most of this sentence. Showing the remaining two
            # words would read as a glitch, so the fragment is dropped.
            continue

        words = _words_in_span(segment, overlap_start, overlap_end, clip)
        text = _text_for(segment, words)
        if not text:
            continue

        start = _to_timeline(clip, overlap_start) - lead_in
        end = _to_timeline(clip, overlap_end)
        start, end = _apply_timing(start, end, config)
        # Never spill outside the clip: a caption over the next shot belongs to
        # a sentence the viewer is no longer hearing.
        start = max(clip.timeline_start, start)
        end = min(clip.timeline_end, end)
        if end - start < config.timing.min_readable_ms / 1000.0:
            continue

        captions.append(
            Caption(
                id=derived_id("caption", clip.id, round(overlap_start, 3)),
                index=0,
                timeline_start=start,
                timeline_end=end,
                text=text,
                language=segment.language or _script_language(text),
                clip_id=clip.id,
                words=words,
            )
        )
    return captions


def _script_language(text: str) -> str | None:
    """A language code read off the text's own script, or ``None``.

    Transcripts analysed before the provider carried the detected language
    through have ``language`` NULL on every segment -- and the only consumer of
    a caption's language is ``is_rtl``, which then never fired: two real Arabic
    projects rendered their captions left-to-right.

    The decision follows UAX#9's paragraph rule: the *first strong-directional
    letter* decides. Arabic gaming commentary code-switches constantly ("nice
    shot يا شباب"), and a Latin-first line with one Arabic word must stay laid
    out left-to-right -- while a single Arabic comma or Arabic-Indic digit is
    not a letter at all and decides nothing. Anything without a recognisable
    strong letter stays ``None`` rather than guessed.
    """
    for char in text:
        if char.isascii() and char.isalpha():
            return None
        point = ord(char)
        for letters, code in _RTL_LETTERS:
            if point in letters:
                return code
    return None


def _words_in_span(
    segment: TranscriptSegment, start: float, end: float, clip: TimelineClip
) -> tuple[tuple[str, float, float], ...]:
    """Word timings inside the surviving span, in timeline coordinates."""
    inside: list[tuple[str, float, float]] = []
    for word in segment.words:
        if word.end <= start or word.start >= end:
            continue
        inside.append(
            (
                word.word,
                _to_timeline(clip, max(word.start, start)),
                _to_timeline(clip, min(word.end, end)),
            )
        )
    return tuple(inside)


def _text_for(
    segment: TranscriptSegment, words: Sequence[tuple[str, float, float]]
) -> str:
    """The caption's text.

    When word timings survive the cut, the text is those words -- so a split
    sentence shows the half the viewer actually hears. Without word timings
    there is nothing to trim by, and the whole segment's text is used rather
    than a guess at which part of it is audible.
    """
    if words:
        return " ".join(word for word, _, _ in words).strip()
    return segment.text.strip()


def _to_timeline(clip: TimelineClip, source_seconds: float) -> float:
    """Source time to timeline time -- the mapping §71 requires captions to use.

    Through the clip's warp mapping rather than a straight division: a clip
    the EDL re-laid for a freeze or a ramp shows its later source seconds
    later, and a caption timed linearly appeared up to the hold's length
    before its words were heard. A frozen extension adds no speech of its own
    -- there are no transcript timestamps inside invented time -- so mapping
    the real timestamps piecewise is the whole adjustment.
    """
    return clip.timeline_start + retime.output_offset(clip, source_seconds - clip.source_in)


def _apply_timing(start: float, end: float, config: CaptionsConfig) -> tuple[float, float]:
    """Hold a caption long enough to read, and not so long it outstays the line."""
    minimum = config.timing.min_duration_ms / 1000.0
    maximum = config.timing.max_duration_ms / 1000.0
    duration = end - start
    if duration < minimum:
        end = start + minimum
    elif duration > maximum:
        end = start + maximum
    return start, end


def _resolve_collisions(captions: Sequence[Caption], config: CaptionsConfig) -> list[Caption]:
    """Keep captions off each other, in timeline order.

    Extending a caption to a readable length can push it into the next one. The
    later caption wins the contested time, because it is the one whose words are
    being spoken; the earlier is shortened, and dropped if shortening leaves it
    unreadable.
    """
    gap = config.timing.gap_ms / 1000.0
    floor = config.timing.min_readable_ms / 1000.0
    resolved: list[Caption] = []
    for index, caption in enumerate(captions):
        end = caption.timeline_end
        if index + 1 < len(captions):
            end = min(end, captions[index + 1].timeline_start - gap)
        if end - caption.timeline_start < floor:
            continue
        resolved.append(
            Caption(
                id=caption.id,
                index=caption.index,
                timeline_start=caption.timeline_start,
                timeline_end=end,
                text=caption.text,
                language=caption.language,
                clip_id=caption.clip_id,
                words=caption.words,
                style=caption.style,
            )
        )
    return resolved


def wrap(text: str, config: CaptionsConfig) -> list[str]:
    """Break a caption into lines for the renderer's layout (§71).

    Greedy by width, capped at ``max_lines``. Kept separate from timing because
    it is the one part of a caption a renderer may reasonably redo: it knows the
    real font metrics, and this only knows a character count.
    """
    limit = config.layout.max_chars_per_line
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) <= config.layout.max_lines:
        return lines
    # Over the line budget: keep the leading lines and let the last carry the
    # rest, rather than dropping words the viewer can hear being said.
    head = lines[: config.layout.max_lines - 1]
    return [*head, " ".join(lines[config.layout.max_lines - 1 :])]


def to_srt(captions: Iterable[Caption]) -> str:
    """SubRip sidecar (§71). Timings are the timeline's, unmodified."""
    blocks = []
    for index, caption in enumerate(captions, start=1):
        blocks.append(
            f"{index}\n"
            f"{_timestamp(caption.timeline_start, ',')} --> "
            f"{_timestamp(caption.timeline_end, ',')}\n"
            f"{caption.text}\n"
        )
    return "\n".join(blocks)


def to_vtt(captions: Iterable[Caption]) -> str:
    """WebVTT sidecar (§71)."""
    blocks = ["WEBVTT\n"]
    for caption in captions:
        blocks.append(
            f"{_timestamp(caption.timeline_start, '.')} --> "
            f"{_timestamp(caption.timeline_end, '.')}\n"
            f"{caption.text}\n"
        )
    return "\n".join(blocks)


def _timestamp(seconds: float, decimal: str) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, whole = divmod(remainder, 60)
    milliseconds = round((seconds - int(seconds)) * 1000)
    if milliseconds == 1000:  # rounding carried into the next second
        whole += 1
        milliseconds = 0
    return f"{hours:02d}:{minutes:02d}:{whole:02d}{decimal}{milliseconds:03d}"


__all__ = [
    "MIN_FRAGMENT_RATIO",
    "Caption",
    "build_captions",
    "caption_track",
    "to_srt",
    "to_vtt",
    "wrap",
]
