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

        # Owner instruction (2026-08-28): the title always grabs -- fully
        # Arabic wording, the game as its Latin brand name, one emoji.
        assert "Grounded" in metadata.title
        assert "معارك Boss" in metadata.title
        assert "لن تصدق" in metadata.title
        assert metadata.language == "ar"

    def test_an_arabic_description_counts_the_stored_episodes(self) -> None:
        events = [_event(GameEventType.COMBAT, 10.0), _event(GameEventType.COMBAT, 200.0)]

        metadata = suggest(
            _project(), events_by_media={"media-1": events}, transcript_language="ar"
        )

        assert "يضم هذا الفيديو 2" in metadata.description
        assert "معارك" in metadata.description
        assert "اللعبة: Grounded." in metadata.description

    def test_the_title_language_is_the_owners_not_the_transcripts(self) -> None:
        # The channel publishes in Arabic; an English-voiced recording is a
        # fact about the footage, not about the title.
        events = [_event(GameEventType.COMBAT, 10.0), _event(GameEventType.COMBAT, 200.0)]

        metadata = suggest(
            _project(), events_by_media={"media-1": events}, transcript_language="en"
        )

        assert "معارك" in metadata.title
        assert metadata.language == "en"

    def test_english_titles_remain_one_knob_away(self) -> None:
        events = [_event(GameEventType.COMBAT, 10.0), _event(GameEventType.COMBAT, 200.0)]

        metadata = suggest(
            _project(),
            events_by_media={"media-1": events},
            transcript_language="en",
            title_language="en",
        )

        assert "combat in Grounded" in metadata.title

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

        metadata = suggest(_project(), story_clips=clips, title_language="en")

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
        metadata = suggest(
            _project(), story_clips=[_clip(40.0), _clip(40.0)], title_language="en"
        )

        assert [chapter.title for chapter in metadata.chapters] == ["Combat", "Combat 2"]

    def test_the_description_carries_the_chapter_lines_and_the_length(self) -> None:
        metadata = suggest(
            _project(),
            story_clips=[_clip(40.0, role="hook"), _clip(45.0), _clip(50.0)],
            transcript_language="en",
            title_language="en",
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
        assert metadata.tags[0] == "gaming"
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


class TestThumbnailHooks:
    """The phrase that grabs, chosen by the analysis and drawn shaped."""

    @staticmethod
    def _moment(kind: str, score: float):
        from types import SimpleNamespace

        return SimpleNamespace(moment_type=SimpleNamespace(value=kind), score=score)

    def test_the_dominant_type_votes_with_its_score(self) -> None:
        from backend.metadata.hooks import hook_phrase

        moments = [
            self._moment("fail", 0.2),
            self._moment("fail", 0.2),
            self._moment("clutch", 0.9),
        ]

        phrase, emoji = hook_phrase(moments, "en")
        assert phrase == "IMPOSSIBLE|CLUTCH!"
        assert emoji == "🔥"

    def test_arabic_transcripts_get_the_arabic_phrase(self) -> None:
        from backend.metadata.hooks import hook_phrase

        phrase, emoji = hook_phrase([self._moment("boss", 0.8)], "ar")
        assert phrase == "معركة|الزعيم!"
        assert emoji == "⚔️"

    def test_no_moments_still_produces_a_phrase(self) -> None:
        from backend.metadata.hooks import hook_phrase

        assert hook_phrase([], "en")[0] == "UNMISSABLE|MOMENTS!"

    def test_burning_changes_the_image_and_never_raises(self, tmp_path) -> None:
        from PIL import Image

        from backend.metadata.hooks import burn_hook

        image = tmp_path / "thumb.jpg"
        Image.new("RGB", (640, 360), (40, 90, 140)).save(image)
        before = image.read_bytes()

        drawn = burn_hook(image, "معركة|الزعيم!", "⚔️")

        after = image.read_bytes()
        if drawn:
            assert after != before
            with Image.open(image) as final:
                assert final.size == (640, 360)
                pixels = final.getcolors(maxcolors=200000) or []
                # White first line, red second line: both must reach pixels.
                assert any(
                    r > 230 and g > 230 and b > 230 for _, (r, g, b) in pixels
                )
                assert any(
                    r > 180 and g < 110 and b < 110 for _, (r, g, b) in pixels
                )
        # ``False`` is the honest answer on a machine with no usable font;
        # the plain frame is already a thumbnail.

    def test_a_missing_file_is_a_false_not_a_crash(self, tmp_path) -> None:
        from backend.metadata.hooks import burn_hook

        assert burn_hook(tmp_path / "nowhere.jpg", "TOTAL CHAOS!", "💥") is False


class TestCreativeWriter:
    """Per-video words from the model, with the tables as the floor."""

    @staticmethod
    def _provider(payload):
        from ai.llm.fake_provider import FakeLLMProvider

        return FakeLLMProvider(default=payload)

    def test_a_valid_answer_becomes_the_video_text(self) -> None:
        from backend.metadata.creative import write

        provider = self._provider(
            {
                "title": "نجوت من العنكبوت بأعجوبة في Grounded 🔥",
                "hook_top": "عنكبوت",
                "hook_bottom": "كاد يلتهمني!",
            }
        )

        written = write(
            provider,
            game="Grounded",
            duration="10:00",
            types="3 معارك",
            creatures="ORB WEAVER",
            speech="-",
            arabic=True,
        )

        assert written is not None
        assert "Grounded" in written.title
        assert written.hook_lines == "عنكبوت|كاد يلتهمني!"

    def test_a_latin_only_title_on_an_arabic_channel_is_refused(self) -> None:
        from backend.metadata.creative import write

        provider = self._provider(
            {"title": "Crazy Grounded moments!!", "hook_top": "wow", "hook_bottom": "insane!"}
        )

        assert (
            write(
                provider,
                game="Grounded",
                duration="10:00",
                types="-",
                creatures="-",
                speech="-",
                arabic=True,
            )
            is None
        )

    def test_out_of_bounds_lengths_fall_back_to_the_tables(self) -> None:
        from backend.metadata.creative import write

        provider = self._provider(
            {"title": "قصير", "hook_top": "أ", "hook_bottom": "ب"}
        )

        assert (
            write(
                provider,
                game="G",
                duration="1:00",
                types="-",
                creatures="-",
                speech="-",
                arabic=True,
            )
            is None
        )

    def test_no_provider_means_the_tables_carry_on(self) -> None:
        from backend.metadata.creative import write

        assert (
            write(
                None,
                game="G",
                duration="1:00",
                types="-",
                creatures="-",
                speech="-",
                arabic=True,
            )
            is None
        )
