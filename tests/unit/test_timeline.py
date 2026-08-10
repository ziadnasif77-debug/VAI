"""Phase 8: the timeline (SPEC §40–§45, §71).

The acceptance criterion is that the generated EDL reproduces the planned video
exactly, and the property that makes it worth having is §42: the original is
never modified, so every clip is a reference and any edit is undoable.

The decisive tests here are the ones that would fail if the timeline quietly
became a copy of the plan rather than a description of it:
``TestNonDestructive`` (source timestamps survive every operation) and
``TestReproducesThePlan`` (what came out is what went in, in order, with no
seconds invented or lost).
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from ai.providers.base import TranscriptSegment, TranscriptWord
from backend.config.loader import load_config
from backend.core.errors import ValidationError
from backend.core.ids import derived_id, is_valid_id
from backend.core.models.enums import MomentType, TrackKind, TransitionType
from backend.timeline import captions as caption_builder
from backend.timeline import operations, validation
from backend.timeline.builder import (
    MIN_CLIP_SECONDS,
    PlannedClip,
    build_timeline,
    clips_from_story_result,
)
from backend.timeline.models import Timeline, TimelineClip, Track

pytestmark = pytest.mark.unit

MEDIA = "media-aaaaaaaaaaaa"


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def policy(config):
    return config.output.duration_policy()


def _planned(count: int = 20, *, seconds: float = 40.0, gap: float = 60.0) -> list[PlannedClip]:
    """A plan the optimiser could plausibly have produced."""
    types = list(MomentType)
    clips: list[PlannedClip] = []
    cursor = 0.0
    for index in range(count):
        clips.append(
            PlannedClip(
                media_id=MEDIA,
                source_start=cursor,
                source_end=cursor + seconds,
                moment_id=f"mom-{index:03d}",
                moment_type=types[index % len(types)],
                score=0.5 + (index % 5) / 10.0,
                role="hook" if index == 0 else "body",
                beat="climax" if index == 3 else "body",
            )
        )
        cursor += seconds + gap
    return clips


def _build(clips, policy, *, durations=None, **kwargs):
    return build_timeline(
        clips,
        project_id="proj-aaaaaaaaaaaa",
        policy=policy,
        media_durations=durations if durations is not None else {MEDIA: 100_000.0},
        **kwargs,
    )


class TestReproducesThePlan:
    """**Acceptance: the generated EDL reproduces the planned video exactly.**"""

    def test_every_planned_clip_becomes_exactly_one_timeline_clip(self, policy) -> None:
        planned = _planned(30)
        timeline = _build(planned, policy).timeline

        assert len(timeline.video_clips()) == len(planned)

    def test_the_order_is_the_plan_s_order(self, policy) -> None:
        # The narrative stage decided the sequence for reasons the timeline does
        # not know. Re-sorting here would silently discard §35's mode.
        planned = _planned(15)
        timeline = _build(planned, policy).timeline

        assert [clip.moment_id for clip in timeline.video_clips()] == [
            clip.moment_id for clip in planned
        ]

    def test_every_source_span_survives_unchanged(self, policy) -> None:
        planned = _planned(12)
        timeline = _build(planned, policy).timeline

        for original, clip in zip(planned, timeline.video_clips(), strict=True):
            assert clip.source_in == pytest.approx(original.source_start)
            assert clip.source_out == pytest.approx(original.source_end)

    def test_the_duration_is_the_sum_of_the_planned_spans(self, policy) -> None:
        planned = _planned(25, seconds=37.5)
        timeline = _build(planned, policy).timeline

        assert timeline.duration == pytest.approx(sum(clip.seconds for clip in planned))

    def test_the_timeline_is_contiguous_from_zero(self, policy) -> None:
        # A gap renders as black frames nobody asked for.
        timeline = _build(_planned(20), policy).timeline
        clips = timeline.video_clips()

        assert clips[0].timeline_start == 0.0
        for previous, following in pairwise(clips):
            assert following.timeline_start == pytest.approx(previous.timeline_end)

    def test_it_validates(self, policy) -> None:
        timeline = _build(_planned(20), policy).timeline
        report = validation.validate(
            timeline, media_durations={MEDIA: 100_000.0}, policy=policy
        )

        assert report.is_valid, [str(item) for item in report.errors]

    def test_rebuilding_the_same_plan_produces_the_same_ids(self, policy) -> None:
        # §48 and §78 both depend on this: a re-run that renamed every clip
        # would invalidate the cache and orphan the user's edit decisions.
        planned = _planned(10)
        first = _build(planned, policy).timeline
        second = _build(_planned(10), policy).timeline

        assert [clip.id for clip in first.video_clips()] == [
            clip.id for clip in second.video_clips()
        ]
        assert all(is_valid_id(clip.id) for clip in first.video_clips())


class TestNonDestructive:
    """§42: the original video is never modified; edits are references."""

    def test_a_clip_names_a_file_and_two_timestamps(self, policy) -> None:
        clip = _build(_planned(3), policy).timeline.video_clips()[0]

        assert clip.media_id == MEDIA
        assert clip.source_out > clip.source_in >= 0.0

    def test_every_operation_leaves_the_source_spans_alone(self, policy) -> None:
        timeline = _build(_planned(10), policy).timeline
        untouched = timeline.video_clips()[5]

        after = operations.move(timeline, timeline.video_clips()[1].id, 8)
        after = operations.delete(after, after.video_clips()[0].id)

        moved = after.clip(untouched.id)
        assert moved is not None
        assert moved.source_in == untouched.source_in
        assert moved.source_out == untouched.source_out
        # Its position in the finished video changed; the frames it names did not.
        assert moved.timeline_start != untouched.timeline_start

    def test_operations_do_not_mutate_the_timeline_they_are_given(self, policy) -> None:
        timeline = _build(_planned(6), policy).timeline
        before = [clip.timeline_start for clip in timeline.video_clips()]

        operations.delete(timeline, timeline.video_clips()[0].id)

        assert [clip.timeline_start for clip in timeline.video_clips()] == before

    def test_a_clip_maps_a_timeline_position_back_to_the_recording(self, policy) -> None:
        # §59 answers "what happens at 4:12 of the video" with this mapping.
        timeline = _build(_planned(10), policy).timeline
        clip = timeline.video_clips()[4]
        halfway = (clip.timeline_start + clip.timeline_end) / 2

        found = timeline.source_at(halfway)
        assert found is not None
        media_id, source_seconds = found
        assert media_id == MEDIA
        assert source_seconds == pytest.approx(clip.source_in + clip.duration / 2)


class TestOperations:
    def test_split_produces_two_clips_covering_the_same_source(self, policy) -> None:
        timeline = _build(_planned(5), policy).timeline
        clip = timeline.video_clips()[2]
        middle = (clip.timeline_start + clip.timeline_end) / 2

        after = operations.split(timeline, clip.id, middle)
        halves = [item for item in after.video_clips() if item.media_id == clip.media_id]
        left, right = halves[2], halves[3]

        assert left.source_in == clip.source_in
        assert right.source_out == clip.source_out
        assert left.source_out == pytest.approx(right.source_in)
        assert after.duration == pytest.approx(timeline.duration)

    def test_split_refuses_to_leave_an_unwatchable_fragment(self, policy) -> None:
        timeline = _build(_planned(5), policy).timeline
        clip = timeline.video_clips()[0]

        with pytest.raises(ValidationError):
            operations.split(timeline, clip.id, clip.timeline_start + 0.05)

    def test_split_refuses_a_position_outside_the_clip(self, policy) -> None:
        timeline = _build(_planned(5), policy).timeline
        clip = timeline.video_clips()[0]

        with pytest.raises(ValidationError):
            operations.split(timeline, clip.id, clip.timeline_end + 10.0)

    def test_trim_moves_the_source_points_and_the_duration_together(self, policy) -> None:
        timeline = _build(_planned(5), policy).timeline
        clip = timeline.video_clips()[1]

        after = operations.trim(timeline, clip.id, start_delta=2.0, end_delta=-3.0)
        trimmed = after.clip(clip.id)

        assert trimmed.source_in == pytest.approx(clip.source_in + 2.0)
        assert trimmed.source_out == pytest.approx(clip.source_out - 3.0)
        assert trimmed.duration == pytest.approx(clip.duration - 5.0)
        assert after.duration == pytest.approx(timeline.duration - 5.0)

    def test_trim_cannot_reach_before_the_start_of_the_file(self, policy) -> None:
        timeline = _build(_planned(3), policy).timeline
        first = timeline.video_clips()[0]

        with pytest.raises(ValidationError):
            operations.trim(timeline, first.id, start_delta=-10.0)

    def test_delete_removes_the_time_but_keeps_the_clip(self, policy) -> None:
        # §78: the user has the last word, so "cut that bit" must be undoable.
        timeline = _build(_planned(8), policy).timeline
        target = timeline.video_clips()[3]

        after = operations.delete(timeline, target.id)

        assert after.clip(target.id) is not None
        assert after.clip(target.id).enabled is False
        assert after.duration == pytest.approx(timeline.duration - target.duration)

    def test_restore_is_the_exact_inverse_of_delete(self, policy) -> None:
        timeline = _build(_planned(8), policy).timeline
        target = timeline.video_clips()[3]

        restored = operations.restore(operations.delete(timeline, target.id), target.id)

        assert restored.duration == pytest.approx(timeline.duration)
        assert [clip.id for clip in restored.video_clips()] == [
            clip.id for clip in timeline.video_clips()
        ]

    def test_a_deleted_clip_leaves_no_gap(self, policy) -> None:
        timeline = _build(_planned(8), policy).timeline
        after = operations.delete(timeline, timeline.video_clips()[3].id)

        report = validation.validate(after, media_durations={MEDIA: 100_000.0})
        assert report.is_valid, [str(item) for item in report.errors]

    def test_move_reorders_without_changing_the_length(self, policy) -> None:
        timeline = _build(_planned(8), policy).timeline
        target = timeline.video_clips()[6]

        after = operations.move(timeline, target.id, 0)

        assert after.video_clips()[0].id == target.id
        assert after.duration == pytest.approx(timeline.duration)
        assert len(after.video_clips()) == len(timeline.video_clips())

    def test_move_re_indexes_so_a_reload_gives_the_same_video(self, policy) -> None:
        timeline = _build(_planned(6), policy).timeline
        after = operations.move(timeline, timeline.video_clips()[4].id, 1)

        assert [clip.clip_index for clip in after.video_clips()] == list(range(6))

    def test_an_unknown_clip_is_a_typed_error(self, policy) -> None:
        timeline = _build(_planned(3), policy).timeline

        with pytest.raises(ValidationError):
            operations.delete(timeline, "clip-000000000000")


class TestValidation:
    def test_a_gap_is_an_error(self, policy) -> None:
        timeline = _build(_planned(4), policy).timeline
        clips = list(timeline.video_clips())
        shifted = clips[2].moved_to(clips[2].timeline_start + 5.0)
        broken = timeline.with_track(
            Track(kind=TrackKind.VIDEO, clips=(*clips[:2], shifted, *clips[3:]))
        )

        report = validation.validate(broken, media_durations={MEDIA: 100_000.0})
        assert not report.is_valid
        assert any(item.code == "gap" for item in report.errors)

    def test_an_overlap_is_an_error(self, policy) -> None:
        timeline = _build(_planned(4), policy).timeline
        clips = list(timeline.video_clips())
        shifted = clips[2].moved_to(clips[2].timeline_start - 5.0)
        broken = timeline.with_track(
            Track(kind=TrackKind.VIDEO, clips=(*clips[:2], shifted, *clips[3:]))
        )

        report = validation.validate(broken, media_durations={MEDIA: 100_000.0})
        assert not report.is_valid
        assert any(item.code == "overlap" for item in report.errors)

    def test_reading_past_the_end_of_the_recording_is_an_error(self, policy) -> None:
        timeline = _build(_planned(4), policy).timeline

        report = validation.validate(timeline, media_durations={MEDIA: 30.0})
        assert not report.is_valid
        assert any(item.code == "source_out_of_range" for item in report.errors)

    def test_unknown_source_lengths_warn_once_per_recording(self, policy) -> None:
        timeline = _build(_planned(10), policy).timeline

        report = validation.validate(timeline)
        assert report.is_valid  # a warning does not block the render
        assert len([item for item in report.warnings if item.code == "unverified_bounds"]) == 1

    def test_an_empty_timeline_is_an_error(self) -> None:
        report = validation.validate(Timeline(project_id="proj-aaaaaaaaaaaa"))

        assert not report.is_valid
        assert report.errors[0].code == "empty_timeline"

    def test_require_valid_reports_every_error_at_once(self, policy) -> None:
        timeline = _build(_planned(4), policy).timeline

        with pytest.raises(ValidationError) as caught:
            validation.require_valid(timeline, media_durations={MEDIA: 30.0})

        assert len(caught.value.details["findings"]) >= 4

    def test_a_short_edit_warns_rather_than_failing(self, policy) -> None:
        # No amount of editing makes a short recording into a long video.
        timeline = _build(_planned(2), policy).timeline

        report = validation.validate(
            timeline, media_durations={MEDIA: 100_000.0}, policy=policy
        )
        assert report.is_valid
        assert any(item.code == "under_minimum" for item in report.warnings)

    def test_a_disabled_clip_cannot_cause_a_gap(self, policy) -> None:
        timeline = _build(_planned(6), policy).timeline
        after = operations.delete(timeline, timeline.video_clips()[2].id)

        report = validation.validate(after, media_durations={MEDIA: 100_000.0})
        assert report.is_valid, [str(item) for item in report.errors]


class TestDurationClamp:
    """§6's hard band, enforced at this boundary as a last resort."""

    def test_an_over_long_plan_is_brought_inside_the_maximum(self, policy) -> None:
        planned = _planned(60, seconds=90.0)
        assert sum(clip.seconds for clip in planned) > policy.max_seconds

        result = _build(planned, policy)

        assert result.timeline.duration <= policy.max_seconds
        assert result.was_clamped

    def test_clamping_is_reported_rather_than_silent(self, policy) -> None:
        result = _build(_planned(60, seconds=90.0), policy)

        assert result.warnings
        assert result.notes

    def test_clamping_trims_the_tail_not_the_middle(self, policy) -> None:
        # Dropping the weakest clip wherever it sits would undo the optimiser's
        # variety work and leave a hole in the arc.
        planned = _planned(60, seconds=90.0)
        result = _build(planned, policy)
        clips = result.timeline.video_clips()

        for original, clip in zip(planned[:20], clips[:20], strict=True):
            assert clip.source_in == pytest.approx(original.source_start)
            assert clip.source_out == pytest.approx(original.source_end)

    def test_a_plan_inside_the_band_is_untouched(self, policy) -> None:
        result = _build(_planned(20), policy)

        assert not result.was_clamped
        assert result.warnings == () or all("below" in item for item in result.warnings)

    def test_the_clamped_timeline_still_validates(self, policy) -> None:
        result = _build(_planned(60, seconds=90.0), policy)

        report = validation.validate(
            result.timeline, media_durations={MEDIA: 100_000.0}, policy=policy
        )
        assert report.is_valid, [str(item) for item in report.errors]

    def test_a_clip_never_reads_past_the_end_of_its_recording(self, policy) -> None:
        planned = [
            PlannedClip(media_id=MEDIA, source_start=0.0, source_end=9_999.0, score=0.5)
        ]
        result = _build(planned, policy, durations={MEDIA: 42.0})

        assert result.timeline.video_clips()[0].source_out == pytest.approx(42.0)

    def test_a_clip_entirely_outside_its_recording_is_dropped(self, policy) -> None:
        planned = [
            PlannedClip(media_id=MEDIA, source_start=500.0, source_end=560.0, score=0.5),
            PlannedClip(media_id=MEDIA, source_start=0.0, source_end=40.0, score=0.5),
        ]
        result = _build(planned, policy, durations={MEDIA: 100.0})

        assert len(result.timeline.video_clips()) == 1

    def test_no_clip_is_left_shorter_than_the_minimum(self, policy) -> None:
        result = _build(_planned(60, seconds=90.0), policy)

        assert all(
            clip.duration >= MIN_CLIP_SECONDS for clip in result.timeline.video_clips()
        )


class TestStructure:
    def test_the_hook_and_the_climax_are_marked(self, policy) -> None:
        # §80: the two structural choices a viewer would notice being wrong.
        timeline = _build(_planned(10), policy).timeline

        labels = {marker.label for marker in timeline.markers}
        assert labels == {"hook", "climax"}

    def test_the_programme_opens_and_closes_on_a_fade(self, policy) -> None:
        clips = _build(_planned(6), policy).timeline.video_clips()

        assert clips[0].transition_in is TransitionType.FADE
        assert clips[-1].transition_out is TransitionType.FADE

    def test_every_internal_edit_is_a_cut(self, policy) -> None:
        # §69: no effect is ever applied globally, and a video of crossfades is
        # exactly that.
        clips = _build(_planned(6), policy).timeline.video_clips()

        for clip in clips[1:-1]:
            assert clip.transition_in is TransitionType.CUT
            assert clip.transition_out is TransitionType.CUT

    def test_the_clip_carries_why_it_is_there(self, policy) -> None:
        clip = _build(_planned(6), policy).timeline.video_clips()[0]

        assert clip.moment_id is not None
        assert clip.moment_type is not None
        assert clip.role == "hook"

    def test_an_empty_plan_produces_an_empty_timeline_not_a_crash(self, policy) -> None:
        result = _build([], policy)

        assert result.timeline.is_empty
        assert result.timeline.notes


class TestFromTheStoryResult:
    """§81: the job result is the contract between stages."""

    def test_it_reads_what_the_story_stage_stored(self) -> None:
        stored = {
            "clips": [
                {
                    "index": 0,
                    "media_id": MEDIA,
                    "moment_id": "mom-001",
                    "moment_type": "epic",
                    "source_start": 12.5,
                    "source_end": 45.25,
                    "score": 0.82,
                    "role": "hook",
                    "beat": "hook",
                }
            ]
        }
        clips = clips_from_story_result(stored)

        assert len(clips) == 1
        assert clips[0].source_start == 12.5
        assert clips[0].moment_type is MomentType.EPIC
        assert clips[0].role == "hook"

    def test_a_moment_type_the_enum_no_longer_knows_costs_the_type_not_the_clip(
        self,
    ) -> None:
        stored = {
            "clips": [
                {
                    "media_id": MEDIA,
                    "moment_type": "vintage_1998",
                    "source_start": 0.0,
                    "source_end": 30.0,
                }
            ]
        }
        clips = clips_from_story_result(stored)

        assert len(clips) == 1
        assert clips[0].moment_type is None

    def test_a_result_with_no_clips_yields_nothing(self) -> None:
        assert clips_from_story_result({"skipped": True}) == []

    def test_a_count_under_the_clips_key_does_not_crash_the_stage(self) -> None:
        # The skipped path once reported `"clips": 0`, so a project with no
        # moments took the EDL stage down with a TypeError three frames deep.
        # A stage that skipped must stop the next one, not break it (§95).
        assert clips_from_story_result({"skipped": True, "clips": 0}) == []



class TestCaptions:
    """§71: caption timing always derives from transcript timestamps."""

    @pytest.fixture
    def timeline(self, policy):
        return _build(_planned(4, seconds=40.0, gap=60.0), policy).timeline

    def _segments(self, *spans: tuple[float, float, str]) -> list[TranscriptSegment]:
        return [
            TranscriptSegment(
                start=start,
                end=end,
                text=text,
                language="en",
                words=tuple(
                    TranscriptWord(
                        word=word,
                        start=start + index * (end - start) / len(text.split()),
                        end=start + (index + 1) * (end - start) / len(text.split()),
                    )
                    for index, word in enumerate(text.split())
                ),
            )
            for start, end, text in spans
        ]

    def test_a_caption_lands_where_the_words_are_spoken(self, timeline, config) -> None:
        clip = timeline.video_clips()[1]
        # Ten seconds into the clip's source span.
        spoken_at = clip.source_in + 10.0
        segments = self._segments((spoken_at, spoken_at + 2.0, "no way that landed"))

        captions = caption_builder.build_captions(
            timeline, {MEDIA: segments}, config.captions
        )

        assert len(captions) == 1
        assert captions[0].timeline_start == pytest.approx(clip.timeline_start + 10.0)

    def test_speech_the_edit_cut_produces_no_caption(self, timeline, config) -> None:
        # Nobody says those words in the finished video.
        first, second = timeline.video_clips()[0], timeline.video_clips()[1]
        between = (first.source_out + second.source_in) / 2
        segments = self._segments((between, between + 2.0, "words that were cut"))

        captions = caption_builder.build_captions(
            timeline, {MEDIA: segments}, config.captions
        )

        assert captions == []

    def test_a_caption_never_spills_past_its_clip(self, timeline, config) -> None:
        clip = timeline.video_clips()[0]
        segments = self._segments((clip.source_out - 1.0, clip.source_out + 20.0, "a b c d"))

        captions = caption_builder.build_captions(
            timeline, {MEDIA: segments}, config.captions
        )

        for caption in captions:
            assert caption.timeline_end <= clip.timeline_end + 1e-6

    def test_word_timings_are_carried_into_timeline_coordinates(
        self, timeline, config
    ) -> None:
        clip = timeline.video_clips()[2]
        start = clip.source_in + 5.0
        segments = self._segments((start, start + 4.0, "one two three four"))

        captions = caption_builder.build_captions(
            timeline, {MEDIA: segments}, config.captions
        )

        assert captions[0].words
        for _word, word_start, word_end in captions[0].words:
            assert clip.timeline_start <= word_start <= word_end <= clip.timeline_end

    def test_captions_never_overlap_each_other(self, timeline, config) -> None:
        clip = timeline.video_clips()[0]
        base = clip.source_in + 2.0
        segments = self._segments(
            *[(base + index * 1.0, base + index * 1.0 + 0.6, f"line {index}") for index in range(8)]
        )

        captions = caption_builder.build_captions(
            timeline, {MEDIA: segments}, config.captions
        )

        for previous, following in pairwise(captions):
            assert previous.timeline_end <= following.timeline_start + 1e-6

    def test_captions_are_indexed_in_timeline_order(self, timeline, config) -> None:
        clips = timeline.video_clips()
        segments = self._segments(
            (clips[2].source_in + 1.0, clips[2].source_in + 3.0, "later words"),
            (clips[0].source_in + 1.0, clips[0].source_in + 3.0, "earlier words"),
        )

        captions = caption_builder.build_captions(
            timeline, {MEDIA: segments}, config.captions
        )

        assert [caption.index for caption in captions] == list(range(len(captions)))
        assert captions[0].text == "earlier words"

    def test_disabling_captions_produces_none(self, timeline, config) -> None:
        segments = self._segments((0.0, 2.0, "anything"))
        disabled = config.captions.model_copy(update={"enabled": False})

        assert caption_builder.build_captions(timeline, {MEDIA: segments}, disabled) == []

    def test_srt_carries_the_timeline_timings(self, timeline, config) -> None:
        clip = timeline.video_clips()[0]
        segments = self._segments((clip.source_in + 1.0, clip.source_in + 3.0, "hello there"))
        captions = caption_builder.build_captions(
            timeline, {MEDIA: segments}, config.captions
        )

        srt = caption_builder.to_srt(captions)
        assert "00:00:01,000 --> " in srt
        assert "hello there" in srt

    def test_wrapping_respects_the_line_budget(self, config) -> None:
        text = " ".join(["word"] * 60)
        lines = caption_builder.wrap(text, config.captions)

        assert len(lines) <= config.captions.layout.max_lines
        # No words are lost to layout.
        assert " ".join(lines).split() == text.split()


class TestClipModel:
    def test_a_clip_whose_spans_disagree_is_rejected(self) -> None:
        # The two coordinate systems drifting apart means the renderer either
        # runs out of source or repeats frames.
        with pytest.raises(ValueError, match="cannot fill"):
            TimelineClip(
                id=derived_id("timeline_clip", "x"),
                media_id=MEDIA,
                clip_index=0,
                source_in=0.0,
                source_out=10.0,
                timeline_start=0.0,
                timeline_end=25.0,
            )

    def test_slow_motion_stretches_the_timeline_not_the_source(self) -> None:
        clip = TimelineClip(
            id=derived_id("timeline_clip", "y"),
            media_id=MEDIA,
            clip_index=0,
            source_in=10.0,
            source_out=20.0,
            timeline_start=0.0,
            timeline_end=20.0,
            speed=0.5,
        )

        assert clip.source_duration == 10.0
        assert clip.duration == 20.0
        assert clip.source_at(10.0) == pytest.approx(15.0)

    def test_a_backwards_span_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            TimelineClip(
                id=derived_id("timeline_clip", "z"),
                media_id=MEDIA,
                clip_index=0,
                source_in=30.0,
                source_out=10.0,
                timeline_start=0.0,
                timeline_end=20.0,
            )


class TestPersistence:
    """The rows, and what a round trip through them must not change."""

    @pytest.fixture
    def stored(self, database, project_manager, policy):
        from datetime import datetime, timezone

        from backend.core.ids import new_id
        from backend.core.models.media import Media, MediaMetadata
        from backend.core.models.project import ProjectCreate
        from backend.database.repositories.media import MediaRepository
        from backend.database.repositories.timeline import TimelineRepository

        project = project_manager.create(
            ProjectCreate(name="Timeline", target_duration_seconds=600)
        )
        now = datetime.now(timezone.utc)
        media = MediaRepository(database).create(
            Media(
                id=new_id("media"),
                project_id=project.id,
                source_path="D:/recordings/session.mp4",
                filename="session.mp4",
                container=".mp4",
                size_bytes=1024,
                checksum="0" * 64,
                metadata=MediaMetadata(duration_seconds=10_000.0),
                created_at=now,
                updated_at=now,
            )
        )
        clips = [
            PlannedClip(
                media_id=media.id,
                source_start=index * 100.0,
                source_end=index * 100.0 + 40.0,
                # No moment_id: the row references moments(id), and this
                # fixture is about the timeline, not about where clips come
                # from. A hand-added clip has none either.
                moment_type=MomentType.EPIC,
                score=0.5 + index / 100.0,
                role="hook" if index == 0 else "body",
            )
            for index in range(5)
        ]
        timeline = build_timeline(
            clips, project_id=project.id, policy=policy, media_durations={media.id: 10_000.0}
        ).timeline
        repository = TimelineRepository(database)
        repository.replace(project.id, timeline)
        return project, repository, timeline

    def test_a_round_trip_preserves_every_clip(self, stored) -> None:
        project, repository, timeline = stored
        loaded = repository.load(project.id)

        assert len(loaded.video_clips()) == len(timeline.video_clips())
        for original, clip in zip(
            timeline.video_clips(), loaded.video_clips(), strict=True
        ):
            assert clip.id == original.id
            assert clip.source_in == pytest.approx(original.source_in)
            assert clip.source_out == pytest.approx(original.source_out)
            assert clip.moment_type is original.moment_type
            assert clip.role == original.role

    def test_a_rebuild_does_not_overrule_the_user(self, stored) -> None:
        # §78: re-running the pipeline must not re-enable a clip they removed.
        project, repository, timeline = stored
        target = timeline.video_clips()[2]
        repository.set_enabled(project.id, target.id, enabled=False)

        repository.replace(project.id, timeline)

        assert repository.load(project.id).clip(target.id).enabled is False

    def test_an_explicit_edit_is_saved_rather_than_reverted(self, stored) -> None:
        # The other half of the same rule: a *user* edit being saved must win
        # over the stored state, or nothing could ever be changed.
        project, repository, timeline = stored
        target = timeline.video_clips()[2]

        edited = operations.delete(timeline, target.id)
        repository.replace(project.id, edited, preserve_user_state=False)

        reloaded = repository.load(project.id)
        assert reloaded.clip(target.id).enabled is False
        assert reloaded.duration == pytest.approx(timeline.duration - target.duration)

    def test_the_stored_duration_counts_only_what_is_shown(self, stored) -> None:
        project, repository, timeline = stored
        target = timeline.video_clips()[1]
        repository.set_enabled(project.id, target.id, enabled=False)

        assert repository.duration_seconds(project.id) == pytest.approx(
            timeline.duration - target.duration
        )

    def test_replacing_a_timeline_leaves_no_orphaned_rows(self, stored, policy) -> None:
        project, repository, timeline = stored
        media_id = timeline.video_clips()[0].media_id

        smaller = build_timeline(
            [PlannedClip(media_id=media_id, source_start=0.0, source_end=40.0)],
            project_id=project.id,
            policy=policy,
        ).timeline
        repository.replace(project.id, smaller)

        assert repository.clip_count(project.id) == 1

    def test_effects_are_stored_relative_to_the_clip_they_decorate(
        self, stored, database
    ) -> None:
        # The schema says clip-relative; the planner works in absolute timeline
        # coordinates. Converting on write is what lets a clip move without
        # every effect on it silently pointing at the wrong second.
        from backend.core.models.enums import EffectCategory, EffectEngine, EffectType
        from backend.effects.models import EffectInstance

        project, repository, timeline = stored
        clip = timeline.video_clips()[2]
        absolute = clip.timeline_start + 4.0

        repository.replace(
            project.id,
            timeline,
            effects=[
                EffectInstance(
                    effect=EffectType.ZOOM,
                    engine=EffectEngine.FFMPEG,
                    category=EffectCategory.CAMERA,
                    start_seconds=absolute,
                    duration_seconds=1.5,
                    clip_id=clip.id,
                )
            ],
        )

        row = database.fetch_one(
            "SELECT start_seconds FROM timeline_effects WHERE clip_id = ?", (clip.id,)
        )
        assert row["start_seconds"] == pytest.approx(4.0)

    def test_a_reorder_survives_the_unique_index_on_clip_index(self, stored) -> None:
        # Updating indices one row at a time collides the moment two clips swap
        # places, because the unique index is checked per statement.
        project, repository, timeline = stored
        last = timeline.video_clips()[-1]

        repository.save_edit(project.id, operations.move(timeline, last.id, 0))

        reloaded = repository.load(project.id)
        assert reloaded.video_clips()[0].id == last.id
        assert [clip.clip_index for clip in reloaded.video_clips()] == list(range(5))

    def test_an_edit_moves_captions_with_their_clip(self, stored, database) -> None:
        # §71 after an edit: a caption whose clip moved but which did not would
        # drift by exactly the length of whatever was removed.
        from backend.timeline.captions import Caption

        project, repository, timeline = stored
        third = timeline.video_clips()[2]
        caption = Caption(
            id=derived_id("caption", third.id, 1.0),
            index=0,
            timeline_start=third.timeline_start + 1.0,
            timeline_end=third.timeline_start + 3.0,
            text="right here",
            clip_id=third.id,
        )
        repository.replace(project.id, timeline, captions=[caption])

        removed = timeline.video_clips()[0]
        repository.save_edit(project.id, operations.delete(timeline, removed.id))

        reloaded = repository.load(project.id)
        moved = reloaded.clip(third.id)
        stored_caption = repository.list_captions(project.id)[0]
        assert stored_caption.timeline_start == pytest.approx(moved.timeline_start + 1.0)
