"""P0.3 — the first grants are the moments stage's, and they survive the table."""

from __future__ import annotations

import pytest

from backend.core.models.enums import GameEventType
from backend.gaming.correlation import GameEvent
from backend.moments import grants
from backend.moments.formation import form_moments, replace_moment
from backend.timeline.authorization import Granter

pytestmark = pytest.mark.unit


def _event(kind: GameEventType, at: float, *, duration: float = 4.0) -> GameEvent:
    return GameEvent(
        event_type=kind,
        start_seconds=at,
        end_seconds=at + duration,
        confidence=0.9,
        importance=0.7,
        sources=("ocr",),
    )


def _moment(config, *, exclusions=()):
    (moment,) = form_moments(
        [_event(GameEventType.KILL, 100.0)],
        config.moments.formation,
        media_id="media-1",
        excluded_spans=list(exclusions),
    )
    return moment


class TestTheFirstGrants:
    def test_p0_3_a_formed_moment_carries_core_then_context(self, config) -> None:
        (granted,) = grants.grant_first_spans([_moment(config)])
        chain = grants.spans_of(granted)
        assert [span.granted_by for span in chain] == [
            Granter.MOMENT_CORE,
            Granter.CONTEXT_EXPANSION,
        ]
        core, context = chain
        assert (core.start, core.end) == (granted.start_seconds, granted.end_seconds)
        assert (context.start, context.end) == (granted.context_start, granted.context_end)
        assert "kill" in core.reason
        assert "context expansion" in context.reason

    def test_p0_3_the_context_grant_is_cut_by_an_exclusion_it_still_reaches(
        self, config
    ) -> None:
        # Defence in depth: whatever widened the context past the pull-back,
        # the grant stops at the exclusion and so does the stored context.
        moment = replace_moment(_moment(config), context_start=80.0, context_end=130.0)
        (granted,) = grants.grant_first_spans([moment], exclusions=[(112.0, 140.0)])
        assert granted.context_end <= 112.0
        assert grants.spans_of(granted)[1].end <= 112.0
        assert "cut back to gameplay" in grants.spans_of(granted)[1].reason

    def test_p0_3_a_moment_without_a_chain_reads_as_empty(self, config) -> None:
        assert grants.spans_of(_moment(config)) == ()


@pytest.fixture
def database(tmp_path):
    from backend.config.schema import DatabaseConfig
    from backend.database.connection import Database
    from backend.database.migrator import migrate

    db = Database(tmp_path / "grants.db", DatabaseConfig())
    migrate(db)
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at, "
        "target_duration_seconds, project_directory, application_version, "
        "analysis_version, schema_version) VALUES "
        "('proj-1', 'p', '2026-01-01', '2026-01-01', 1200, '/p', '1', 1, 1)",
        (),
    )
    db.execute(
        "INSERT INTO media (id, project_id, role, state, source_path, filename, "
        "container, size_bytes, checksum, duration_seconds, created_at, updated_at) "
        "VALUES ('media-1', 'proj-1', 'primary', 'ready', '/x.mkv', 'x.mkv', "
        "'mkv', 1, 'abc', 600.0, '2026-01-01', '2026-01-01')",
        (),
    )
    try:
        yield db
    finally:
        db.close()


class TestTheChainSurvivesTheTable:
    def test_p0_3_the_grant_chain_survives_the_moments_table(self, config, database) -> None:
        from backend.database.repositories.moments import MomentRepository

        (granted,) = grants.grant_first_spans([_moment(config)])
        repository = MomentRepository(database)
        with database.transaction():
            repository.replace_for_media("proj-1", "media-1", [granted])
        (stored,) = repository.list_for_project("proj-1")
        assert grants.spans_of(stored) == grants.spans_of(granted)

    def test_p0_3_a_moment_stored_before_p0_3_carries_no_grant(self, database) -> None:
        # The column's default is an empty list; nothing is backfilled.
        from backend.database.repositories.moments import MomentRepository

        database.execute(
            "INSERT INTO moments (id, project_id, media_id, moment_type, start_seconds, "
            "end_seconds, context_start, context_end, score, confidence, dead_time_score, "
            "repetition_score, score_breakdown, explanation, event_ids, needs_review, "
            "user_state, thumbnail_path, analysis_version, created_at) VALUES "
            "('mom-old', 'proj-1', 'media-1', 'skill', 100, 104, 90, 114, 0.5, 0.9, 0, 0, "
            "'{}', '[]', '[]', 0, 'auto', NULL, 1, '2026-01-01')",
            (),
        )
        (stored,) = MomentRepository(database).list_for_project("proj-1")
        assert grants.spans_of(stored) == ()
