"""Score the pipeline against the human-labelled golden dataset (docs/BRIEF_P0.md).

    python scripts/score_moments.py                  # every CSV under tests/golden/labels
    python scripts/score_moments.py --project ID     # one project
    python scripts/score_moments.py --gate           # exit non-zero on a threshold violation

The labels are written by a person, by hand, into ``tests/golden/labels/<project>.csv``
with the schema ``start,end,label,note``. This script never writes a label,
never fills one in from pipeline output, and never estimates a baseline: a CSV
with no labelled span produces the sentence the brief asks for and nothing
else.

**What is measured**, per labelled project, from what the pipeline stored:

* ``best_moment`` -- precision, recall and F1 between the stored moment cores
  and the spans a person called best. A pair counts when the overlap is at
  least half of the shorter of the two spans.
* ``event_start`` -- boundary error: for each labelled onset, the distance in
  seconds from the label's start to the nearest stored game event's start.
  Reported as the mean; the median travels with it.
* ``non_gameplay`` leakage -- the share of the final timeline's source seconds
  that fall inside spans a person labelled non-gameplay.

**The baseline** is read from ``docs/BASELINE.md``, from the table under the
heading ``## Golden dataset baseline``. Until a person has labelled and the
first measurement is recorded there, the comparison is NOT YET AVAILABLE and
nothing here blocks anything. Once it exists, ``--gate`` applies the brief's
default thresholds: F1 may not fall by more than 0.02, leakage may never rise,
boundary error may not rise by more than 0.50 s. PLAN.md may only tighten them.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

LABELS_DIR: Final[Path] = REPOSITORY / "tests" / "golden" / "labels"
BASELINE_FILE: Final[Path] = REPOSITORY / "docs" / "BASELINE.md"
BASELINE_HEADING: Final[str] = "## Golden dataset baseline"

#: The closed label vocabulary, verbatim from the brief.
LABELS: Final[tuple[str, ...]] = (
    "best_moment",
    "unimportant",
    "event_start",
    "payoff",
    "reaction",
    "dead_time",
    "failed_attempt",
    "non_gameplay",
)
MIN_SPAN_SECONDS: Final[float] = 2.0
NO_LABELS_MESSAGE: Final[str] = "No labeled spans available; baseline cannot be computed"
NOT_YET_AVAILABLE: Final[str] = "NOT YET AVAILABLE"

#: Default gate thresholds (docs/BRIEF_P0.md, DATASET GATE).
MAX_F1_DROP: Final[float] = 0.02
MAX_LEAKAGE_INCREASE: Final[float] = 0.0
MAX_BOUNDARY_ERROR_INCREASE: Final[float] = 0.50

#: A pair of spans counts as the same thing when they share at least this
#: fraction of the shorter one.
MATCH_FRACTION: Final[float] = 0.5


class LabelError(ValueError):
    """A CSV that does not follow the schema. Named per line, never guessed."""


@dataclass(frozen=True, slots=True)
class Span:
    start: float
    end: float
    label: str
    note: str = ""

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Predictions:
    """What the pipeline stored for one project, as plain spans."""

    moments: tuple[tuple[float, float], ...] = ()
    event_starts: tuple[float, ...] = ()
    clips: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class Metrics:
    precision: float | None
    recall: float | None
    f1: float | None
    boundary_error_mean: float | None
    boundary_error_median: float | None
    leakage: float | None
    labelled: int


@dataclass(frozen=True, slots=True)
class Baseline:
    f1: float | None
    precision: float | None
    recall: float | None
    boundary_error: float | None
    leakage: float | None


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def read_labels(path: Path) -> list[Span]:
    """The labelled spans in one CSV, validated. An empty file (header only) is fine."""
    return read_sheet(path)[0]


def read_sheet(path: Path) -> tuple[list[Span], list[Span]]:
    """The labelled spans and the note lines of one CSV, validated.

    A line whose label is empty is a note, not a label (the brief: "if
    uncertain, use ``note`` and do not force a label"). It is kept apart,
    counted in the report, and enters no metric. A line with no label *and*
    no note says nothing and is refused by line, like every other bad line.
    """
    problems: list[str] = []
    spans: list[Span] = []
    notes: list[Span] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected = ["start", "end", "label", "note"]
        if reader.fieldnames is None:
            return []
        if [name.strip() for name in reader.fieldnames] != expected:
            raise LabelError(
                f"{path.name}: header must be exactly 'start,end,label,note', "
                f"got {','.join(reader.fieldnames)!r}"
            )
        for number, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                start = float(row["start"])
                end = float(row["end"])
            except (TypeError, ValueError):
                problems.append(f"line {number}: start and end must be seconds as numbers")
                continue
            label = (row["label"] or "").strip()
            note = (row.get("note") or "").strip()
            if not label and not note:
                problems.append(f"line {number}: a line with no label needs a note")
                continue
            if label and label not in LABELS:
                problems.append(
                    f"line {number}: label {label!r} is not one of {', '.join(LABELS)}"
                )
                continue
            if end <= start:
                problems.append(f"line {number}: end ({end}) must be after start ({start})")
                continue
            if end - start < MIN_SPAN_SECONDS:
                problems.append(
                    f"line {number}: a span must be at least {MIN_SPAN_SECONDS:g} s "
                    f"({end - start:.2f} s given)"
                )
                continue
            (spans if label else notes).append(Span(start, end, label, note))
    if problems:
        raise LabelError(f"{path.name}: " + "; ".join(problems))
    return (
        sorted(spans, key=lambda span: span.start),
        sorted(notes, key=lambda span: span.start),
    )


# ---------------------------------------------------------------------------
# metrics -- pure, so the tests own the definitions
# ---------------------------------------------------------------------------


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _matches(a: tuple[float, float], b: tuple[float, float]) -> bool:
    shorter = min(a[1] - a[0], b[1] - b[0])
    return shorter > 0 and _overlap(a, b) >= MATCH_FRACTION * shorter


def score(labels: list[Span], predictions: Predictions) -> Metrics:
    best = [(s.start, s.end) for s in labels if s.label == "best_moment"]
    onsets = [s.start for s in labels if s.label == "event_start"]
    dead = [(s.start, s.end) for s in labels if s.label == "non_gameplay"]

    precision = recall = f1 = None
    if best:
        hits_pred = sum(1 for m in predictions.moments if any(_matches(m, b) for b in best))
        hits_label = sum(1 for b in best if any(_matches(m, b) for m in predictions.moments))
        precision = hits_pred / len(predictions.moments) if predictions.moments else 0.0
        recall = hits_label / len(best)
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )

    mean = median = None
    if onsets and predictions.event_starts:
        errors = [
            min(abs(onset - start) for start in predictions.event_starts) for onset in onsets
        ]
        mean = statistics.fmean(errors)
        median = statistics.median(errors)

    leakage = None
    if dead:
        total = sum(b - a for a, b in predictions.clips)
        inside = sum(_overlap(clip, span) for clip in predictions.clips for span in dead)
        leakage = inside / total if total > 0 else 0.0

    return Metrics(precision, recall, f1, mean, median, leakage, len(labels))


# ---------------------------------------------------------------------------
# baseline -- read, never written, from docs/BASELINE.md
# ---------------------------------------------------------------------------

_ROW = re.compile(r"^\|\s*`?(?P<project>[^|`]+?)`?\s*\|(?P<rest>.*)\|\s*$")


def read_baseline(text: str, project: str) -> Baseline | None:
    """The recorded numbers for ``project``, or ``None`` when none were recorded.

    The table under ``## Golden dataset baseline`` has the columns
    ``project | F1 | precision | recall | boundary error (s) | leakage``; a
    cell that is not a number (``—``, ``n/a``) is an unmeasured metric.
    """
    if BASELINE_HEADING not in text:
        return None
    section = text.split(BASELINE_HEADING, 1)[1]
    for line in section.splitlines():
        if line.startswith("## "):
            break
        match = _ROW.match(line.strip())
        if not match or match.group("project").strip() != project:
            continue
        cells = [cell.strip() for cell in match.group("rest").split("|")]
        if len(cells) < 5:
            continue

        def number(cell: str) -> float | None:
            try:
                return float(cell)
            except ValueError:
                return None

        return Baseline(
            f1=number(cells[0]),
            precision=number(cells[1]),
            recall=number(cells[2]),
            boundary_error=number(cells[3]),
            leakage=number(cells[4]),
        )
    return None


def violations(metrics: Metrics, baseline: Baseline) -> list[str]:
    """The gate: which default thresholds the current numbers break."""
    found: list[str] = []
    f1_fell = (
        baseline.f1 is not None
        and metrics.f1 is not None
        and baseline.f1 - metrics.f1 > MAX_F1_DROP + 1e-9
    )
    if f1_fell:
        found.append(
            f"best_moment F1 fell {baseline.f1 - metrics.f1:.3f} "
            f"(allowed {MAX_F1_DROP:.2f}): {baseline.f1:.3f} -> {metrics.f1:.3f}"
        )
    leakage_rose = (
        baseline.leakage is not None
        and metrics.leakage is not None
        and metrics.leakage - baseline.leakage > MAX_LEAKAGE_INCREASE + 1e-9
    )
    if leakage_rose:
        found.append(
            f"non_gameplay leakage rose: {baseline.leakage:.4f} -> {metrics.leakage:.4f}"
        )
    now = metrics.boundary_error_mean
    then = baseline.boundary_error
    if now is not None and then is not None and now - then > MAX_BOUNDARY_ERROR_INCREASE + 1e-9:
        found.append(
            f"event_start boundary error rose {now - then:.2f} s "
            f"(allowed {MAX_BOUNDARY_ERROR_INCREASE:.2f}): {then:.2f} -> {now:.2f}"
        )
    return found


# ---------------------------------------------------------------------------
# the pipeline's side -- read only
# ---------------------------------------------------------------------------


def load_predictions(project_id: str) -> Predictions:
    """Moments, game-event onsets and timeline clips the pipeline stored."""
    import backend.database.repositories  # noqa: F401  (registers repositories)
    from backend.config.loader import load_config
    from backend.config.paths import build_paths, find_repository_root
    from backend.database.connection import Database

    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    database = Database(paths.database_path, config.application.database)
    try:
        moments = tuple(
            (float(r["start_seconds"]), float(r["end_seconds"]))
            for r in database.fetch_all(
                "SELECT start_seconds, end_seconds FROM moments WHERE project_id = ?",
                (project_id,),
            )
        )
        starts = tuple(
            float(r["start_seconds"])
            for r in database.fetch_all(
                "SELECT e.start_seconds FROM game_events e JOIN media m ON m.id = e.media_id "
                "WHERE m.project_id = ?",
                (project_id,),
            )
        )
        clips = tuple(
            (float(r["source_in"]), float(r["source_out"]))
            for r in database.fetch_all(
                "SELECT source_in, source_out FROM timeline_clips "
                "WHERE project_id = ? AND enabled = 1 AND track = 'video'",
                (project_id,),
            )
        )
    finally:
        database.close()
    return Predictions(moments=moments, event_starts=starts, clips=clips)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _fmt(value: float | None, places: int = 3, suffix: str = "") -> str:
    return "—" if value is None else f"{value:.{places}f}{suffix}"


def report(
    project: str, metrics: Metrics, baseline: Baseline | None, *, notes: int = 0
) -> list[str]:
    lines = [
        f"{project}: {metrics.labelled} labelled spans, {notes} note line(s) not scored",
        f"  best_moment   precision {_fmt(metrics.precision)}  recall {_fmt(metrics.recall)}"
        f"  F1 {_fmt(metrics.f1)}",
        f"  event_start   boundary error mean {_fmt(metrics.boundary_error_mean, 2, ' s')}"
        f"  median {_fmt(metrics.boundary_error_median, 2, ' s')}",
        f"  non_gameplay  leakage {_fmt(metrics.leakage, 4)}",
    ]
    if baseline is None:
        lines.append(f"  baseline: {NOT_YET_AVAILABLE} (no row for {project} in docs/BASELINE.md)")
        return lines

    def delta(now: float | None, then: float | None, places: int = 3) -> str:
        if now is None or then is None:
            return "—"
        return f"{now - then:+.{places}f}"

    lines.append(
        f"  vs baseline   F1 {delta(metrics.f1, baseline.f1)}  "
        f"boundary error {delta(metrics.boundary_error_mean, baseline.boundary_error, 2)} s  "
        f"leakage {delta(metrics.leakage, baseline.leakage, 4)}"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", help="score one project; default: every CSV")
    parser.add_argument(
        "--gate", action="store_true", help="exit non-zero on a threshold violation"
    )
    parser.add_argument("--labels-dir", type=Path, default=LABELS_DIR)
    parser.add_argument("--baseline", type=Path, default=BASELINE_FILE)
    parser.add_argument(
        "--predictions-json",
        type=Path,
        help="testing aid: read predictions from a JSON file instead of the database",
    )
    arguments = parser.parse_args(argv)

    files = sorted(arguments.labels_dir.glob("*.csv"))
    if arguments.project:
        files = [f for f in files if f.stem == arguments.project]
        if not files:
            print(f"no labels file for {arguments.project} under {arguments.labels_dir}")
            return 2
    if not files:
        print(NO_LABELS_MESSAGE)
        return 0

    baseline_text = ""
    if arguments.baseline.is_file():
        baseline_text = arguments.baseline.read_text(encoding="utf-8")
    failures: list[str] = []
    scored = 0
    said = 0
    for path in files:
        project = path.stem
        try:
            labels, notes = read_sheet(path)
        except LabelError as error:
            print(f"{project}: {error}")
            return 2
        if not labels:
            print(f"{project}: {NO_LABELS_MESSAGE}")
            if notes:
                print(f"{project}: {len(notes)} note line(s), not scored")
            said += 1
            continue
        if arguments.predictions_json is not None:
            import json

            raw = json.loads(arguments.predictions_json.read_text(encoding="utf-8"))
            predictions = Predictions(
                moments=tuple(tuple(m) for m in raw.get("moments", [])),
                event_starts=tuple(raw.get("event_starts", [])),
                clips=tuple(tuple(c) for c in raw.get("clips", [])),
            )
        else:
            predictions = load_predictions(project)
        metrics = score(labels, predictions)
        baseline = read_baseline(baseline_text, project)
        print("\n".join(report(project, metrics, baseline, notes=len(notes))))
        scored += 1
        if arguments.gate:
            if baseline is None:
                print(f"  gate: {NOT_YET_AVAILABLE} -- not a blocker until a baseline is recorded")
            else:
                broken = violations(metrics, baseline)
                for item in broken:
                    print(f"  GATE VIOLATION: {item}")
                failures.extend(f"{project}: {item}" for item in broken)
    if scored == 0:
        if not said:
            print(NO_LABELS_MESSAGE)
        return 0
    if arguments.gate and failures:
        print(f"\nDATASET GATE: FAILED ({len(failures)} violation(s))")
        return 1
    if arguments.gate:
        print("\nDATASET GATE: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
