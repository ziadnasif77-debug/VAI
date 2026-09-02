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
from backend.core.models.enums import (
    EffectCategory,
    EffectEngine,
    EffectType,
    MomentType,
    TrackKind,
    TransitionType,
)
from backend.effects.models import EffectInstance
from backend.rendering.encoder import EncodeTarget
from backend.rendering.ffmpeg_renderer import (
    _effect_filters,
    _effects_token,
    _realised,
    _retime_token,
    _warp_graph,
)
from backend.timeline import captions as caption_builder
from backend.timeline import operations, retime, validation
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
        report = validation.validate(timeline, media_durations={MEDIA: 100_000.0}, policy=policy)

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

        report = validation.validate(timeline, media_durations={MEDIA: 100_000.0}, policy=policy)
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
        planned = [PlannedClip(media_id=MEDIA, source_start=0.0, source_end=9_999.0, score=0.5)]
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

        assert all(clip.duration >= MIN_CLIP_SECONDS for clip in result.timeline.video_clips())


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

        captions = caption_builder.build_captions(timeline, {MEDIA: segments}, config.captions)

        assert len(captions) == 1
        assert captions[0].timeline_start == pytest.approx(clip.timeline_start + 10.0)

    def test_speech_the_edit_cut_produces_no_caption(self, timeline, config) -> None:
        # Nobody says those words in the finished video.
        first, second = timeline.video_clips()[0], timeline.video_clips()[1]
        between = (first.source_out + second.source_in) / 2
        segments = self._segments((between, between + 2.0, "words that were cut"))

        captions = caption_builder.build_captions(timeline, {MEDIA: segments}, config.captions)

        assert captions == []

    def test_a_caption_never_spills_past_its_clip(self, timeline, config) -> None:
        clip = timeline.video_clips()[0]
        segments = self._segments((clip.source_out - 1.0, clip.source_out + 20.0, "a b c d"))

        captions = caption_builder.build_captions(timeline, {MEDIA: segments}, config.captions)

        for caption in captions:
            assert caption.timeline_end <= clip.timeline_end + 1e-6

    def test_word_timings_are_carried_into_timeline_coordinates(self, timeline, config) -> None:
        clip = timeline.video_clips()[2]
        start = clip.source_in + 5.0
        segments = self._segments((start, start + 4.0, "one two three four"))

        captions = caption_builder.build_captions(timeline, {MEDIA: segments}, config.captions)

        assert captions[0].words
        for _word, word_start, word_end in captions[0].words:
            assert clip.timeline_start <= word_start <= word_end <= clip.timeline_end

    def test_captions_never_overlap_each_other(self, timeline, config) -> None:
        clip = timeline.video_clips()[0]
        base = clip.source_in + 2.0
        segments = self._segments(
            *[(base + index * 1.0, base + index * 1.0 + 0.6, f"line {index}") for index in range(8)]
        )

        captions = caption_builder.build_captions(timeline, {MEDIA: segments}, config.captions)

        for previous, following in pairwise(captions):
            assert previous.timeline_end <= following.timeline_start + 1e-6

    def test_captions_are_indexed_in_timeline_order(self, timeline, config) -> None:
        clips = timeline.video_clips()
        segments = self._segments(
            (clips[2].source_in + 1.0, clips[2].source_in + 3.0, "later words"),
            (clips[0].source_in + 1.0, clips[0].source_in + 3.0, "earlier words"),
        )

        captions = caption_builder.build_captions(timeline, {MEDIA: segments}, config.captions)

        assert [caption.index for caption in captions] == list(range(len(captions)))
        assert captions[0].text == "earlier words"

    def test_disabling_captions_produces_none(self, timeline, config) -> None:
        segments = self._segments((0.0, 2.0, "anything"))
        disabled = config.captions.model_copy(update={"enabled": False})

        assert caption_builder.build_captions(timeline, {MEDIA: segments}, disabled) == []

    def test_srt_carries_the_timeline_timings(self, timeline, config) -> None:
        clip = timeline.video_clips()[0]
        segments = self._segments((clip.source_in + 1.0, clip.source_in + 3.0, "hello there"))
        captions = caption_builder.build_captions(timeline, {MEDIA: segments}, config.captions)

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
        for original, clip in zip(timeline.video_clips(), loaded.video_clips(), strict=True):
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

    def test_effects_are_stored_relative_to_the_clip_they_decorate(self, stored, database) -> None:
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

    def test_stored_effects_read_back_for_the_renderers(self, stored) -> None:
        # The writer existed from Phase 8 and nothing ever read the rows:
        # seventeen planned effects across three real projects, none in any
        # finished video. The reader must return exactly what was planned --
        # including a library magnitude that happens to be named "strength",
        # which the old writer clobbered with the instance strength (blur
        # stored 0.6 where the scaled 0.24 belonged).
        project, repository, timeline = stored
        clip = timeline.video_clips()[1]
        repository.replace(
            project.id,
            timeline,
            effects=[
                EffectInstance(
                    effect=EffectType.BLUR,
                    engine=EffectEngine.FFMPEG,
                    category=EffectCategory.LIGHT,
                    start_seconds=clip.timeline_start + 2.0,
                    duration_seconds=1.0,
                    clip_id=clip.id,
                    params={"strength": 0.24, "mode": "background"},
                    strength=0.6,
                    reason="blur on a tension moment",
                ),
                EffectInstance(
                    effect=EffectType.TEXT_POP,
                    engine=EffectEngine.REMOTION,
                    category=EffectCategory.TEXT,
                    start_seconds=clip.timeline_start + 5.0,
                    duration_seconds=1.2,
                    clip_id=clip.id,
                    params={"text": "VICTORY"},
                    strength=0.6,
                ),
            ],
        )

        loaded = repository.list_effects(project.id)

        assert len(loaded) == 2
        blur = next(item for item in loaded if item.effect is EffectType.BLUR)
        assert blur.engine is EffectEngine.FFMPEG
        assert blur.category is EffectCategory.LIGHT
        assert blur.start_seconds == pytest.approx(2.0), "clip-relative, as stored"
        assert blur.strength == pytest.approx(0.6)
        assert blur.params["strength"] == pytest.approx(0.24), (
            "the library magnitude must survive the instance strength"
        )
        assert blur.reason == "blur on a tension moment"
        pop = next(item for item in loaded if item.effect is EffectType.TEXT_POP)
        assert pop.engine is EffectEngine.REMOTION
        assert pop.params["text"] == "VICTORY"

    def test_a_legacy_row_gives_up_its_strength_key_entirely(self, stored, database) -> None:
        # Rows written before the reserved key carried the instance strength
        # under "strength". Reading it back must REMOVE it from params: left
        # in, a renderer would take the instance value for a library
        # magnitude, and the same effect would hash differently before and
        # after a re-plan rewrites the row.
        import json

        project, repository, timeline = stored
        clip = timeline.video_clips()[0]
        database.execute(
            "INSERT INTO timeline_effects (id, project_id, clip_id, effect_type, "
            "start_seconds, duration_seconds, parameters, enabled) VALUES "
            "(?, ?, ?, 'zoom', 2.0, 1.5, ?, 1)",
            (
                "timeline_effect-legacyrow0000",
                project.id,
                clip.id,
                json.dumps(
                    {"scale": 1.07, "engine": "ffmpeg", "category": "camera",
                     "strength": 0.6, "reason": "legacy"}
                ),
            ),
        )

        loaded = repository.list_effects(project.id)

        legacy = next(item for item in loaded if item.reason == "legacy")
        assert legacy.strength == pytest.approx(0.6)
        assert "strength" not in legacy.params
        assert legacy.params["scale"] == pytest.approx(1.07)

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

    def test_a_split_adds_a_row_rather_than_being_lost(self, stored) -> None:
        # save_edit was written for edits that only move clips, so a split --
        # which adds one and changes a source span -- wrote a row whose stored
        # span no longer matched its timeline span, and the model refused to
        # load it.
        project, repository, timeline = stored
        clip = timeline.video_clips()[2]
        middle = (clip.timeline_start + clip.timeline_end) / 2

        repository.save_edit(project.id, operations.split(timeline, clip.id, middle))

        reloaded = repository.load(project.id)
        assert len(reloaded.video_clips()) == 6
        assert reloaded.duration == pytest.approx(timeline.duration)

    def test_a_trim_writes_the_new_source_points(self, stored) -> None:
        project, repository, timeline = stored
        clip = timeline.video_clips()[1]

        repository.save_edit(
            project.id, operations.trim(timeline, clip.id, start_delta=2.0, end_delta=-3.0)
        )

        trimmed = repository.load(project.id).clip(clip.id)
        assert trimmed.source_in == pytest.approx(clip.source_in + 2.0)
        assert trimmed.source_out == pytest.approx(clip.source_out - 3.0)

    def test_removing_a_clip_takes_it_out_of_the_rows(self, stored) -> None:
        # `remove` is not `delete`: the row goes, and so do the captions that
        # described footage no longer in the edit.
        project, repository, timeline = stored
        clip = timeline.video_clips()[0]

        repository.save_edit(project.id, operations.remove(timeline, clip.id))

        assert repository.clip_count(project.id) == 4
        assert repository.load(project.id).clip(clip.id) is None

    def test_a_relaid_timeline_survives_the_round_trip(self, stored) -> None:
        # The whole EDL-side of the warp: plan a freeze on a stored timeline,
        # re-lay, persist, reload. The reloaded clip must carry its extended
        # span, its warp metadata, and an effect row whose clip-relative time
        # is the anchor -- because the renderer reads all three from the rows,
        # not from the objects that wrote them.
        project, repository, timeline = stored
        target = timeline.video_clips()[1]
        freeze = EffectInstance(
            effect=EffectType.FREEZE_FRAME,
            engine=EffectEngine.FFMPEG,
            category=EffectCategory.TIME,
            start_seconds=target.timeline_start + 12.0,
            duration_seconds=1.5,
            clip_id=target.id,
            params={"max_duration_seconds": 1.5},
        )
        relaid = retime.relay_timeline(timeline, [freeze])
        repository.replace(project.id, relaid.timeline, effects=relaid.effects)

        loaded = repository.load(project.id)
        clip = loaded.clip(target.id)
        assert clip is not None
        assert clip.duration == pytest.approx(target.duration + 1.5)
        assert retime.clip_retime(clip) is not None
        assert loaded.duration == pytest.approx(timeline.duration + 1.5)

        rows = repository.list_effects(project.id)
        assert len(rows) == 1
        assert rows[0].start_seconds == pytest.approx(12.0), "stored clip-relative"
        assert retime.clip_retime(clip).at_seconds == pytest.approx(12.0)


class TestSourceExclusivity:
    """Each second of source appears at most once (§42, and a real viewer).

    The narrative stage should guarantee this; the first real edit proved it
    can fail to (a replayed hook, and two context windows overlapping by
    1.2s after §29 expansion). The builder is the boundary where "the same
    footage twice" is stopped regardless of which upstream stage slipped.
    """

    def _build(self, clips, policy=None):
        from backend.core.duration import DurationPolicy
        from backend.timeline.builder import build_timeline

        return build_timeline(
            clips,
            project_id="proj-x",
            policy=policy
            or DurationPolicy(
                min_seconds=600,
                max_seconds=3600,
                presets_seconds=(600,),
                default_seconds=600,
                tolerance_seconds=60.0,
                tolerance_ratio=0.1,
            ),
            target_seconds=600.0,
        )

    def test_an_edge_overlap_is_trimmed_from_the_later_clip(self) -> None:
        # The overlap is trimmed off the clip that arrives second, and what
        # is left still runs forwards. The original fixture here put 53-78s
        # before 0-78s and expected 53->78 then 0->53; V2's constitution
        # forbids exactly that output, so the rule is now tested with an
        # overlap the edit can legally survive.
        result = self._build(
            [
                PlannedClip(media_id=MEDIA, source_start=0.0, source_end=78.0, score=0.9),
                PlannedClip(media_id=MEDIA, source_start=53.0, source_end=100.0, score=0.5),
            ]
        )

        clips = result.timeline.video_clips()
        assert [(c.source_in, c.source_out) for c in clips] == [(0.0, 78.0), (78.0, 100.0)]
        assert any("already in the edit" in note for note in result.notes)

    def test_a_fully_contained_clip_is_dropped_with_a_note(self) -> None:
        result = self._build(
            [
                PlannedClip(media_id=MEDIA, source_start=0.0, source_end=100.0, score=0.9),
                PlannedClip(media_id=MEDIA, source_start=20.0, source_end=40.0, score=0.5),
            ]
        )

        assert len(result.timeline.video_clips()) == 1
        assert any("dropped" in note for note in result.notes)

    def test_the_same_seconds_on_different_media_are_untouched(self) -> None:
        result = self._build(
            [
                PlannedClip(media_id=MEDIA, source_start=0.0, source_end=30.0, score=0.9),
                PlannedClip(media_id="other", source_start=0.0, source_end=30.0, score=0.9),
            ]
        )

        assert len(result.timeline.video_clips()) == 2

    def test_a_sliver_below_the_minimum_is_dropped_not_kept(self) -> None:
        # 1.5s of unseen footage is a glitch, not a shot.
        result = self._build(
            [
                PlannedClip(media_id=MEDIA, source_start=10.0, source_end=40.0, score=0.9),
                PlannedClip(media_id=MEDIA, source_start=8.5, source_end=40.0, score=0.5),
            ]
        )

        assert len(result.timeline.video_clips()) == 1

    def test_disjoint_clips_pass_untouched(self) -> None:
        result = self._build(
            [
                PlannedClip(media_id=MEDIA, source_start=0.0, source_end=30.0, score=0.9),
                PlannedClip(media_id=MEDIA, source_start=100.0, source_end=130.0, score=0.9),
            ]
        )

        assert len(result.timeline.video_clips()) == 2
        assert not any("already in the edit" in note for note in result.notes)


class TestCaptionConfidenceFloor:
    """§71 + the honesty rule: a caption the model did not believe is not shown.

    Measured on a real project: 95 of 129 transcript segments sat below 0.45
    confidence, including Whisper's classic Arabic subtitle hallucinations
    ("المترجم...") at 0.08 — burned into the finished picture. The speech
    still plays; only the caption is withheld.
    """

    def _clip(self) -> TimelineClip:
        return TimelineClip(
            id="clip-000000000cap",
            media_id="media-aaaaaaaaaaaa",
            clip_index=0,
            source_in=0.0,
            source_out=30.0,
            timeline_start=0.0,
            timeline_end=30.0,
        )

    def _timeline(self) -> Timeline:
        return Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
            Track(kind=TrackKind.VIDEO, clips=(self._clip(),))
        )

    def test_a_hallucinated_segment_is_not_captioned(self, config) -> None:
        segments = {
            "media-aaaaaaaaaaaa": [
                TranscriptSegment(start=1.0, end=3.0, text="كلام حقيقي", confidence=0.8),
                TranscriptSegment(start=5.0, end=7.0, text="المترجم للقناة", confidence=0.08),
            ]
        }

        captions = caption_builder.build_captions(self._timeline(), segments, config.captions)

        texts = [item.text for item in captions]
        assert "كلام حقيقي" in " ".join(texts)
        assert all("المترجم" not in text for text in texts)

    def test_an_unscored_segment_is_given_the_benefit_of_the_doubt(self, config) -> None:
        # A provider that reports no confidence (the fake, an old analysis)
        # must not lose every caption to a floor it never measured against.
        segments = {
            "media-aaaaaaaaaaaa": [
                TranscriptSegment(start=1.0, end=3.0, text="بلا تقييم", confidence=None),
            ]
        }

        captions = caption_builder.build_captions(self._timeline(), segments, config.captions)

        assert any("بلا تقييم" in item.text for item in captions)


class TestTimeJumpGrammar:
    """§40 + film grammar: a hard cut says "continuous time".

    Every cut in the edit was hard, and a chronological gaming edit jumps
    minutes of session time at nearly every join -- so each join claimed a
    continuity it did not have. A short dip to black is the standard signal
    for time passing, and until this existed no transition reached the
    picture at all: FADE and DIP_TO_BLACK were honoured by the audio mix and
    silently dropped by every frame.
    """

    def _built(self, config, *spans, media=None):
        clips = [
            PlannedClip(
                media_id=(media or ["m"] * len(spans))[index],
                source_start=start,
                source_end=end,
            )
            for index, (start, end) in enumerate(spans)
        ]
        result = build_timeline(
            clips,
            project_id="proj-aaaaaaaaaaaa",
            policy=config.output.duration_policy(),
            transitions=config.narrative.transitions,
        )
        return list(result.timeline.track(TrackKind.VIDEO).clips)

    def test_a_small_gap_stays_a_hard_cut(self, config) -> None:
        # Two seconds of trimmed footage is a beat, not a scene change --
        # dipping to black there would read as the video stuttering.
        clips = self._built(config, (0.0, 30.0), (32.0, 60.0))

        assert clips[0].transition_out is TransitionType.CUT
        assert clips[1].transition_in is TransitionType.CUT

    def test_a_time_jump_earns_a_dip(self, config) -> None:
        clips = self._built(config, (0.0, 30.0), (300.0, 340.0))

        assert clips[0].transition_out is TransitionType.DIP_TO_BLACK
        assert clips[1].transition_in is TransitionType.DIP_TO_BLACK

    def test_a_change_of_recording_is_a_change_of_session(self, config) -> None:
        clips = self._built(config, (0.0, 30.0), (10.0, 40.0), media=["m", "other"])

        assert clips[0].transition_out is TransitionType.DIP_TO_BLACK
        assert clips[1].transition_in is TransitionType.DIP_TO_BLACK

    def test_the_dip_stays_under_the_qa_black_floor(self, config) -> None:
        # blackdetect's floor is 0.5s (§76). A dip that long would make the
        # QA black check argue with the grammar on every render.
        assert config.narrative.transitions.dip_seconds < 0.5
        clips = self._built(config, (0.0, 30.0), (300.0, 340.0))
        assert clips[1].metadata["fade_in_seconds"] < 0.5

    def test_the_picture_actually_fades(self, config) -> None:
        # Three clips so the middle one has no programme edge: cut in from
        # clip 0, dip out toward clip 2. Exactly one fade, on the right end.
        from backend.rendering.ffmpeg_renderer import _fade_filter

        clips = self._built(config, (0.0, 30.0), (32.0, 60.0), (300.0, 340.0))

        middle = _fade_filter(clips[1])
        assert "fade=t=out" in middle
        assert middle.count("fade=") == 1, "a cut edge grew a fade"
        assert "fade=t=in" in _fade_filter(clips[2])

    def test_a_reused_segment_carries_its_fades_in_its_name(self, config) -> None:
        # Segment reuse checks duration, and a 0.3s dip baked into the file
        # does not change its length -- the name is what keeps a re-render
        # from splicing in yesterday's fade-less segment.
        from backend.rendering.ffmpeg_renderer import _fade_token

        dipped = self._built(config, (0.0, 30.0), (300.0, 340.0), (302.0, 330.0))
        continuous = self._built(config, (0.0, 30.0), (32.0, 60.0), (62.0, 90.0))

        # Middle clips: same position, same duration check -- the token is the
        # only thing telling the dipped one from the plain one.
        assert _fade_token(dipped[1]) != ""
        assert _fade_token(continuous[1]) == ""

    def test_a_medium_jump_keeps_the_cut_and_marks_the_whoosh(self, config) -> None:
        # Passage of time, not an act break: the hard cut stays and the
        # audio grammar (the whoosh reads this marker) says what happened.
        clips = self._built(config, (0.0, 30.0), (90.0, 120.0))

        assert clips[0].transition_out is TransitionType.CUT
        assert clips[1].transition_in is TransitionType.CUT
        assert clips[1].metadata.get("time_jump") == "medium"

    def test_the_dip_budget_goes_to_the_largest_jumps(self, config) -> None:
        # Measured before the tiers: seven dips in ten clips, a strobing
        # edit. Four act-sized jumps, budget two: the two largest dip, the
        # rest keep their cuts with the medium marker.
        clips = self._built(
            config,
            (0.0, 10.0),
            (200.0, 210.0),    # gap 190
            (500.0, 510.0),    # gap 290
            (900.0, 910.0),    # gap 390
            (1400.0, 1410.0),  # gap 490
        )

        dips = [c.transition_in is TransitionType.DIP_TO_BLACK for c in clips[1:]]
        assert dips == [False, False, True, True], "largest two claim the budget"
        assert clips[1].metadata.get("time_jump") == "medium"
        assert clips[2].metadata.get("time_jump") == "medium"

    def test_a_recording_change_dips_outside_the_budget(self, config) -> None:
        clips = self._built(
            config,
            (0.0, 10.0),
            (400.0, 410.0),
            (900.0, 910.0),
            (10.0, 20.0),
            media=["m", "m", "m", "other"],
        )

        assert clips[1].transition_in is TransitionType.DIP_TO_BLACK
        assert clips[2].transition_in is TransitionType.DIP_TO_BLACK
        assert clips[3].transition_in is TransitionType.DIP_TO_BLACK, (
            "a new session is an act break regardless of the budget"
        )

    def test_disabled_means_every_join_is_a_cut(self, config) -> None:
        narrowed = config.narrative.transitions.model_copy(update={"enabled": False})
        clips = [
            PlannedClip(media_id="m", source_start=0.0, source_end=30.0),
            PlannedClip(media_id="m", source_start=300.0, source_end=340.0),
        ]
        result = build_timeline(
            clips,
            project_id="proj-aaaaaaaaaaaa",
            policy=config.output.duration_policy(),
            transitions=narrowed,
        )
        laid = list(result.timeline.track(TrackKind.VIDEO).clips)

        assert laid[0].transition_out is TransitionType.CUT


class TestCaptionLanguage:
    """Direction comes from evidence: the stored language, else the script.

    Transcripts analysed before the provider carried the detected language
    through left ``language`` NULL on every row -- and the only consumer of a
    caption's language is ``is_rtl``, which then never fired: two real Arabic
    projects rendered every caption left-to-right.
    """

    def _built(self, config, segment: TranscriptSegment):
        clip = TimelineClip(
            id="clip-00000000lang",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=30.0,
            timeline_start=0.0,
            timeline_end=30.0,
        )
        timeline = Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
            Track(kind=TrackKind.VIDEO, clips=(clip,))
        )
        return caption_builder.build_captions(timeline, {MEDIA: [segment]}, config.captions)

    def test_arabic_text_reads_as_arabic_without_a_stored_language(self, config) -> None:
        captions = self._built(
            config, TranscriptSegment(start=1.0, end=3.0, text="ضربة قاضية", language=None)
        )

        assert captions and captions[0].language == "ar"

    def test_latin_text_stays_unlabelled_rather_than_guessed(self, config) -> None:
        # "Not right-to-left" is all the script proves; claiming "en" for it
        # would be invention, and nothing downstream needs more than direction.
        captions = self._built(
            config, TranscriptSegment(start=1.0, end=3.0, text="nice shot", language=None)
        )

        assert captions and captions[0].language is None

    def test_a_stored_language_always_wins(self, config) -> None:
        captions = self._built(
            config, TranscriptSegment(start=1.0, end=3.0, text="ضربة قاضية", language="en")
        )

        assert captions and captions[0].language == "en"

    def test_the_first_strong_letter_decides_a_code_switched_line(self, config) -> None:
        # Arabic gaming commentary code-switches constantly. A Latin-first
        # line with one Arabic word must stay left-to-right (UAX#9's
        # first-strong rule), not flip because any Arabic letter appears.
        captions = self._built(
            config,
            TranscriptSegment(start=1.0, end=3.0, text="nice shot يا شباب", language=None),
        )

        assert captions and captions[0].language is None

    def test_arabic_punctuation_alone_decides_nothing(self, config) -> None:
        # Whisper emits the Arabic comma and question mark inside otherwise
        # Latin lines; punctuation has no direction of its own.
        captions = self._built(
            config,
            TranscriptSegment(start=1.0, end=3.0, text="gg wp ؟", language=None),
        )

        assert captions and captions[0].language is None


class TestBakedEffects:
    """§68's FFmpeg half, finally realised.

    The planner stored these rows from Phase 8 on -- measured: fifteen
    ffmpeg-engine effects across three real projects, positioned to the
    centisecond -- and no renderer ever read them. Everything realised here is
    duration-neutral by construction: no filter changes a timestamp or the
    frame count, which is what keeps every §76 QA gate untouched.
    """

    def _clip(self, duration: float = 40.0) -> TimelineClip:
        return TimelineClip(
            id="clip-000000000efx",
            media_id=MEDIA,
            clip_index=0,
            source_in=10.0,
            source_out=10.0 + duration,
            timeline_start=0.0,
            timeline_end=duration,
        )

    @staticmethod
    def _effect(effect_type, category, start: float, duration: float, params=None):
        return EffectInstance(
            effect=effect_type,
            engine=EffectEngine.FFMPEG,
            category=category,
            start_seconds=start,
            duration_seconds=duration,
            clip_id="clip-000000000efx",
            params=params or {},
        )

    @staticmethod
    def _target(width: int = 1920, height: int = 1080) -> EncodeTarget:
        return EncodeTarget(width=width, height=height, fps=60)

    def test_each_realisable_effect_becomes_its_filter(self) -> None:
        clip = self._clip()
        effects = _realised(
            clip,
            [
                self._effect(
                    EffectType.ZOOM, EffectCategory.CAMERA, 19.45, 1.76, {"scale": 1.072}
                ),
                self._effect(
                    EffectType.CINEMATIC_BARS, EffectCategory.FRAME, 10.0, 3.5, {"ratio": 2.39}
                ),
                self._effect(
                    EffectType.FLASH, EffectCategory.LIGHT, 2.0, 0.12, {"peak_opacity": 0.33}
                ),
                self._effect(
                    EffectType.CAMERA_SHAKE,
                    EffectCategory.CAMERA,
                    30.0,
                    0.44,
                    {"amplitude_px": 4.8, "frequency_hz": 14.0},
                ),
            ],
        )
        chain = _effect_filters(clip, effects, self._target())

        assert "zoompan=" in chain
        assert chain.count("drawbox=") == 2, "letterbox is a top bar and a bottom bar"
        assert "eq=brightness=" in chain
        assert "pad=iw+" in chain and "crop=1920:1080" in chain

    def test_camera_moves_come_before_frame_furniture(self) -> None:
        # A letterbox drawn first and zoomed after visibly thickens by the
        # zoom factor and snaps back. Geometry reshapes the world; bars are
        # drawn on the finished frame -- whatever the start-time order says.
        chain = _effect_filters(
            self._clip(),
            _realised(
                self._clip(),
                [
                    self._effect(
                        EffectType.CINEMATIC_BARS,
                        EffectCategory.FRAME,
                        2.4,
                        3.5,
                        {"ratio": 2.39},
                    ),
                    self._effect(
                        EffectType.PUNCH_IN, EffectCategory.CAMERA, 5.0, 1.2, {"scale": 1.12}
                    ),
                ],
            ),
            self._target(),
        )

        assert chain.index("zoompan=") < chain.index("drawbox="), (
            "the earlier-starting bars must still be drawn after the zoom"
        )

    def test_bars_cover_a_quarter_of_the_frame_not_all_of_it(self) -> None:
        # 1080 minus 1920/2.39 leaves two 138px bars: ~26% of the frame.
        # QA's blackdetect needs the whole picture dark (§76), so a
        # letterboxed beat can never read as a broken black run.
        chain = _effect_filters(
            self._clip(),
            [
                self._effect(
                    EffectType.CINEMATIC_BARS, EffectCategory.FRAME, 10.0, 3.5, {"ratio": 2.39}
                )
            ],
            self._target(),
        )

        assert "h=138" in chain and "y=ih-138" in chain

    def test_bars_on_a_portrait_target_are_skipped(self) -> None:
        # The same 2.39 arithmetic on a 9:16 Shorts render would yield two
        # 734px bars -- 76% of the picture black. That is a blackout, not a
        # cinematic beat, and it would trip QA's black check besides.
        chain = _effect_filters(
            self._clip(),
            [
                self._effect(
                    EffectType.CINEMATIC_BARS, EffectCategory.FRAME, 10.0, 3.5, {"ratio": 2.39}
                )
            ],
            self._target(width=1080, height=1920),
        )

        assert "drawbox" not in chain

    def test_time_warping_effects_stay_unrealised(self) -> None:
        # slow_motion changes playback length; realising it as a picture
        # filter without re-laying the EDL would break the §76 duration gate.
        kept = _realised(
            self._clip(),
            [self._effect(EffectType.SLOW_MOTION, EffectCategory.TIME, 5.0, 1.5, {"rate": 0.5})],
        )

        assert kept == []

    def test_an_effect_past_the_clip_end_is_dropped(self) -> None:
        kept = _realised(
            self._clip(duration=8.0),
            [self._effect(EffectType.FLASH, EffectCategory.LIGHT, 9.0, 0.12, {})],
        )

        assert kept == []

    def test_the_frame_rate_is_conformed_before_a_zoom(self) -> None:
        # zoompan regenerates timestamps at its own rate: fed a lower-rate
        # source it would time-compress the clip. The chain must open with an
        # fps conform whenever a zoom is present.
        chain = _effect_filters(
            self._clip(),
            [self._effect(EffectType.PUNCH_IN, EffectCategory.CAMERA, 5.0, 1.2, {"scale": 1.12})],
            self._target(),
        )

        assert chain.startswith(",fps=60")

    def test_the_segment_name_carries_the_effects(self) -> None:
        # Segment reuse checks duration, and a zoom baked into the file does
        # not change its length -- the token is the only thing telling a
        # decorated segment from yesterday's plain one (§47).
        zoomed = [
            self._effect(EffectType.ZOOM, EffectCategory.CAMERA, 19.45, 1.76, {"scale": 1.072})
        ]
        stronger = [
            self._effect(EffectType.ZOOM, EffectCategory.CAMERA, 19.45, 1.76, {"scale": 1.2})
        ]

        assert _effects_token([]) == ""
        assert _effects_token(zoomed) != ""
        assert _effects_token(zoomed) != _effects_token(stronger), (
            "a parameter change must re-cut the segment"
        )


class TestTimeWarpRelay:
    """Doctrine §11/§12: a time-warp's seconds exist on the timeline first.

    The renderer refused freeze_frame and speed_ramp for a stated reason --
    baking them into a segment without the EDL knowing would falsify every
    duration in the system. The re-lay is the fix: the clip's timeline span
    grows by the warp's added seconds, everything after it shifts, and the
    warp's shape rides in clip metadata for the renderer and the audio graph
    to read back.
    """

    def _clips(self) -> list[TimelineClip]:
        return [
            TimelineClip(
                id=f"clip-relay{index:06d}",
                media_id=MEDIA,
                clip_index=index,
                source_in=index * 100.0,
                source_out=index * 100.0 + 10.0,
                timeline_start=index * 10.0,
                timeline_end=index * 10.0 + 10.0,
            )
            for index in range(3)
        ]

    def _timeline(self, clips) -> Timeline:
        return Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
            Track(kind=TrackKind.VIDEO, clips=tuple(clips))
        )

    @staticmethod
    def _freeze(clip: TimelineClip, at: float = 4.0, hold: float = 1.5) -> EffectInstance:
        return EffectInstance(
            effect=EffectType.FREEZE_FRAME,
            engine=EffectEngine.FFMPEG,
            category=EffectCategory.TIME,
            start_seconds=clip.timeline_start + at,
            duration_seconds=hold,
            clip_id=clip.id,
            params={"max_duration_seconds": 1.5},
        )

    @staticmethod
    def _ramp(
        clip: TimelineClip, at: float = 3.0, window: float = 0.8, factor: float = 0.4
    ) -> EffectInstance:
        return EffectInstance(
            effect=EffectType.SPEED_RAMP,
            engine=EffectEngine.FFMPEG,
            category=EffectCategory.TIME,
            start_seconds=clip.timeline_start + at,
            duration_seconds=window,
            clip_id=clip.id,
            params={"slow_factor": factor},
        )

    def test_a_frozen_clip_occupies_its_source_span_plus_the_hold(self) -> None:
        clips = self._clips()
        relaid = retime.relay_timeline(self._timeline(clips), [self._freeze(clips[1])])

        frozen = relaid.timeline.video_clips()[1]
        assert frozen.duration == pytest.approx(11.5)
        assert frozen.source_duration == pytest.approx(10.0), "the source span never moves"
        assert frozen.metadata["retime"]["extra_seconds"] == pytest.approx(1.5)

    def test_clips_after_the_warp_shift_and_the_track_stays_contiguous(self) -> None:
        clips = self._clips()
        relaid = retime.relay_timeline(self._timeline(clips), [self._freeze(clips[1])])

        laid = relaid.timeline.video_clips()
        assert laid[0].timeline_start == 0.0
        assert laid[2].timeline_start == pytest.approx(21.5)
        assert relaid.timeline.duration == pytest.approx(31.5)
        assert validation.validate(relaid.timeline).is_valid

    def test_a_ramp_occupies_the_sum_of_its_phases(self) -> None:
        # 0.8 s of source at 0.4 plays for 2.0 s: the clip gains 1.2 s.
        clips = self._clips()
        relaid = retime.relay_timeline(self._timeline(clips), [self._ramp(clips[0])])

        ramped = relaid.timeline.video_clips()[0]
        assert ramped.duration == pytest.approx(11.2)
        assert ramped.metadata["retime"]["factor"] == pytest.approx(0.4)
        assert relaid.timeline.duration == pytest.approx(31.2)

    def test_effect_anchors_stay_clip_relative_across_the_shift(self) -> None:
        # The repository stores times relative to the clip; a shifted clip's
        # effects must shift with it or the subtraction stores garbage.
        clips = self._clips()
        zoom = EffectInstance(
            effect=EffectType.ZOOM,
            engine=EffectEngine.FFMPEG,
            category=EffectCategory.CAMERA,
            start_seconds=clips[2].timeline_start + 2.0,
            duration_seconds=1.0,
            clip_id=clips[2].id,
        )
        relaid = retime.relay_timeline(
            self._timeline(clips), [self._freeze(clips[1]), zoom]
        )

        moved = next(item for item in relaid.effects if item.effect is EffectType.ZOOM)
        new_start = relaid.timeline.video_clips()[2].timeline_start
        assert moved.start_seconds - new_start == pytest.approx(2.0)

    def test_markers_move_with_the_footage_they_point_at(self) -> None:
        from backend.timeline.models import Marker

        clips = self._clips()
        timeline = self._timeline(clips).with_markers(
            [
                Marker(id="marker-00000early", timeline_seconds=5.0, label="hook"),
                Marker(id="marker-0000climax", timeline_seconds=20.0, label="climax"),
            ]
        )
        relaid = retime.relay_timeline(timeline, [self._freeze(clips[1])])

        by_label = {marker.label: marker for marker in relaid.timeline.markers}
        assert by_label["hook"].timeline_seconds == pytest.approx(5.0)
        assert by_label["climax"].timeline_seconds == pytest.approx(21.5)

    def test_the_six_band_cap_skips_a_warp_rather_than_crossing_it(self) -> None:
        clips = self._clips()
        relaid = retime.relay_timeline(
            self._timeline(clips), [self._freeze(clips[1])], max_duration_seconds=30.5
        )

        assert relaid.retimed_clips == 0
        assert relaid.timeline.duration == pytest.approx(30.0)
        assert any("§6" in note for note in relaid.notes)

    def test_a_second_warp_on_one_clip_is_refused(self) -> None:
        clips = self._clips()
        relaid = retime.relay_timeline(
            self._timeline(clips), [self._freeze(clips[1]), self._ramp(clips[1])]
        )

        assert relaid.retimed_clips == 1
        assert relaid.timeline.video_clips()[1].duration == pytest.approx(11.5)
        assert any("already carries" in note for note in relaid.notes)

    def test_the_clip_model_accepts_a_warped_span_and_rejects_a_drifted_one(self) -> None:
        warped = TimelineClip(
            id="clip-model-warp0",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=6.0,
            timeline_start=0.0,
            timeline_end=7.5,
            metadata={"retime": {"effect": "freeze_frame", "at": 2.5, "extra_seconds": 1.5}},
        )
        assert warped.duration == pytest.approx(7.5)

        with pytest.raises(Exception, match="cannot fill"):
            warped.model_copy(update={"timeline_end": 9.0}).model_validate(
                warped.model_copy(update={"timeline_end": 9.0}).model_dump()
            )
        with pytest.raises(Exception, match="cannot fill"):
            TimelineClip(
                id="clip-model-plain",
                media_id=MEDIA,
                clip_index=0,
                source_in=0.0,
                source_out=6.0,
                timeline_start=0.0,
                timeline_end=7.5,
            )

    def test_output_offset_maps_piecewise_around_the_warp(self) -> None:
        frozen = TimelineClip(
            id="clip-offset-frz0",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=6.0,
            timeline_start=0.0,
            timeline_end=7.5,
            metadata={"retime": {"effect": "freeze_frame", "at": 2.5, "extra_seconds": 1.5}},
        )
        assert retime.output_offset(frozen, 1.0) == pytest.approx(1.0)
        assert retime.output_offset(frozen, 4.0) == pytest.approx(5.5)

        ramped = TimelineClip(
            id="clip-offset-rmp0",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=6.0,
            timeline_start=0.0,
            timeline_end=7.0,
            metadata={
                "retime": {
                    "effect": "speed_ramp",
                    "at": 2.0,
                    "extra_seconds": 1.0,
                    "window_seconds": 1.0,
                    "factor": 0.5,
                }
            },
        )
        assert retime.output_offset(ramped, 1.0) == pytest.approx(1.0)
        assert retime.output_offset(ramped, 2.5) == pytest.approx(3.0), "inside: stretched"
        assert retime.output_offset(ramped, 4.0) == pytest.approx(5.0), "after: late by extra"

    def test_source_at_answers_through_the_warp(self) -> None:
        # "What was happening at 4:12" and a §127 split both go through this;
        # the linear form returned source positions past the recording's end
        # for the warp-extended tail, and a split there refused to load.
        frozen = TimelineClip(
            id="clip-srcat-frz0",
            media_id=MEDIA,
            clip_index=0,
            source_in=10.0,
            source_out=16.0,
            timeline_start=0.0,
            timeline_end=7.5,
            metadata={"retime": {"effect": "freeze_frame", "at": 2.5, "extra_seconds": 1.5}},
        )
        assert frozen.source_at(1.0) == pytest.approx(11.0)
        assert frozen.source_at(3.0) == pytest.approx(12.5), "during the hold: the held instant"
        assert frozen.source_at(7.0) == pytest.approx(15.5)
        assert frozen.source_at(7.5) <= 16.0

    def test_clip_retime_reconciles_the_shape_against_the_spans(self) -> None:
        # A §127 trim rewrites spans without understanding warps; the spans
        # win, so the renderer always emits a segment of clip.duration.
        drifted = TimelineClip(
            id="clip-recon-drft",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=6.0,
            timeline_start=0.0,
            timeline_end=7.0,
            metadata={"retime": {"effect": "freeze_frame", "at": 2.5, "extra_seconds": 1.5}},
        )
        warp = retime.clip_retime(drifted)
        assert warp is not None
        assert warp.extra_seconds == pytest.approx(1.0), "the spans outvote the metadata"

        junk_ramp = TimelineClip(
            id="clip-recon-junk",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=6.0,
            timeline_start=0.0,
            timeline_end=7.0,
            metadata={"retime": {"effect": "speed_ramp", "at": 2.0, "extra_seconds": 1.0}},
        )
        degraded = retime.clip_retime(junk_ramp)
        assert degraded is not None
        assert degraded.effect is EffectType.FREEZE_FRAME, (
            "a ramp whose shape cannot be honoured holds the frame rather than "
            "shipping a wrong-length segment"
        )

    def test_captions_after_a_freeze_land_after_the_hold(self, config) -> None:
        clips = self._clips()
        relaid = retime.relay_timeline(self._timeline(clips), [self._freeze(clips[0])])
        segment = TranscriptSegment(start=6.0, end=8.0, text="what a save", language="en")

        captions = caption_builder.build_captions(
            relaid.timeline, {MEDIA: [segment]}, config.captions
        )

        lead_in = config.captions.timing.lead_in_ms / 1000.0
        assert captions, "speech inside the used span must still caption"
        assert captions[0].timeline_start == pytest.approx(7.5 - lead_in, abs=0.01), (
            "spoken at source 6.0, seen at 7.5: the 1.5s hold sits before it"
        )


class TestTimeWarpSegments:
    """The renderer's half: one segment file of exactly the re-laid duration."""

    def _frozen_clip(self, at: float = 2.5, hold: float = 1.5) -> TimelineClip:
        return TimelineClip(
            id="clip-00000segfrz",
            media_id=MEDIA,
            clip_index=0,
            source_in=10.0,
            source_out=16.0,
            timeline_start=0.0,
            timeline_end=6.0 + hold,
            metadata={"retime": {"effect": "freeze_frame", "at": at, "extra_seconds": hold}},
        )

    def _target(self) -> EncodeTarget:
        return EncodeTarget(width=1920, height=1080, fps=60)

    def test_a_mid_clip_freeze_splits_pads_and_concatenates(self) -> None:
        clip = self._frozen_clip()
        graph = _warp_graph(clip, retime.clip_retime(clip), [], self._target())

        assert "split=2" in graph
        assert "tpad=stop_mode=clone:stop_duration=1.500000" in graph
        assert "concat=n=2:v=1:a=0" in graph
        assert graph.endswith("[vout]")

    def test_a_freeze_at_the_clip_edge_needs_no_split(self) -> None:
        clip = self._frozen_clip(at=6.0)
        graph = _warp_graph(clip, retime.clip_retime(clip), [], self._target())

        assert "split" not in graph
        assert "tpad=stop_mode=clone" in graph

    def test_the_edge_fades_land_after_the_warp_on_the_output_clock(self) -> None:
        # A fade-out timed on the source clock would end 1.5 s before the
        # extended clip does; it must sit after the concat, at final-duration
        # arithmetic.
        clip = self._frozen_clip().model_copy(
            update={"transition_out": TransitionType.FADE}
        )
        graph = _warp_graph(clip, retime.clip_retime(clip), [], self._target())

        assert graph.index("concat=") < graph.index("fade=t=out")
        assert "fade=t=out:st=7.100" in graph, "0.4s default fade against the 7.5s clip"

    def test_a_ramp_keeps_the_promised_extra_when_its_window_snaps(self) -> None:
        # The slow window runs into the clip's end; the window shrinks to fit
        # and the factor is re-derived so the *added seconds* -- the number
        # the timeline promised -- survive exactly.
        clip = TimelineClip(
            id="clip-00000segrmp",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=6.0,
            timeline_start=0.0,
            timeline_end=7.2,
            metadata={
                "retime": {
                    "effect": "speed_ramp",
                    "at": 5.3,
                    "extra_seconds": 1.2,
                    "window_seconds": 0.8,
                    "factor": 0.4,
                }
            },
        )
        warp = retime.clip_retime(clip)
        assert warp is not None
        assert warp.window_seconds == pytest.approx(0.7)
        graph = _warp_graph(clip, warp, [], self._target())

        assert "concat=n=2:v=1:a=0" in graph, "the snapped window leaves no tail piece"
        assert f"/{0.7 / 1.9:.6f}" in graph, "the factor bends; the duration does not"

    def test_the_segment_name_carries_the_warp(self) -> None:
        # §47's reuse compares durations, and two different anchors produce
        # same-length files with different frames in them.
        clip = self._frozen_clip(at=2.5)
        moved = self._frozen_clip(at=4.0)

        assert _retime_token(None) == ""
        assert _retime_token(retime.clip_retime(clip)) != ""
        assert _retime_token(retime.clip_retime(clip)) != _retime_token(
            retime.clip_retime(moved)
        )

    def test_neutral_effects_ride_the_source_clock_before_the_warp(self) -> None:
        # A zoom's stored window is a source-clock position; it must be baked
        # before the timebase bends, so the held frame freezes *zoomed*.
        clip = self._frozen_clip()
        zoom = EffectInstance(
            effect=EffectType.ZOOM,
            engine=EffectEngine.FFMPEG,
            category=EffectCategory.CAMERA,
            start_seconds=2.0,
            duration_seconds=1.0,
            clip_id=clip.id,
            params={"scale": 1.2},
        )
        graph = _warp_graph(clip, retime.clip_retime(clip), _realised(clip, [zoom]), self._target())

        assert graph.index("zoompan=") < graph.index("split=2")


class TestEffectPlacementRules:
    """The render worker's single filter over the stored plan.

    The rules are engine-independent, and enforcing them after the engine
    split meant each renderer held its own subset: the FFmpeg half dropped a
    disabled clip's zoom while the overlay drew the same clip's text_pop at a
    placeholder position over unrelated footage.
    """

    @staticmethod
    def _instance(
        effect=EffectType.TEXT_POP,
        engine=EffectEngine.REMOTION,
        *,
        clip_id: str | None = "clip-000000000one",
        start: float = 5.0,
        params: dict | None = None,
    ) -> EffectInstance:
        return EffectInstance(
            effect=effect,
            engine=engine,
            category=EffectCategory.TEXT,
            start_seconds=start,
            duration_seconds=1.2,
            clip_id=clip_id,
            params={"text": "VICTORY"} if params is None else params,
        )

    @staticmethod
    def _clips() -> dict[str, TimelineClip]:
        clip = TimelineClip(
            id="clip-000000000one",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=20.0,
            timeline_start=0.0,
            timeline_end=20.0,
        )
        return {clip.id: clip}

    def _placeable(self, effect: EffectInstance) -> bool:
        from backend.pipeline.workers.render_worker import RenderWorker

        return RenderWorker._still_placeable(effect, self._clips())

    def test_an_effect_on_a_missing_clip_is_dropped(self) -> None:
        # §78: the user disabled the clip. Its effects must not be guessed
        # onto the programme -- for EITHER engine.
        assert not self._placeable(self._instance(clip_id="clip-00000000gone"))

    def test_an_effect_past_a_trimmed_clip_is_dropped(self) -> None:
        # §127's save_edit keeps effect rows across trims on purpose; a
        # clip-relative 25s on a clip now 20s long points at footage the
        # clip no longer shows.
        assert not self._placeable(self._instance(start=25.0))

    def test_a_legacy_text_pop_without_text_is_dropped(self) -> None:
        # Rows planned before the content guards existed are replayed by
        # plain re-renders without re-planning; the reader holds the same
        # line the planner now does.
        assert not self._placeable(self._instance(params={}))

    def test_a_marker_without_its_region_is_dropped(self) -> None:
        assert not self._placeable(
            self._instance(
                effect=EffectType.HIGHLIGHT_BOX,
                params={"require_detected_region": True},
            )
        )

    def test_a_counter_without_a_tally_is_dropped(self) -> None:
        assert not self._placeable(
            self._instance(
                effect=EffectType.KILL_COUNTER,
                params={"require_event_count": True},
            )
        )
        assert self._placeable(
            self._instance(
                effect=EffectType.KILL_COUNTER,
                params={"require_event_count": True, "count": 3},
            )
        )

    def test_a_valid_effect_passes(self) -> None:
        assert self._placeable(self._instance())

@pytest.mark.unit
class TestNothingButGameplayReachesTheTimeline:
    """V2-P0.2, written from the render that carried a menu at 1:47.

    The moment's own core never touched that menu. Its clips reached 180
    seconds past its window and picked one up, so the guard has to be here as
    well as at formation -- this is the stage that decides what footage a clip
    occupies.
    """

    def _clip(self, start: float, end: float):
        from backend.timeline.builder import PlannedClip

        return PlannedClip(
            media_id=MEDIA,
            source_start=start,
            source_end=end,
            moment_id="m1",
            moment_type=None,
            score=0.5,
            role="body",
            beat="body",
            sources=(),
        )

    def test_a_clip_inside_a_menu_is_dropped(self) -> None:
        from backend.timeline.builder import _without_excluded

        kept, notes = _without_excluded([self._clip(100.0, 110.0)], [(95.0, 120.0)])

        assert kept == []
        assert notes and "not gameplay" in notes[0]

    def test_a_clip_that_runs_into_a_menu_is_trimmed_not_dropped(self) -> None:
        # A shot that ends early because a loading screen begins is still a
        # shot. Dropping it would lose the play that came before.
        from backend.timeline.builder import _without_excluded

        (kept,), _ = _without_excluded([self._clip(100.0, 120.0)], [(110.0, 130.0)])

        assert kept.source_start == pytest.approx(100.0)
        assert kept.source_end == pytest.approx(110.0)

    def test_a_menu_in_the_middle_keeps_the_longer_side_only(self) -> None:
        # Never both halves: splitting one planned clip into two would put a
        # cut nobody chose in the middle of a shot.
        from backend.timeline.builder import _without_excluded

        (kept,), _ = _without_excluded(
            [self._clip(100.0, 130.0)], [(105.0, 110.0)]
        )

        assert kept.source_start == pytest.approx(110.0)
        assert kept.source_end == pytest.approx(130.0)

    def test_a_sliver_left_over_is_not_a_shot(self) -> None:
        from backend.timeline.builder import MIN_SURVIVING_SECONDS, _without_excluded

        kept, _ = _without_excluded(
            [self._clip(100.0, 110.0)],
            [(100.0 + MIN_SURVIVING_SECONDS / 2, 120.0)],
        )

        assert kept == []

    def test_clean_footage_is_returned_unchanged(self) -> None:
        # By identity: a project with nothing to exclude must build exactly the
        # timeline it built before this existed.
        from backend.timeline.builder import _without_excluded

        clips = [self._clip(100.0, 110.0)]
        kept, notes = _without_excluded(clips, [])

        assert kept[0] is clips[0]
        assert notes == []

