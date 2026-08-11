"""Score a project against the golden dataset (SPEC §118).

    python scripts/evaluate.py --project proj-abc123 --dataset datasets/x.dataset.json

Reads what the pipeline stored -- game events and moments -- and scores it
against labels written by a person who was not looking at any of it. Prints
precision, recall, the counts behind them, and the cases: what was missed and
what was claimed that nobody labelled. The numbers are the summary; the cases
are the work.

``--offset`` exists because analysing a ten-minute cut of an hour-long
recording is minutes rather than an afternoon, and the cut's timestamps start
at zero while the labels are written against the original. Pass the seconds the
cut began at and the two line up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.loader import load_config
from backend.config.paths import build_paths
from backend.database.connection import Database
from backend.quality.dataset import AnnotatedRecording, load_dataset
from backend.quality.metrics import Prediction, evaluate
from backend.quality.user_edits import measure_project


def timecode(seconds: float) -> str:
    minutes, secs = divmod(int(max(0.0, seconds)), 60)
    return f"{minutes:>3d}:{secs:02d}"


def predictions_from(
    database: Database, project_id: str, table: str, offset: float
) -> list[Prediction]:
    label_column = "event_type" if table == "game_events" else "moment_type"
    rows = database.fetch_all(
        f"SELECT start_seconds, end_seconds, {label_column} AS label, confidence "
        f"FROM {table} WHERE project_id = ? ORDER BY start_seconds",
        (project_id,),
    )
    return [
        Prediction(
            start_seconds=float(row["start_seconds"]) + offset,
            end_seconds=float(row["end_seconds"]) + offset,
            label=str(row["label"]),
            confidence=float(row["confidence"] or 0.0),
        )
        for row in rows
    ]


def report(recording: AnnotatedRecording, evaluation, *, certain_only: bool) -> None:
    scope = "certain labels only" if certain_only else "every label"
    print(f"\n--- {Path(recording.source_path).name}  ({scope}) ---")
    print(f"    watched {timecode(recording.window[0])} to {timecode(recording.window[1])}")

    for name, score in (("events", evaluation.events), ("moments", evaluation.moments)):
        data = score.as_dict()
        print(
            f"\n  {name:8s} precision {data['precision']:.2f}  recall {data['recall']:.2f}"
            f"  f1 {data['f1']:.2f}"
        )
        print(
            f"           {score.true_positives} found, {score.false_positives} claimed and "
            f"not labelled, {score.false_negatives} labelled and missed"
        )
        if score.out_of_window:
            print(f"           {score.out_of_window} discarded: outside the watched window")
        if score.excluded:
            print(f"           {score.excluded} labels excluded as opinion")

    print(
        f"\n  boring   {evaluation.boring_selected} selected moment(s) overlap a stretch "
        f"marked boring, {evaluation.boring_seconds_selected:.0f}s in total"
    )

    if evaluation.misses:
        print("\n  missed:")
        for span in evaluation.misses:
            kind = span.event_type.value if span.event_type else (
                span.moment_type.value if span.moment_type else span.kind.value
            )
            print(
                f"    {timecode(span.start_seconds)}-{timecode(span.end_seconds)}  "
                f"{kind:16s} {span.note[:60]}"
            )

    unmatched = [match for match in evaluation.matches if match.span is None]
    if unmatched:
        print(f"\n  claimed, not labelled ({len(unmatched)}):")
        for match in unmatched[:12]:
            print(
                f"    {timecode(match.prediction.start_seconds)}-"
                f"{timecode(match.prediction.end_seconds)}  {match.prediction.label:16s} "
                f"conf {match.prediction.confidence:.2f}"
            )
        if len(unmatched) > 12:
            print(f"    ... and {len(unmatched) - 12} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument(
        "--offset",
        type=float,
        default=0.0,
        help="seconds to add to stored timestamps, when the media is a cut",
    )
    parser.add_argument("--source", default=None, help="which recording in the dataset")
    args = parser.parse_args()

    config = load_config()
    paths = build_paths(config)
    dataset = load_dataset(Path(args.dataset))
    print(f"dataset: {dataset.summary()}")

    if args.source:
        recording = dataset.for_source(args.source)
    else:
        recording = dataset.recordings[0] if dataset.recordings else None
    if recording is None:
        print("FAILED: the dataset has no recording to score against.")
        return 1

    database = Database(paths.database_path, config.application.database)
    try:
        events = predictions_from(database, args.project, "game_events", args.offset)
        moments = predictions_from(database, args.project, "moments", args.offset)
        print(
            f"stored:  {len(events)} game events, {len(moments)} moments "
            f"(offset {args.offset:g}s)"
        )
        if not events and not moments:
            print("FAILED: this project has nothing stored. Has it been analysed?")
            return 1

        for certain_only in (False, True):
            report(
                recording,
                evaluate(recording, events=events, moments=moments, certain_only=certain_only),
                certain_only=certain_only,
            )

        edits = measure_project(database, args.project)
        print("\n--- user edits (§119) ---")
        if not edits.edited:
            print("    nobody has edited this project, so §119 has nothing to measure")
        else:
            print(f"    acceptance {edits.acceptance_rate:.2f} over {edits.clips} clips")
            print(f"    {edits.selected_deleted} deleted, {edits.rejected_restored} restored")
            print(f"    {edits.manual_edits} manual edits ({edits.edits_per_clip:.2f} per clip)")
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
