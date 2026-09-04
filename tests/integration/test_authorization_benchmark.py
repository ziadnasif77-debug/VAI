"""P0.3 test 7: the 88.5-minute benchmark, every clip inside its authorization.

Reads the machine's own database -- the project the brief names as the
canonical real-footage test -- and skips by name where it is absent. Run
explicitly for acceptance:

    python -m pytest tests/integration/test_authorization_benchmark.py -q

What it proves is the whole chain on real footage: every stored clip of the
benchmark's latest EDL carries its grants -- moment_core where the core has
length, context_expansion always -- lies inside its governing span, and the stored moments
each carry a chain. It also reports, for the closure note, how many seconds
of the edit each granter is responsible for beyond the context grant.
"""

from __future__ import annotations

import pytest

from backend.timeline import authorization, validation

pytestmark = [pytest.mark.integration, pytest.mark.slow]

BENCHMARK = "proj-5db1780821a6"


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
        yield database
    finally:
        database.close()


def test_p0_3_every_benchmark_clip_lies_inside_its_authorization(benchmark) -> None:
    from backend.database.repositories.timeline import TimelineRepository

    timeline = TimelineRepository(benchmark).load(BENCHMARK)
    clips = timeline.video_clips()
    assert clips, "the benchmark has no stored edit"

    granters: dict[str, float] = {}
    for clip in clips:
        chain = authorization.spans_from_metadata(clip.metadata)
        assert chain, (
            f"clip {clip.clip_index} carries no authorized span: the benchmark's EDL "
            "predates P0.3 -- re-run MOMENTS and STORY"
        )
        names = [span.granted_by for span in chain]
        assert authorization.Granter.CONTEXT_EXPANSION in names
        assert names[0] in {
            authorization.Granter.MOMENT_CORE,
            authorization.Granter.CONTEXT_EXPANSION,
        }
        assert not authorization.check_clip(
            clip.media_id, clip.source_in, clip.source_out, chain, label=f"clip {clip.clip_index}"
        )
        # Seconds this clip shows that no context grant in its chain covers,
        # credited to the granter whose span the clip needed for them. A
        # merged clip carries both moments' context grants, so only the seam
        # between them is anyone else's.
        contexts = [
            (span.start, span.end)
            for span in chain
            if span.granted_by is authorization.Granter.CONTEXT_EXPANSION
        ]
        beyond = _seconds_outside((clip.source_in, clip.source_out), contexts)
        if beyond > 1e-6:
            governing = authorization.newest_for(chain, clip.media_id)
            assert governing is not None
            granters[governing.granted_by.value] = granters.get(governing.granted_by.value, 0.0) + beyond

    report = validation.validate(timeline, require_authorization=True)
    assert report.is_valid, [str(item) for item in report.errors]
    print(
        f"\nbenchmark {BENCHMARK}: {len(clips)} clips, seconds beyond the context grant by granter: "
        + (", ".join(f"{name} {seconds:.1f} s" for name, seconds in sorted(granters.items())) or "none")
    )


def _seconds_outside(span: tuple[float, float], covered: list[tuple[float, float]]) -> float:
    """Seconds of ``span`` inside none of ``covered``."""
    pieces = [span]
    for lo, hi in sorted(covered):
        next_pieces = []
        for a, b in pieces:
            if hi <= a or lo >= b:
                next_pieces.append((a, b))
                continue
            if a < lo:
                next_pieces.append((a, lo))
            if hi < b:
                next_pieces.append((hi, b))
        pieces = next_pieces
    return sum(b - a for a, b in pieces)


def test_p0_3_every_benchmark_moment_carries_its_first_grants(benchmark) -> None:
    from backend.database.repositories.moments import MomentRepository
    from backend.moments.grants import spans_of

    moments = MomentRepository(benchmark).list_for_project(BENCHMARK)
    assert moments
    for moment in moments:
        chain = spans_of(moment)
        assert chain, f"moment {moment.metadata.get('id')} carries no grant"
        assert chain[0].covers(moment.start_seconds, moment.end_seconds)
        governing = authorization.newest_for(chain, moment.media_id)
        assert governing is not None and governing.covers(moment.context_start, moment.context_end)
