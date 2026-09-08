"""P0.6 on the 88.5-minute benchmark: what the owner stopped watching for.

Reads the machine's own database -- the project the brief names as the
canonical real-footage test -- and skips by name where it is absent:

    python -m pytest tests/integration/test_pacing_benchmark.py -q -s

The four numbers these guard are the ones the P0.3 gate render failed on: a
median shot of 2.1 s with 130 clips under two seconds ("as if the connection
kept dropping"), a kill shown without its cause, cuts landing inside spoken
words, and skips that removed an event's first frames.
"""

from __future__ import annotations

import json
import statistics
from itertools import pairwise

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

BENCHMARK = "proj-5db1780821a6"
#: A gap this short between two clips plays as a cut inside one shot; longer
#: is the edit moving on, which is not a jump cut.
SKIP_SECONDS = 2.0
#: How far inside a word a boundary must fall to be heard. A cut landing on a
#: word's own edge is a clean boundary, not a cut through it.
INSIDE_WORD = 0.01


@pytest.fixture(scope="module")
def benchmark():
    import backend.database.repositories  # noqa: F401
    from backend.config.loader import load_config
    from backend.config.paths import build_paths, find_repository_root
    from backend.database.connection import Database

    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    if not paths.database_path.is_file():
        pytest.skip("no database on this machine")
    database = Database(paths.database_path, config.application.database)
    row = database.fetch_one("SELECT id FROM projects WHERE id = ?", (BENCHMARK,))
    if row is None:
        database.close()
        pytest.skip(f"the benchmark project {BENCHMARK} is not on this machine")
    try:
        yield database, config
    finally:
        database.close()


def _clips(database):
    return [
        (float(row["source_in"]), float(row["source_out"]), str(row["media_id"]))
        for row in database.fetch_all(
            "SELECT media_id, source_in, source_out FROM timeline_clips "
            "WHERE project_id = ? AND enabled = 1 AND track = 'video' ORDER BY timeline_start",
            (BENCHMARK,),
        )
    ]


def _events(database):
    return [
        (float(r["start_seconds"]), float(r["end_seconds"]), float(r["importance"]))
        for r in database.fetch_all(
            "SELECT e.start_seconds, e.end_seconds, e.importance FROM game_events e "
            "JOIN media m ON m.id = e.media_id WHERE m.project_id = ?",
            (BENCHMARK,),
        )
    ]


def _words(database):
    spans = []
    for row in database.fetch_all(
        "SELECT words FROM transcript_segments WHERE project_id = ?", (BENCHMARK,)
    ):
        for word in json.loads(row["words"] or "[]"):
            if word.get("start") is not None and word.get("end") is not None:
                spans.append((float(word["start"]), float(word["end"])))
    return spans


def test_p0_6_no_benchmark_clip_is_shorter_than_the_floor(benchmark) -> None:
    database, config = benchmark
    clips = _clips(database)
    assert clips, "the benchmark has no stored edit"
    floor = float(config.editorial.pacing.min_shot_seconds)
    short = [(round(a, 2), round(b - a, 2)) for a, b, _ in clips if b - a < floor - 1e-6]
    assert not short, f"{len(short)} clip(s) under the {floor:.1f} s floor: {short[:5]}"

    lengths = sorted(b - a for a, b, _ in clips)
    print(
        f"\nbenchmark {BENCHMARK}: {len(clips)} clips, {sum(lengths):.0f} s, "
        f"median {statistics.median(lengths):.2f} s, shortest {lengths[0]:.2f} s, "
        f"under 3 s {sum(x < 3.0 for x in lengths)}, under 2 s {sum(x < 2.0 for x in lengths)}"
    )


def test_p0_6_no_benchmark_cut_lands_inside_a_word(benchmark) -> None:
    database, _config = benchmark
    words = _words(database)
    if not words:
        pytest.skip("the benchmark has no word timings")
    inside = [
        round(at, 2)
        for a, b, _ in _clips(database)
        for at in (a, b)
        if any(start + INSIDE_WORD < at < end - INSIDE_WORD for start, end in words)
    ]
    assert not inside, f"{len(inside)} boundary(s) inside a spoken word: {inside[:5]}"


def test_p0_6_no_benchmark_skip_removes_an_event_onset(benchmark) -> None:
    database, _config = benchmark
    clips = _clips(database)
    events = _events(database)
    lost = []
    for (_a, end, media), (start, _b, next_media) in pairwise(clips):
        if media != next_media or not 0.0 < start - end <= SKIP_SECONDS:
            continue
        # ``start`` is the next clip's first frame, so an onset there is
        # shown, not removed -- the same convention jump_cuts.decide uses.
        lost.extend(round(onset, 2) for onset, _e, _i in events if end <= onset < start)
    assert not lost, f"{len(lost)} onset(s) removed by a skip: {lost[:5]}"


def test_p0_6_every_overlapping_moment_the_selection_dropped_kept_its_onsets(benchmark) -> None:
    """Item 5's number, measured after the merges rather than before.

    Joining shots only lengthens them, so it cannot lose an onset -- but a
    piece with no neighbour of its own moment is dropped, and that could. The
    owner asked for this checked on the finished clips, not on the plan.
    """
    database, _config = benchmark
    clips = _clips(database)
    events = _events(database)
    story = database.fetch_one(
        "SELECT result FROM analysis_jobs WHERE project_id = ? AND stage = 'story'", (BENCHMARK,)
    )
    if story is None or not story["result"]:
        pytest.skip("the benchmark has no story result")
    kept_ids = {clip["moment_id"] for clip in json.loads(story["result"])["clips"]}
    moments = [
        dict(row)
        for row in database.fetch_all(
            "SELECT id, start_seconds, end_seconds, context_start, context_end "
            "FROM moments WHERE project_id = ?",
            (BENCHMARK,),
        )
    ]
    kept = [m for m in moments if m["id"] in kept_ids]

    def overlaps(a, b, x, y):
        return min(b, y) - max(a, x) > 0

    def shown(at):
        return any(a - 0.01 <= at <= b + 0.01 for a, b, _ in clips)

    orphans = []
    for moment in moments:
        if moment["id"] in kept_ids:
            continue
        if not any(
            overlaps(moment["context_start"], moment["context_end"], k["context_start"], k["context_end"])
            for k in kept
        ):
            continue
        for onset, end, importance in events:
            if (
                importance >= 0.5
                and overlaps(onset, end, moment["start_seconds"], moment["end_seconds"])
                and not shown(onset)
            ):
                orphans.append((moment["id"], round(onset, 2)))
    assert not orphans, f"{len(orphans)} onset(s) of a dropped overlapping moment: {orphans[:5]}"
