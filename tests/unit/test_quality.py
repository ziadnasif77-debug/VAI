"""Quality measurement (SPEC §117–§119).

A benchmark that flatters the thing it measures is worse than no benchmark: it
converts "we do not know" into "we checked". So most of this file is about the
ways these numbers could lie, and the code refusing to let them:

* a prediction outside the stretch anyone actually watched, counted as a false
  positive, would measure how long the annotator watched
* five clips over one highlight, counted as five hits, would reward spam
* an unedited project, counted as agreement, would turn "nobody used it" into
  a 100% acceptance rate

The arithmetic is tested too, but the arithmetic was never the risk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.errors import GamingEditorError
from backend.core.models.enums import GameEventType, MomentType
from backend.quality.dataset import (
    SCHEMA_VERSION,
    AnnotatedRecording,
    Confidence,
    GoldenDataset,
    Span,
    SpanKind,
    load_dataset,
)
from backend.quality.metrics import (
    Prediction,
    Score,
    boring_overlap,
    duration_error,
    evaluate,
    read_as_episodes,
    render_failure_rate,
    score_events,
    score_moments,
)

pytestmark = pytest.mark.unit


def recording(*spans: Span, watched: tuple[float, float] = (0.0, 600.0)) -> AnnotatedRecording:
    return AnnotatedRecording(
        source_path="D:/Gaming/session.mkv",
        size_bytes=1024,
        duration_seconds=3600.0,
        annotated_from_seconds=watched[0],
        annotated_to_seconds=watched[1],
        spans=spans,
    )


def event(start: float, end: float, **kwargs) -> Span:
    return Span(
        kind=SpanKind.EVENT,
        event_type=GameEventType.DEATH,
        start_seconds=start,
        end_seconds=end,
        **kwargs,
    )


def highlight(start: float, end: float, **kwargs) -> Span:
    return Span(
        kind=SpanKind.HIGHLIGHT,
        moment_type=MomentType.CLUTCH,
        start_seconds=start,
        end_seconds=end,
        **kwargs,
    )


class TestTheDatasetRefusesNonsense:
    def test_a_span_must_end_after_it_starts(self) -> None:
        with pytest.raises(ValueError):
            Span(kind=SpanKind.BORING, start_seconds=100.0, end_seconds=100.0)

    def test_an_event_span_must_say_which_event(self) -> None:
        with pytest.raises(ValueError):
            Span(kind=SpanKind.EVENT, start_seconds=1.0, end_seconds=2.0)

    def test_a_game_state_span_must_say_what_the_state_was(self) -> None:
        with pytest.raises(ValueError):
            Span(kind=SpanKind.GAME_STATE, start_seconds=1.0, end_seconds=2.0)

    def test_a_span_cannot_run_past_the_recording(self) -> None:
        with pytest.raises(ValueError):
            AnnotatedRecording(
                source_path="x.mkv",
                size_bytes=1,
                duration_seconds=60.0,
                spans=(event(10.0, 900.0),),
            )

    def test_a_future_schema_version_is_refused_rather_than_half_read(self) -> None:
        # Silently dropping labels reports a recall gain that is a labelling
        # loss, and it would be believed.
        with pytest.raises(ValueError):
            GoldenDataset(name="x", schema_version=SCHEMA_VERSION + 1)

    def test_an_unreadable_file_is_a_typed_error(self, tmp_path) -> None:
        broken = tmp_path / "broken.dataset.json"
        broken.write_text("{not json", encoding="utf-8")

        with pytest.raises(GamingEditorError):
            load_dataset(broken)

    def test_a_recording_is_found_by_filename_not_by_path(self) -> None:
        # The same recording legitimately moves between drives, and a benchmark
        # that breaks on a rename stops being run.
        dataset = GoldenDataset(name="x", recordings=(recording(event(10, 20)),))

        assert dataset.for_source("E:/elsewhere/session.mkv") is not None
        assert dataset.for_source("E:/elsewhere/other.mkv") is None


class TestEventScoring:
    def test_an_event_near_its_label_is_found(self) -> None:
        # An instant matches by proximity. Asking a kill at 812.4 to overlap a
        # label written 812-815 by half would fail a correct detection.
        score, _, _ = score_events([Prediction(101.0, 103.0)], recording(event(100.0, 110.0)))

        assert (score.true_positives, score.false_positives, score.false_negatives) == (1, 0, 0)

    def test_an_event_far_from_every_label_is_a_false_positive(self) -> None:
        score, _, _ = score_events([Prediction(400.0, 402.0)], recording(event(100.0, 110.0)))

        assert (score.true_positives, score.false_positives, score.false_negatives) == (0, 1, 1)

    def test_the_tolerance_is_a_boundary_not_a_cliff_edge(self) -> None:
        inside, _, _ = score_events(
            [Prediction(111.0, 113.0)], recording(event(100.0, 110.0)), tolerance_seconds=3.0
        )
        outside, _, _ = score_events(
            [Prediction(120.0, 122.0)], recording(event(100.0, 110.0)), tolerance_seconds=3.0
        )

        assert inside.true_positives == 1
        assert outside.true_positives == 0

    def test_a_long_prediction_covering_the_instant_is_a_hit(self) -> None:
        # The case the first real measurement got wrong: §27 merges detectors,
        # so a death arrives as a 26-second span whose midpoint sits well away
        # from the moment. It had found it, at 0.97 confidence.
        score, _, _ = score_events(
            [Prediction(2269.0, 2295.0)], recording(event(2290.0, 2299.0), watched=(0.0, 2400.0))
        )

        assert score.true_positives == 1

    def test_a_long_prediction_still_claims_only_one_label(self) -> None:
        subject = recording(
            event(100.0, 110.0), event(200.0, 210.0), watched=(0.0, 600.0)
        )

        score, _, _ = score_events([Prediction(50.0, 400.0)], subject)

        assert score.true_positives == 1
        assert score.false_negatives == 1

    def test_two_predictions_cannot_both_claim_one_label(self) -> None:
        score, _, _ = score_events(
            [Prediction(101.0, 103.0), Prediction(104.0, 106.0)],
            recording(event(100.0, 110.0)),
        )

        assert score.true_positives == 1
        assert score.false_positives == 1


class TestMomentScoring:
    def test_a_moment_covering_its_label_is_found(self) -> None:
        score, _, _ = score_moments(
            [Prediction(95.0, 135.0)], recording(highlight(100.0, 130.0))
        )

        assert score.true_positives == 1

    def test_a_moment_that_barely_clips_its_label_is_not(self) -> None:
        # Sharing one second with a highlight is not finding it.
        score, _, _ = score_moments(
            [Prediction(129.0, 200.0)], recording(highlight(100.0, 130.0))
        )

        assert score.true_positives == 0
        assert score.false_positives == 1

    def test_overlap_is_measured_against_the_shorter_span(self) -> None:
        # A 4-second label inside a 30-second clip is found; a 4-second clip
        # inside a 30-second label is not. Different failures.
        found, _, _ = score_moments(
            [Prediction(100.0, 130.0)], recording(highlight(110.0, 114.0))
        )
        missed, _, _ = score_moments(
            [Prediction(110.0, 114.0)], recording(highlight(100.0, 130.0))
        )

        assert found.true_positives == 1
        assert missed.true_positives == 1  # the shorter span is fully covered either way

    def test_five_clips_over_one_highlight_are_one_hit_and_four_misses(self) -> None:
        # Which is right: they are four clips a person would delete.
        predictions = [Prediction(100.0 + index, 130.0 + index) for index in range(5)]

        score, _, _ = score_moments(predictions, recording(highlight(100.0, 130.0)))

        assert score.true_positives == 1
        assert score.false_positives == 4


class TestTheWindow:
    """The easiest way to make a benchmark lie, and it lies flatteringly."""

    def test_a_prediction_outside_the_watched_window_is_discarded(self) -> None:
        # An annotator who watched ten minutes of an hour has not found the
        # other fifty minutes' events. Counting a prediction there as wrong
        # measures how long they watched.
        score, _, _ = score_events(
            [Prediction(2000.0, 2002.0)],
            recording(event(100.0, 110.0), watched=(0.0, 600.0)),
        )

        assert score.false_positives == 0
        assert score.out_of_window == 1

    def test_a_straddler_that_finds_nothing_is_discarded_too(self) -> None:
        # Its claim may live in the twenty seconds nobody watched.
        score, _, _ = score_events(
            [Prediction(590.0, 620.0)], recording(event(100.0, 110.0), watched=(0.0, 600.0))
        )

        assert score.out_of_window == 1
        assert score.false_positives == 0

    def test_a_straddler_covering_an_inside_label_is_a_find(self) -> None:
        # An episode that began before the window and ran into it covers
        # watched footage. Discarding it turned a found label into a miss the
        # day episode merging reached the boundary: the Grounded collision
        # running 19:53-20:48 against the buggy fail labelled at 20:41.
        score, _, _ = score_events(
            [Prediction(590.0, 620.0)],
            recording(event(600.0, 608.0), watched=(595.0, 1200.0)),
        )

        assert score.true_positives == 1
        assert score.out_of_window == 0

    def test_a_straddling_moment_still_answers_for_its_boring_seconds(self) -> None:
        subject = recording(
            Span(kind=SpanKind.BORING, start_seconds=100.0, end_seconds=200.0),
            watched=(50.0, 600.0),
        )

        count, seconds = boring_overlap([Prediction(40.0, 130.0)], subject)

        assert count == 1
        assert seconds == pytest.approx(30.0)


class TestGenericMarkers:
    """`unexpected_event` is the correlator saying it cannot name this.

    A person cannot label "unexpected", so an unmatched marker is unjudgeable
    -- the same reasoning as a window straddler that finds nothing. One that
    *does* find a label still counts: flagging the instant is finding it.
    """

    def test_an_unmatched_generic_claim_is_not_a_false_positive(self) -> None:
        score, _, _ = score_events(
            [Prediction(300.0, 302.0, "unexpected_event", 0.9)],
            recording(event(100.0, 110.0)),
        )

        assert score.false_positives == 0
        assert score.generic_markers == 1

    def test_a_generic_claim_that_finds_a_label_still_counts(self) -> None:
        score, _, _ = score_events(
            [Prediction(104.0, 106.0, "unexpected_event", 0.9)],
            recording(event(100.0, 110.0)),
        )

        assert score.true_positives == 1
        assert score.generic_markers == 0

    def test_named_claims_still_pay_for_being_wrong(self) -> None:
        score, _, _ = score_events(
            [Prediction(300.0, 302.0, "combat", 0.9)],
            recording(event(100.0, 110.0)),
        )

        assert score.false_positives == 1


class TestTypeTieBreak:
    """One prediction over two labels at the same distance means the one it names.

    Measured: a death prediction spanning both a rare-loot pickup and the
    death eight seconds later matched the pickup -- greedy order, equal
    distance -- and the report said the system missed a death it had found.
    """

    def test_the_label_sharing_the_name_wins_the_tie(self) -> None:
        labels = recording(
            Span(
                kind=SpanKind.EVENT,
                event_type=GameEventType.RARE_LOOT,
                start_seconds=628.0,
                end_seconds=633.0,
            ),
            event(636.0, 642.0),
            watched=(600.0, 1200.0),
        )

        score, matches, misses = score_events(
            [Prediction(618.0, 650.0, "death", 0.82)], labels
        )

        assert score.true_positives == 1
        found = next(match for match in matches if match.span is not None)
        assert found.span.event_type is GameEventType.DEATH
        assert [span.event_type for span in misses] == [GameEventType.RARE_LOOT]


class TestOpinions:
    def test_certain_only_excludes_the_arguable_labels(self) -> None:
        subject = recording(
            event(100.0, 110.0, confidence=Confidence.CERTAIN),
            event(200.0, 210.0, confidence=Confidence.OPINION),
        )

        everything, _, _ = score_events([Prediction(101.0, 103.0)], subject)
        certain, _, _ = score_events([Prediction(101.0, 103.0)], subject, certain_only=True)

        assert everything.actual == 2
        assert certain.actual == 1
        assert certain.excluded == 1

    def test_the_exclusion_count_is_reported(self) -> None:
        # A recall of 1.0 over two labels should be visibly that.
        subject = recording(event(100.0, 110.0, confidence=Confidence.OPINION))

        score, _, _ = score_events([], subject, certain_only=True)

        assert score.recall == 1.0
        assert score.excluded == 1


class TestBoring:
    def test_a_moment_over_a_boring_stretch_is_counted(self) -> None:
        subject = recording(
            Span(kind=SpanKind.BORING, start_seconds=100.0, end_seconds=200.0)
        )

        count, seconds = boring_overlap([Prediction(150.0, 250.0)], subject)

        assert count == 1
        assert seconds == pytest.approx(50.0)

    def test_a_moment_elsewhere_is_not(self) -> None:
        subject = recording(
            Span(kind=SpanKind.BORING, start_seconds=100.0, end_seconds=200.0)
        )

        assert boring_overlap([Prediction(300.0, 400.0)], subject) == (0, 0.0)


class TestTheRatios:
    def test_claiming_nothing_is_perfect_precision_and_no_recall(self) -> None:
        score = Score(true_positives=0, false_positives=0, false_negatives=3)

        assert score.precision == 1.0
        assert score.recall == 0.0

    def test_nothing_to_find_is_perfect_recall(self) -> None:
        score = Score(true_positives=0, false_positives=2, false_negatives=0)

        assert score.recall == 1.0
        assert score.precision == 0.0

    def test_the_counts_survive_into_the_report(self) -> None:
        # "0.5 precision" is one hit and one miss, or fifty and fifty, and only
        # one of those is worth acting on.
        data = Score(true_positives=1, false_positives=1, false_negatives=0).as_dict()

        assert data["true_positives"] == 1
        assert data["false_positives"] == 1

    def test_duration_error_reports_both_directions(self) -> None:
        over = duration_error(actual_seconds=1300.0, target_seconds=1200.0)
        under = duration_error(actual_seconds=1100.0, target_seconds=1200.0)

        assert over["error_seconds"] == 100.0
        assert under["error_seconds"] == -100.0

    def test_render_failure_rate_survives_no_attempts(self) -> None:
        assert render_failure_rate(0, 0)["rate"] == 0.0


class TestEvaluatingOneRecording:
    def test_it_scores_events_and_moments_together(self) -> None:
        subject = recording(
            event(100.0, 110.0),
            highlight(200.0, 260.0),
            Span(kind=SpanKind.BORING, start_seconds=400.0, end_seconds=500.0),
        )

        result = evaluate(
            subject,
            events=[Prediction(102.0, 104.0)],
            moments=[Prediction(195.0, 265.0), Prediction(410.0, 470.0)],
        )

        assert result.events.true_positives == 1
        assert result.moments.true_positives == 1
        assert result.boring_selected == 1

    def test_the_misses_are_carried_out_for_inspection(self) -> None:
        # §118 asks for numbers; the cases are the work.
        subject = recording(event(100.0, 110.0), event(300.0, 310.0))

        result = evaluate(subject, events=[Prediction(101.0, 103.0)])

        assert len(result.misses) == 1
        assert result.misses[0].start_seconds == 300.0


class TestReadAsEpisodes:
    """Scoring speaks the product's granularity, through the product's reader."""

    def test_a_same_type_run_becomes_one_prediction(self) -> None:
        merged = read_as_episodes(
            [
                Prediction(10.0, 11.0, "combat", 0.8),
                Prediction(18.0, 19.0, "combat", 0.9),
                Prediction(25.0, 26.0, "combat", 0.7),
            ]
        )

        assert len(merged) == 1
        assert (merged[0].start_seconds, merged[0].end_seconds) == (10.0, 26.0)
        # An episode is as certain as its clearest sighting, not the average.
        assert merged[0].confidence == 0.9

    def test_a_gap_beyond_the_knee_splits_the_run(self) -> None:
        merged = read_as_episodes(
            [Prediction(10.0, 11.0, "combat"), Prediction(40.0, 41.0, "combat")]
        )

        assert len(merged) == 2

    def test_different_types_never_merge(self) -> None:
        merged = read_as_episodes(
            [Prediction(10.0, 11.0, "combat"), Prediction(12.0, 13.0, "collision")]
        )

        assert {item.label for item in merged} == {"combat", "collision"}

    def test_generic_claims_pass_through_and_do_not_break_a_run(self) -> None:
        # `unexpected_event` is the correlator saying it could not name this.
        # It stays on the table as its own claim -- hiding it would flatter
        # precision -- and the named run continues across it, exactly as the
        # product's own reading does.
        merged = read_as_episodes(
            [
                Prediction(10.0, 11.0, "combat"),
                Prediction(14.0, 15.0, "unexpected_event"),
                Prediction(18.0, 19.0, "combat"),
            ]
        )

        assert [item.label for item in merged] == ["combat", "unexpected_event"]
        assert (merged[0].start_seconds, merged[0].end_seconds) == (10.0, 19.0)

    def test_a_label_the_enum_does_not_know_passes_through(self) -> None:
        merged = read_as_episodes([Prediction(10.0, 11.0, "not_a_type", 0.5)])

        assert merged == [Prediction(10.0, 11.0, "not_a_type", 0.5)]

    def test_empty_in_empty_out(self) -> None:
        assert read_as_episodes([]) == []


_DATASETS = sorted(
    (Path(__file__).resolve().parents[2] / "datasets").glob("*.dataset.json")
)


@pytest.mark.parametrize("path", _DATASETS, ids=lambda p: p.stem)
class TestTheShippedDatasets:
    """Every golden file is data, and data can rot.

    Parametrised over the directory, not named files: a window added to the
    set is covered the day it lands, and one that breaks the schema fails
    here rather than at evaluation time.
    """

    def test_it_loads(self, path: Path) -> None:
        dataset = load_dataset(path)

        assert dataset.recordings
        assert dataset.total_spans > 0

    def test_it_labels_more_than_one_kind_of_thing(self, path: Path) -> None:
        # §117 lists events, boring segments, best moments, reactions and game
        # state. A dataset of only events would benchmark one detector.
        dataset = load_dataset(path)
        kinds = {span.kind for recording in dataset.recordings for span in recording.spans}

        assert len(kinds) >= 3

    def test_every_span_says_why_it_is_there(self, path: Path) -> None:
        # The reason a label exists is the first thing anyone re-checking it
        # wants, and it is unrecoverable later.
        dataset = load_dataset(path)

        for item in dataset.recordings:
            for span in item.spans:
                assert span.note.strip(), f"{span.kind.value} at {span.start_seconds} has no note"

    def test_the_file_is_formatted_json(self, path: Path) -> None:
        json.loads(path.read_text(encoding="utf-8"))

    def test_every_span_sits_inside_its_window(self, path: Path) -> None:
        # A label outside the watched stretch would be discarded silently by
        # the evaluator’s own window rule — better to refuse it here.
        dataset = load_dataset(path)

        for item in dataset.recordings:
            low, high = item.window
            for span in item.spans:
                assert low <= span.start_seconds and span.end_seconds <= high, (
                    f"{span.kind.value} {span.start_seconds}-{span.end_seconds} "
                    f"outside {low}-{high}"
                )


def test_the_dataset_directory_is_not_empty() -> None:
    assert _DATASETS, "the golden set is gone"
