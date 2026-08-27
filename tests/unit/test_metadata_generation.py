"""Metadata generation from stored evidence (§50, §80).

The pure core only: which language, which title, which chapters, which tags --
the product rules -- proved without a database or FFmpeg in the room. The
thumbnail is tested as the argv it builds, the way the whole rendering layer
is tested, because executing it is the router's job.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.models.enums import GameEventType, MomentType
from backend.core.models.project import Project
from backend.core.models.publishing import MAX_TAGS_TOTAL_CHARS, VideoMetadata
from backend.gaming.correlation import GameEvent
from backend.metadata.generation import (
    MAX_CHAPTERS,
    detect_transcript_language,
    suggest,
    thumbnail_arguments,
    thumbnail_peak,
)
from backend.moments.formation import Moment

pytestmark = pytest.mark.unit


def _project(*, name: str = "Night in Grounded", detected_game: str | None = "grounded") -> Project:
    now = datetime.now(timezone.utc)
    return Project(
        id="proj-1",
        name=name,
        created_at=now,
        updated_at=now,
        target_duration_seconds=900,
        detected_game=detected_game,
        project_directory="D:/VAI/projects/proj-1",
    )


def _event(
    kind: GameEventType, start: float, *, end: float | None = None, confidence: float = 0.8
) -> GameEvent:
    return GameEvent(
        event_type=kind,
        start_seconds=start,
        end_seconds=end if end is not None else start + 2.0,
        confidence=confidence,
        importance=0.7,
        sources=("audio",),
    )


def _moment(
    start: float,
    *,
    score: float = 0.6,
    kind: MomentType = MomentType.EPIC,
    events: tuple[GameEvent, ...] = (),
    media_id: str = "media-1",
) -> Moment:
    return Moment(
        media_id=media_id,
        moment_type=kind,
        start_seconds=start,
        end_seconds=start + 30.0,
        context_start=start,
        context_end=start + 30.0,
        score=score,
        events=events,
    )


def _clip(seconds: float, *, role: str = "body", moment_type: str = "combat") -> dict:
    return {"seconds": seconds, "role": role, "moment_type": moment_type}


class TestLanguage:
    def test_an_arabic_transcript_yields_an_arabic_title_with_english_terms(self) -> None:
        # Two boss fights far enough apart to stay two episodes.
        events = [_event(GameEventType.BOSS_FIGHT, 10.0), _event(GameEventType.BOSS_FIGHT, 200.0)]

        metadata = suggest(
            _project(),
            events_by_media={"media-1": events},
            transcript_language="ar",
        )

        assert metadata.title.startswith("أقوى لحظات")
        # The game name and the genre jargon stay English inside the Arabic text.
        assert "Grounded" in metadata.title
        assert "Boss" in metadata.title
        assert metadata.language == "ar"

    def test_an_arabic_description_counts_the_stored_episodes(self) -> None:
        events = [_event(GameEventType.COMBAT, 10.0), _event(GameEventType.COMBAT, 200.0)]

        metadata = suggest(
            _project(), events_by_media={"media-1": events}, transcript_language="ar"
        )

        assert "يضم هذا الفيديو 2" in metadata.description
        assert "معارك" in metadata.description
        assert "اللعبة: Grounded." in metadata.description

    def test_an_english_transcript_yields_an_english_title(self) -> None:
        events = [_event(GameEventType.COMBAT, 10.0), _event(GameEventType.COMBAT, 200.0)]

        metadata = suggest(
            _project(), events_by_media={"media-1": events}, transcript_language="en"
        )

        assert metadata.title == "Best of Grounded: combat"
        assert metadata.language == "en"

    def test_detection_trusts_the_stored_language_field_first(self) -> None:
        segments = [SimpleNamespace(language="ar", text="nice shot")]
        assert detect_transcript_language(segments) == "ar"

    def test_detection_reads_the_script_when_no_language_was_stored(self) -> None:
        # The first strong letter decides (UAX#9), so "nice shot يا شباب"
        # stays Latin while an Arabic-first line reads as Arabic.
        arabic_first = [SimpleNamespace(language=None, text="يا شباب nice shot")]
        latin_first = [SimpleNamespace(language=None, text="nice shot يا شباب")]

        assert detect_transcript_language(arabic_first) == "ar"
        assert detect_transcript_language(latin_first) is None
        assert detect_transcript_language([]) is None


class TestChapters:
    def test_chapters_lay_the_story_clips_end_to_end_from_zero(self) -> None:
        clips = [
            _clip(40.0, role="hook", moment_type="epic"),
            _clip(45.0, moment_type="combat"),
            _clip(50.0, role="climax", moment_type="boss"),
        ]

        metadata = suggest(_project(), story_clips=clips)

        starts = [chapter.start_seconds for chapter in metadata.chapters]
        assert starts == [0.0, 40.0, 85.0]
        assert starts == sorted(starts)
        assert [chapter.title for chapter in metadata.chapters] == [
            "Opening",
            "Combat",
            "Climax",
        ]

    def test_clips_below_the_spacing_floor_merge_into_the_previous_chapter(self) -> None:
        metadata = suggest(
            _project(),
            story_clips=[_clip(10.0) for _ in range(7)],
            min_chapter_seconds=30.0,
        )

        assert [chapter.start_seconds for chapter in metadata.chapters] == [0.0, 30.0, 60.0]

    def test_the_chapter_count_is_capped(self) -> None:
        metadata = suggest(_project(), story_clips=[_clip(40.0) for _ in range(40)])

        assert len(metadata.chapters) == MAX_CHAPTERS

    def test_repeated_chapter_titles_are_numbered(self) -> None:
        metadata = suggest(_project(), story_clips=[_clip(40.0), _clip(40.0)])

        assert [chapter.title for chapter in metadata.chapters] == ["Combat", "Combat 2"]

    def test_the_description_carries_the_chapter_lines_and_the_length(self) -> None:
        metadata = suggest(
            _project(),
            story_clips=[_clip(40.0, role="hook"), _clip(45.0), _clip(50.0)],
            transcript_language="en",
        )

        assert "0:00 Opening" in metadata.description
        assert "0:40 Combat" in metadata.description
        # 40 + 45 + 50 seconds of selected clips is the video's own length.
        assert "Video length: 2:15." in metadata.description


class TestTags:
    def test_tags_carry_gaming_the_game_and_the_moment_types(self) -> None:
        metadata = suggest(
            _project(),
            moments=[_moment(0.0, kind=MomentType.FUNNY), _moment(60.0, kind=MomentType.BOSS)],
        )

        assert "gaming" in metadata.tags
        assert "grounded" in metadata.tags
        assert "funny" in metadata.tags
        assert "boss" in metadata.tags

    def test_an_arabic_project_gets_the_arabic_gaming_tag_too(self) -> None:
        metadata = suggest(_project(), transcript_language="ar")

        assert "gaming" in metadata.tags
        assert "ألعاب" in metadata.tags

    def test_the_tag_budget_is_enforced_by_dropping_not_by_raising(self) -> None:
        # Absurd vocabulary on purpose: the pure core must trim to the budget
        # the model enforces, because a suggestion that 500s over its own tag
        # list helps nobody.
        absurd = [SimpleNamespace(moment_type=f"{chr(97 + i)}" * 90) for i in range(10)]

        metadata = suggest(_project(), moments=absurd)

        assert sum(len(tag) for tag in metadata.tags) <= MAX_TAGS_TOTAL_CHARS
        assert "gaming" in metadata.tags


class TestEmptyProject:
    def test_an_unanalysed_project_still_gets_valid_minimal_metadata(self) -> None:
        metadata = suggest(_project(name="Fresh import", detected_game=None))

        assert isinstance(metadata, VideoMetadata)
        assert metadata.title == "Fresh import"
        assert metadata.chapters == []
        assert metadata.description == ""
        assert metadata.tags == ["gaming"]
        assert metadata.language == "auto"

    def test_a_generic_profile_is_not_presented_as_a_game_name(self) -> None:
        metadata = suggest(_project(detected_game=None))

        assert "generic" not in [tag.lower() for tag in metadata.tags]
        assert "Game:" not in metadata.description


class TestThumbnail:
    def test_the_argv_seeks_before_input_and_takes_one_scaled_frame(self) -> None:
        argv = thumbnail_arguments(Path("in.mp4"), 12.5, Path("out.jpg"))

        assert argv == [
            "-ss",
            "12.500",
            "-i",
            "in.mp4",
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-2",
            "-q:v",
            "2",
            "out.jpg",
        ]
        # The seek must precede the input or FFmpeg decodes its way to the moment.
        assert argv.index("-ss") < argv.index("-i")

    def test_the_peak_is_the_best_moments_most_confident_instant(self) -> None:
        weak = _moment(0.0, score=0.4)
        strong = _moment(
            100.0,
            score=0.9,
            media_id="media-2",
            events=(
                _event(GameEventType.COMBAT, 105.0, confidence=0.5),
                _event(GameEventType.COMBAT, 120.0, confidence=0.9),
            ),
        )

        assert thumbnail_peak([weak, strong]) == ("media-2", 120.0)

    def test_a_stale_event_timestamp_is_clamped_into_the_moment(self) -> None:
        moment = _moment(
            10.0, score=0.9, events=(_event(GameEventType.COMBAT, 500.0, confidence=0.9),)
        )

        media_id, at_seconds = thumbnail_peak([moment])
        assert media_id == "media-1"
        assert at_seconds == 40.0  # the moment's own end, never beyond it

    def test_no_moments_means_no_thumbnail(self) -> None:
        assert thumbnail_peak([]) is None
