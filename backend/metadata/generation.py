"""Upload metadata from stored evidence, and nothing else (SPEC §50, §80).

The publish flow already carries :class:`~backend.core.models.publishing.
VideoMetadata` with YouTube's own limits, and §12 already promised the product
would fill it in. This module is that promise kept the way §80 keeps every
explanation: **generated from stored data, deterministically**. No model is
consulted, because a title invented by a model cannot cite the evidence behind
it, and the same project must suggest the same metadata tomorrow.

What each field traces to:

* **Title** -- the project (or detected game) name plus the dominant episode
  types from :mod:`backend.gaming.episodes`, in the language the person
  actually spoke on the recording. Technical gaming terms stay English inside
  Arabic text, exactly as the captions layer renders them (§71).
* **Description** -- counts of episodes by type, the game when detection named
  one, the finished video's length, then the chapter lines YouTube parses.
  Every sentence is a restatement of a stored number. No marketing copy: a
  sentence that traces to nothing is a sentence §80 forbids.
* **Tags** -- the game, the moment types actually present, and "gaming",
  trimmed to the 500-character budget the model enforces.
* **Chapters** -- the STORY result's clips laid end to end, because those
  *are* the video's sections (§35-§39): the first starts at 0, and spacing
  below ``publishing.defaults.min_chapter_seconds`` is skipped because YouTube
  ignores chapters shorter than its own floor.
* **Thumbnail** -- an FFmpeg argv for one frame at the highest-scored moment's
  strongest instant, built here and executed by the caller's runner, the way
  the whole rendering layer works (:mod:`backend.rendering.shorts`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from backend.core.duration import format_duration
from backend.core.models.project import Project
from backend.core.models.publishing import (
    MAX_DESCRIPTION_LENGTH,
    MAX_TAGS_TOTAL_CHARS,
    MAX_TITLE_LENGTH,
    Chapter,
    VideoMetadata,
)
from backend.gaming.episodes import read

# The captions layer already solved "which language is this text" for §71 and
# was corrected against real Arabic projects (first *strong letter* decides,
# UAX#9). Reusing it means the description and the burned-in captions can
# never disagree about what language the video is in.
from backend.timeline.captions import _script_language

#: More chapters than this reads as a table of contents for a film, not a
#: 10-60 minute video; YouTube itself stops being useful long before here.
MAX_CHAPTERS: Final[int] = 20

#: Chapter titles for the narrative roles the STORY stage assigns
#: (hook/climax/outro are the ones a viewer scrubs for; the rest read better
#: as their moment type). (english, arabic) -- the role words are structural,
#: not technical, so they translate.
_ROLE_TITLES: Final[dict[str, tuple[str, str]]] = {
    "hook": ("Opening", "البداية"),
    "climax": ("Climax", "الذروة"),
    "outro": ("Outro", "الختام"),
}

#: Display phrases for the event and moment vocabulary, (english, arabic).
#: A fixed table, not a translation call: §80 again. Genre jargon -- Boss,
#: Clutch -- stays English inside the Arabic phrase because that is how the
#: audience says it; the fallback for anything unlisted is the type's own
#: English words in both languages, which keeps every technical term English.
_PHRASES: Final[dict[str, tuple[str, str]]] = {
    # open-world vocabulary -- the types real footage actually produces
    "combat": ("combat", "معارك"),
    "collision": ("collisions", "اصطدامات"),
    "low_health": ("close calls", "لحظات خطر"),
    "near_death": ("near-death moments", "لحظات موت وشيك"),
    "objective": ("objectives", "إنجاز مهام"),
    "objective_failure": ("failed objectives", "مهام فاشلة"),
    "high_damage": ("destruction", "دمار"),
    "rare_loot": ("rare loot", "غنائم نادرة"),
    "rare_event": ("rare events", "أحداث نادرة"),
    "unexpected_event": ("surprises", "مفاجآت"),
    "death": ("deaths", "لحظات موت"),
    "kill": ("kills", "تصفيات"),
    "multi_kill": ("multi-kills", "تصفيات متتالية"),
    "comeback": ("comebacks", "عودات مستحيلة"),
    "outplay": ("outplays", "تفوق ساحق"),
    # classic vocabulary (§21)
    "boss_fight": ("boss fights", "معارك Boss"),
    "boss_defeat": ("boss takedowns", "إسقاط Boss"),
    "victory": ("victories", "انتصارات"),
    "defeat": ("defeats", "هزائم"),
    "clutch": ("clutch plays", "لقطات Clutch"),
    "escape": ("escapes", "لحظات هروب"),
    "chase": ("chases", "مطاردات"),
    "funny_moment": ("funny moments", "لقطات مضحكة"),
    "fail": ("fails", "لقطات فشل"),
    # moment taxonomy (§34) -- for chapter titles and tags
    "epic": ("epic moments", "لحظات ملحمية"),
    "funny": ("funny moments", "لقطات مضحكة"),
    "boss": ("boss fights", "معارك Boss"),
    "chaos": ("chaos", "فوضى"),
    "tension": ("tense moments", "لحظات توتر"),
    "discovery": ("discoveries", "اكتشافات"),
    "reaction": ("reactions", "ردود فعل"),
}


def suggest(
    project: Project,
    *,
    moments: Sequence[Any] = (),
    events_by_media: Mapping[str, Sequence[Any]] | None = None,
    story_clips: Sequence[Mapping[str, Any]] = (),
    transcript_language: str | None = None,
    min_chapter_seconds: float = 30.0,
    title_language: str = "ar",
) -> VideoMetadata:
    """Build upload metadata from what the pipeline already stored.

    Pure on purpose: repositories hand in the evidence, this function only
    reads it, so the product rules -- which language, which title, which
    chapters -- are testable without a database in the room.

    Args:
        project: the project row; supplies the name, the detected game and the
            declared language.
        moments: every scored moment of the project (any media). Supplies the
            tag vocabulary and the fallback for the title's dominant types.
        events_by_media: stored game events keyed by recording. Read through
            :func:`backend.gaming.episodes.read` so the description counts
            *situations*, not the three reports one fight arrived as.
        story_clips: the ``clips`` list from the STORY job result (§81's
            contract). Empty when no edit has been generated yet.
        transcript_language: what :func:`detect_transcript_language` read off
            the stored transcript, or ``None`` when there is none.
        min_chapter_seconds: ``publishing.defaults.min_chapter_seconds``; the
            default mirrors the config default for callers without a config.

    Returns:
        A valid :class:`VideoMetadata`, minimal but honest when the project
        has no analysis yet (the title is then simply the project name).
    """
    language = _resolve_language(project, transcript_language)
    if title_language in ("ar", "en"):
        # The owner's standing choice beats the transcript: an English-voiced
        # or silent recording still gets the channel's own language.
        arabic = title_language == "ar"
    else:
        arabic = bool(language and language.startswith("ar"))

    readings = {
        media_id: read(events, media_id=media_id)
        for media_id, events in (events_by_media or {}).items()
    }
    episode_counts = _episode_counts(readings)
    moment_counts = _moment_counts(moments)
    # Episodes are the primary evidence of what happened; moments are the
    # fallback so a project analysed before episodes existed still gets a
    # descriptive title rather than a bare name.
    dominant = _dominant(episode_counts or moment_counts)

    chapters = _chapters(story_clips, arabic=arabic, min_spacing=min_chapter_seconds)

    return VideoMetadata(
        title=_title(project, dominant=dominant, arabic=arabic),
        description=_description(
            project,
            episode_counts=episode_counts,
            recordings=len(readings),
            story_clips=story_clips,
            chapters=chapters,
            arabic=arabic,
        ),
        tags=_tags(project, moment_counts=moment_counts, arabic=arabic),
        category="Gaming",
        language=language or "auto",
        chapters=chapters,
    )


def detect_transcript_language(segments: Sequence[Any]) -> str | None:
    """The language spoken on the recording, from the stored transcript.

    The provider's own detection wins when a segment carries it. Older
    projects stored ``language`` as NULL on every segment, so the fallback is
    the same first-strong-letter rule the captions layer uses (§71) over the
    transcript's opening text -- one paragraph, one verdict, per UAX#9. A
    transcript with no strong letter stays ``None`` rather than guessed.
    """
    for segment in segments:
        code = str(getattr(segment, "language", None) or "").strip().lower()
        if code and code != "auto":
            return code

    sample: list[str] = []
    length = 0
    for segment in segments:
        text = str(getattr(segment, "text", "") or "")
        if text:
            sample.append(text)
            length += len(text)
            if length >= 2000:  # the first strong letter decides; this is memory hygiene
                break
    return _script_language(" ".join(sample)) if sample else None


def thumbnail_peak(moments: Sequence[Any]) -> tuple[str, float] | None:
    """Where the thumbnail frame comes from: (media_id, seconds), or ``None``.

    The highest-scored moment, at its most confident event's instant --
    §35's ranking already measured which moment carries the video, and the
    confidence peak is when that situation was most unmistakably on screen.
    Clamped inside the moment's core span so a stored event with a stale
    timestamp cannot point the frame grab at unrelated footage.
    """
    best = max(moments, key=lambda m: float(getattr(m, "score", 0.0)), default=None)
    if best is None:
        return None
    start = float(getattr(best, "start_seconds", 0.0))
    end = max(float(getattr(best, "end_seconds", start)), start)
    peak_event = max(
        getattr(best, "events", ()) or (),
        key=lambda event: float(getattr(event, "confidence", 0.0)),
        default=None,
    )
    at = (start + end) / 2.0
    if peak_event is not None:
        at = float(getattr(peak_event, "start_seconds", at))
    return str(getattr(best, "media_id", "")), min(max(at, start), end)


def thumbnail_arguments(source: Path, at_seconds: float, destination: Path) -> list[str]:
    """The argv (after the runner's base arguments) for one thumbnail frame.

    ``-ss`` before ``-i`` for the index seek -- on a 67-minute recording that
    is milliseconds instead of decoding to the moment. One frame, scaled to
    YouTube's 1280-wide thumbnail size with an even height (``-2``) because
    yuv420 refuses odd geometry, at ``-q:v 2`` -- visually lossless JPEG.
    Returned rather than executed, like every builder in the rendering layer:
    the caller's runner owns process policy, and a unit test can read the
    command without FFmpeg in the room.
    """
    return [
        "-ss",
        f"{max(float(at_seconds), 0.0):.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:-2",
        "-q:v",
        "2",
        str(destination),
    ]


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _resolve_language(project: Project, transcript_language: str | None) -> str | None:
    """The transcript's verdict first; the project's declared language after.

    The transcript is what was actually spoken; the project field is what the
    person selected at import, which may still be ``auto``.
    """
    code = (transcript_language or "").strip().lower()
    if code:
        return code
    declared = (getattr(project, "language", "") or "").strip().lower()
    return None if declared in ("", "auto") else declared


def _game_name(project: Project) -> str | None:
    """The detected game as a display name, or ``None`` for generic/unknown.

    §23: "detected with the generic profile" is not a game name, so a generic
    project must not be titled after a profile that identified nothing.
    """
    game = project.effective_game
    if game in ("", "auto", "generic"):
        return None
    return game.replace("_", " ").title()


def _episode_counts(readings: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reading in readings.values():
        for episode in reading.episodes:
            value = episode.event_type.value
            counts[value] = counts.get(value, 0) + 1
    return counts


def _moment_counts(moments: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for moment in moments:
        value = str(getattr(getattr(moment, "moment_type", ""), "value", "")) or str(
            getattr(moment, "moment_type", "")
        )
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _dominant(counts: Mapping[str, int]) -> list[str]:
    """Type values by frequency, ties broken alphabetically for determinism."""
    return [value for value, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _phrase(value: str, *, arabic: bool) -> str:
    english, ar = _PHRASES.get(value, (value.replace("_", " "), value.replace("_", " ")))
    return ar if arabic else english


def _capitalise(text: str) -> str:
    """First letter up, the rest untouched.

    Not ``str.capitalize``: that lowercases the remainder, which would turn
    the deliberately-English "Boss" inside an Arabic phrase into "boss".
    """
    return text[:1].upper() + text[1:]


#: The title's emoji, by the dominant type. The fallback flame is the one
#: every gaming title tradition converged on.
_TITLE_EMOJI: Final[dict[str, str]] = {
    "clutch": "🔥",
    "near_death": "😱",
    "death": "💀",
    "fail": "😂",
    "funny": "😂",
    "funny_moment": "😂",
    "victory": "🏆",
    "boss": "⚔️",
    "boss_fight": "⚔️",
    "chaos": "💥",
    "high_damage": "💥",
    "chase": "🚔",
    "surprise": "😮",
    "unexpected_event": "😮",
    "rare_loot": "💎",
    "discovery": "🔍",
}


def _title(project: Project, *, dominant: Sequence[str], arabic: bool) -> str:
    """The click-worthy line, in the owner's standing language.

    Owner instruction (2026-08-28): the title is always Arabic and always
    grabs -- fully Arabic type names (the day this was written the pipeline
    published "near death وobjective" inside an Arabic sentence), the game
    kept as its Latin brand name, one emoji, one promise.
    """
    subject = _game_name(project) or project.name
    if not dominant:
        return project.name[:MAX_TITLE_LENGTH]
    top = [_phrase(value, arabic=arabic) for value in dominant[:2]]
    emoji = _TITLE_EMOJI.get(dominant[0], "🔥")
    if arabic:
        joined = " و".join(top)
        title = f"{joined} في {subject} {emoji} لن تصدق ما حدث!"
    else:
        joined = " & ".join(top)
        title = f"{joined} in {subject} {emoji} moments you won't believe!"
    return title[:MAX_TITLE_LENGTH]


def _description(
    project: Project,
    *,
    episode_counts: Mapping[str, int],
    recordings: int,
    story_clips: Sequence[Mapping[str, Any]],
    chapters: Sequence[Chapter],
    arabic: bool,
) -> str:
    """Two to four sentences of stored numbers, then the chapter lines.

    Deliberately no sign-off and no call to action: every line here restates
    evidence, and a line that traces to nothing has no business in it (§80).
    """
    sentences: list[str] = []

    total = sum(episode_counts.values())
    if total:
        listing = _listing(episode_counts, arabic=arabic)
        if arabic:
            sentences.append(
                f"يضم هذا الفيديو {total} موقفًا مميزًا من {recordings} تسجيل: {listing}."
            )
        else:
            plural = "recording" if recordings == 1 else "recordings"
            sentences.append(f"{total} notable situations across {recordings} {plural}: {listing}.")

    game = _game_name(project)
    if game:
        sentences.append(f"اللعبة: {game}." if arabic else f"Game: {game}.")

    video_seconds = sum(max(float(clip.get("seconds") or 0.0), 0.0) for clip in story_clips)
    if video_seconds > 0:
        length = format_duration(video_seconds)
        sentences.append(f"مدة الفيديو: {length}." if arabic else f"Video length: {length}.")

    blocks = []
    if sentences:
        blocks.append(" ".join(sentences))
    if chapters:
        blocks.append(
            "\n".join(
                f"{format_duration(chapter.start_seconds)} {chapter.title}" for chapter in chapters
            )
        )
    text = "\n\n".join(blocks)
    if len(text) > MAX_DESCRIPTION_LENGTH:
        # Cut at a line boundary: a half chapter line would be worse than none.
        text = text[:MAX_DESCRIPTION_LENGTH].rsplit("\n", 1)[0]
    return text


def _listing(counts: Mapping[str, int], *, arabic: bool) -> str:
    """ "12 combat, 5 close calls" -- the top three types with their counts."""
    parts = [
        f"{count} {_phrase(value, arabic=arabic)}"
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    ]
    return "، ".join(parts) if arabic else ", ".join(parts)


def _tags(project: Project, *, moment_counts: Mapping[str, int], arabic: bool) -> list[str]:
    """Game, moment types present, "gaming" -- inside the character budget.

    The model rejects a list over :data:`MAX_TAGS_TOTAL_CHARS`, so the budget
    is enforced here by dropping the least frequent types, never by raising:
    a suggestion endpoint that 500s over its own tag list helps nobody.
    """
    candidates = ["gaming"]
    if arabic:
        candidates.append("ألعاب")
    game = _game_name(project)
    if game:
        candidates.append(game.lower())
    candidates += [value.replace("_", " ") for value in _dominant(moment_counts)]

    kept: list[str] = []
    total = 0
    for tag in dict.fromkeys(candidates):  # deduplicate, order preserved
        if total + len(tag) > MAX_TAGS_TOTAL_CHARS:
            break
        kept.append(tag)
        total += len(tag)
    return kept


def _chapters(
    story_clips: Sequence[Mapping[str, Any]], *, arabic: bool, min_spacing: float
) -> list[Chapter]:
    """The STORY clips laid end to end, as YouTube chapter markers.

    The STORY result stores each clip's length, not its timeline position --
    that is the EDL's job -- but chapters need only the running total, and
    computing it here keeps this readable from the job result alone. The
    first chapter therefore starts at exactly 0.0, which the metadata model
    requires and YouTube's parser insists on.
    """
    kept: list[Chapter] = []
    used: dict[str, int] = {}
    position = 0.0
    for index, clip in enumerate(story_clips):
        start = position
        position += max(float(clip.get("seconds") or 0.0), 0.0)
        if kept and start - kept[-1].start_seconds < max(min_spacing, 0.0):
            continue
        if len(kept) >= MAX_CHAPTERS:
            break
        base = _chapter_title(clip, index=index, arabic=arabic)
        used[base] = used.get(base, 0) + 1
        title = base if used[base] == 1 else f"{base} {used[base]}"
        kept.append(Chapter(title=title[:100], start_seconds=round(start, 3)))
    return kept


def _chapter_title(clip: Mapping[str, Any], *, index: int, arabic: bool) -> str:
    role = str(clip.get("role") or "")
    if role in _ROLE_TITLES:
        english, ar = _ROLE_TITLES[role]
        return ar if arabic else english
    moment_type = str(clip.get("moment_type") or "")
    if moment_type:
        return _capitalise(_phrase(moment_type, arabic=arabic))
    return f"جزء {index + 1}" if arabic else f"Part {index + 1}"


__all__ = [
    "MAX_CHAPTERS",
    "detect_transcript_language",
    "suggest",
    "thumbnail_arguments",
    "thumbnail_peak",
]
