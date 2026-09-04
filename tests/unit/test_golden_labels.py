"""The golden dataset's tooling (docs/BRIEF_P0.md, HUMAN-LABELED GOLDEN DATASET).

The labels are a person's. What is tested here is everything around them: the
schema is enforced line by line, an empty file says the sentence the brief
asks for and nothing else, the three metrics mean what the brief says, the
baseline is read from docs/BASELINE.md and never invented, and the gate trips
on the default thresholds and only on them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import score_moments as sm

pytestmark = pytest.mark.unit

HEADER = "start,end,label,note\n"


def _csv(tmp_path: Path, name: str, body: str = "") -> Path:
    path = tmp_path / f"{name}.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


class TestP0DatasetLabelsAreAPersonsAndValidated:
    def test_p0_dataset_an_empty_csv_says_no_labels_and_nothing_else(
        self, tmp_path: Path, capsys
    ) -> None:
        _csv(tmp_path, "proj-x")
        code = sm.main(["--labels-dir", str(tmp_path), "--baseline", str(tmp_path / "none.md")])
        out = capsys.readouterr().out
        assert code == 0
        assert sm.NO_LABELS_MESSAGE in out
        assert "precision" not in out, "no metric is estimated from nothing"

    def test_p0_dataset_no_csv_at_all_says_the_same(self, tmp_path: Path, capsys) -> None:
        code = sm.main(["--labels-dir", str(tmp_path)])
        assert code == 0
        assert capsys.readouterr().out.strip() == sm.NO_LABELS_MESSAGE

    def test_p0_dataset_only_the_eight_labels_are_accepted(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "proj-x", "10,20,great_bit,\n")
        with pytest.raises(sm.LabelError, match="great_bit"):
            sm.read_labels(path)

    def test_p0_dataset_a_span_under_two_seconds_is_refused(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "proj-x", "10,11.5,best_moment,\n")
        with pytest.raises(sm.LabelError, match="at least 2 s"):
            sm.read_labels(path)

    def test_p0_dataset_a_line_with_no_label_and_a_note_is_a_note_not_a_span(
        self, tmp_path: Path, capsys
    ) -> None:
        # The brief: "if uncertain, use note and do not force a label". Such a
        # line is counted apart and enters no metric.
        path = _csv(
            tmp_path,
            "proj-x",
            "10,20,best_moment,\n1524.0,1529.6,,red room walk - gameplay or transition?\n",
        )
        labels, notes = sm.read_sheet(path)
        assert [s.label for s in labels] == ["best_moment"]
        assert [s.note for s in notes] == ["red room walk - gameplay or transition?"]
        assert sm.read_labels(path) == labels
        predictions = tmp_path / "p.json"
        predictions.write_text('{"moments": [[10, 20]], "clips": [[1520, 1530]]}', encoding="utf-8")
        code = sm.main(
            [
                "--labels-dir", str(tmp_path),
                "--baseline", str(tmp_path / "none.md"),
                "--predictions-json", str(predictions),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "1 labelled spans, 1 note line(s) not scored" in out
        assert "leakage —" in out, "a note line is not a non_gameplay label"

    def test_p0_dataset_a_line_with_no_label_and_no_note_is_refused_by_line(
        self, tmp_path: Path
    ) -> None:
        path = _csv(tmp_path, "proj-x", "10,20,best_moment,\n30,40,,\n")
        with pytest.raises(sm.LabelError, match="line 3: a line with no label needs a note"):
            sm.read_labels(path)

    def test_p0_dataset_end_before_start_is_refused(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "proj-x", "20,10,best_moment,\n")
        with pytest.raises(sm.LabelError, match="after start"):
            sm.read_labels(path)

    def test_p0_dataset_the_header_is_exact(self, tmp_path: Path) -> None:
        path = tmp_path / "proj-x.csv"
        path.write_text("begin,finish,label,note\n", encoding="utf-8")
        with pytest.raises(sm.LabelError, match="header"):
            sm.read_labels(path)

    def test_p0_dataset_a_bad_file_stops_the_run_by_line(self, tmp_path: Path, capsys) -> None:
        _csv(tmp_path, "proj-x", "10,20,best_moment,fine\n30,31,reaction,too short\n")
        code = sm.main(["--labels-dir", str(tmp_path)])
        assert code == 2
        assert "line 3" in capsys.readouterr().out

    def test_p0_dataset_valid_rows_are_read_in_time_order(self, tmp_path: Path) -> None:
        path = _csv(tmp_path, "proj-x", "50,60,reaction,laugh\n10,20,best_moment,\n")
        spans = sm.read_labels(path)
        assert [(s.start, s.end, s.label) for s in spans] == [
            (10.0, 20.0, "best_moment"),
            (50.0, 60.0, "reaction"),
        ]
        assert spans[1].note == "laugh"


class TestP0DatasetMetricsMeanWhatTheBriefSays:
    def test_p0_dataset_best_moment_precision_recall_f1(self) -> None:
        labels = [
            sm.Span(100.0, 110.0, "best_moment"),
            sm.Span(200.0, 210.0, "best_moment"),
            sm.Span(300.0, 310.0, "best_moment"),
        ]
        # two moments hit two labels, one moment hits nothing, one label unfound
        predictions = sm.Predictions(moments=((102.0, 112.0), (200.0, 205.0), (500.0, 510.0)))
        metrics = sm.score(labels, predictions)
        assert metrics.precision == pytest.approx(2 / 3)
        assert metrics.recall == pytest.approx(2 / 3)
        assert metrics.f1 == pytest.approx(2 / 3)

    def test_p0_dataset_a_match_needs_half_of_the_shorter_span(self) -> None:
        labels = [sm.Span(100.0, 110.0, "best_moment")]
        grazing = sm.Predictions(moments=((108.0, 118.0),))  # 2 s of a 10 s span
        assert sm.score(labels, grazing).recall == 0.0
        half = sm.Predictions(moments=((105.0, 115.0),))  # 5 s of 10
        assert sm.score(labels, half).recall == 1.0

    def test_p0_dataset_boundary_error_is_distance_to_the_nearest_event_start(self) -> None:
        labels = [sm.Span(100.0, 104.0, "event_start"), sm.Span(200.0, 204.0, "event_start")]
        predictions = sm.Predictions(event_starts=(101.0, 97.0, 250.0))
        metrics = sm.score(labels, predictions)
        # 100 -> nearest 101 (1.0); 200 -> nearest 250 (50.0)
        assert metrics.boundary_error_mean == pytest.approx(25.5)
        assert metrics.boundary_error_median == pytest.approx(25.5)

    def test_p0_dataset_leakage_is_clip_seconds_inside_non_gameplay_labels(self) -> None:
        labels = [sm.Span(100.0, 110.0, "non_gameplay")]
        predictions = sm.Predictions(clips=((95.0, 105.0), (200.0, 210.0)))  # 5 of 20 s inside
        assert sm.score(labels, predictions).leakage == pytest.approx(0.25)

    def test_p0_dataset_an_unlabelled_metric_is_none_not_zero(self) -> None:
        labels = [sm.Span(100.0, 110.0, "reaction")]
        metrics = sm.score(labels, sm.Predictions(moments=((1.0, 5.0),)))
        assert metrics.f1 is None
        assert metrics.boundary_error_mean is None
        assert metrics.leakage is None


BASELINE_TEXT = """# Baseline

## Golden dataset baseline

| project | F1 | precision | recall | boundary error (s) | leakage |
|---|---:|---:|---:|---:|---:|
| `proj-x` | 0.600 | 0.700 | 0.525 | 1.20 | 0.0100 |

## Another section
"""


class TestP0DatasetBaselineIsReadNeverInvented:
    def test_p0_dataset_a_recorded_row_is_read(self) -> None:
        baseline = sm.read_baseline(BASELINE_TEXT, "proj-x")
        assert baseline is not None
        assert baseline.f1 == pytest.approx(0.6)
        assert baseline.boundary_error == pytest.approx(1.2)
        assert baseline.leakage == pytest.approx(0.01)

    def test_p0_dataset_no_row_means_not_yet_available(self, tmp_path: Path, capsys) -> None:
        _csv(tmp_path, "proj-y", "100,110,best_moment,\n")
        predictions = tmp_path / "p.json"
        predictions.write_text('{"moments": [[100, 110]]}', encoding="utf-8")
        baseline = tmp_path / "BASELINE.md"
        baseline.write_text(BASELINE_TEXT, encoding="utf-8")
        code = sm.main(
            [
                "--labels-dir", str(tmp_path), "--baseline", str(baseline),
                "--predictions-json", str(predictions), "--gate",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0, "no baseline is not a blocker"
        assert sm.NOT_YET_AVAILABLE in out

    def test_p0_dataset_a_missing_heading_means_no_baseline(self) -> None:
        assert sm.read_baseline("# nothing here\n", "proj-x") is None


class TestP0DatasetGateTripsOnTheDefaultThresholds:
    def _baseline(self) -> sm.Baseline:
        return sm.Baseline(f1=0.60, precision=0.7, recall=0.5, boundary_error=1.2, leakage=0.01)

    def _metrics(self, *, f1=0.60, boundary=1.2, leakage=0.01) -> sm.Metrics:
        return sm.Metrics(0.7, 0.5, f1, boundary, boundary, leakage, 10)

    def test_p0_dataset_within_thresholds_passes(self) -> None:
        assert sm.violations(self._metrics(f1=0.585, boundary=1.6, leakage=0.01), self._baseline()) == []

    def test_p0_dataset_f1_may_not_drop_more_than_0_02(self) -> None:
        found = sm.violations(self._metrics(f1=0.57), self._baseline())
        assert len(found) == 1 and "F1 fell" in found[0]

    def test_p0_dataset_leakage_may_never_rise(self) -> None:
        found = sm.violations(self._metrics(leakage=0.0101), self._baseline())
        assert len(found) == 1 and "leakage rose" in found[0]

    def test_p0_dataset_boundary_error_may_not_rise_more_than_half_a_second(self) -> None:
        found = sm.violations(self._metrics(boundary=1.71), self._baseline())
        assert len(found) == 1 and "boundary error rose" in found[0]

    def test_p0_dataset_gate_mode_exits_non_zero_and_normal_mode_does_not(
        self, tmp_path: Path, capsys
    ) -> None:
        _csv(tmp_path, "proj-x", "100,110,best_moment,\n200,210,best_moment,\n")
        predictions = tmp_path / "p.json"
        predictions.write_text('{"moments": [[100, 110]]}', encoding="utf-8")  # F1 0.667 < 0.6? no: recall .5
        baseline = tmp_path / "BASELINE.md"
        baseline.write_text(BASELINE_TEXT.replace("0.600", "0.900"), encoding="utf-8")
        common = [
            "--labels-dir", str(tmp_path), "--baseline", str(baseline),
            "--predictions-json", str(predictions),
        ]
        assert sm.main(common) == 0, "normal execution reports and exits 0 even on a regression"
        assert "vs baseline" in capsys.readouterr().out
        assert sm.main([*common, "--gate"]) == 1
        assert "GATE VIOLATION" in capsys.readouterr().out
