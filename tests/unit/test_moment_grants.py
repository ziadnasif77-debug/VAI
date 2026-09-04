"""P0.3 — the first grants are the moments stage's, and they survive the table."""

from __future__ import annotations

import pytest

from backend.core.models.enums import GameEventType
from backend.gaming.correlation import GameEvent
from backend.moments import grants
from backend.moments.formation import form_moments, replace_moment
from backend.timeline.authorization import AuthorizationError, Granter

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

    def test_p0_3_a_point_moment_is_granted_its_context_only(self, config) -> None:
        # The benchmark holds a surprise at 680.7 s with no duration of its
        # own. There is no core span to grant; the context is the first and
        # only grant, and it covers the instant.
        moment = replace_moment(_moment(config), start_seconds=680.7, end_seconds=680.7)
        moment = replace_moment(moment, context_start=670.0, context_end=690.0)
        (granted,) = grants.grant_first_spans([moment])
        chain = grants.spans_of(granted)
        assert [span.granted_by for span in chain] == [Granter.CONTEXT_EXPANSION]
        assert chain[0].covers(680.7, 680.7)

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


class TestWideningsAreGrants:
    def _granted(self, config, **context):
        (moment,) = grants.grant_first_spans([_moment(config)])
        return replace_moment(moment, **context) if context else moment

    def test_p0_3_a_marked_widening_becomes_a_new_span_by_its_granter(self, config) -> None:
        moment = self._granted(config)
        widened = grants.note_widening(
            moment, Granter.DURATION_OPTIMIZER, start=moment.context_start - 5.0,
            end=moment.context_end + 5.0, reason="duration optimizer: +5.0 s before / +5.0 s after",
        )
        (result,) = grants.grant_widenings([widened], {})
        chain = grants.spans_of(result)
        assert chain[-1].granted_by is Granter.DURATION_OPTIMIZER
        assert (chain[-1].start, chain[-1].end) == (result.context_start, result.context_end)
        assert "+5.0 s before" in chain[-1].reason
        assert grants.WIDENED_KEY not in result.metadata, "the mark is spent"
        assert len(chain) == 3, "the earlier grants are kept, immutable"

    def test_p0_3_an_unmarked_widening_fails_hard(self, config) -> None:
        moment = self._granted(config)
        widened = replace_moment(moment, context_end=moment.context_end + 10.0)
        with pytest.raises(AuthorizationError, match="no listed step widened it"):
            grants.grant_widenings([widened], {})

    def test_p0_3_a_widening_into_an_exclusion_is_cut_at_the_grant(self, config) -> None:
        moment = self._granted(config)
        menu = (moment.context_end + 3.0, moment.context_end + 60.0)
        widened = grants.note_widening(
            moment, Granter.REFINEMENT, start=moment.context_start,
            end=moment.context_end + 20.0, reason="refinement: end +20.0 s",
        )
        (result,) = grants.grant_widenings([widened], {"media-1": [menu]})
        assert result.context_end <= menu[0]
        assert grants.spans_of(result)[-1].end <= menu[0]

    def test_p0_3_narrowing_needs_no_grant(self, config) -> None:
        moment = self._granted(config)
        narrowed = replace_moment(moment, context_end=moment.context_end - 1.0)
        (result,) = grants.grant_widenings([narrowed], {})
        assert len(grants.spans_of(result)) == 2

    def test_p0_3_moments_without_a_chain_are_refused_by_the_pipeline(self, config) -> None:
        with pytest.raises(grants.AuthorizationChainMissingError, match="re-run MOMENTS"):
            grants.require_chain([_moment(config)])
        grants.require_chain([self._granted(config)])

    def test_p0_3_the_optimizer_and_refinement_mark_what_they_widen(self, config) -> None:
        from backend.narrative import optimizer, refinement

        moment = self._granted(config)
        grown, gain = optimizer._grow_context([moment], 10.0, 0.5, {"media-1": 600.0})
        assert gain > 0
        marks = grown[0].metadata[grants.WIDENED_KEY]
        assert marks[-1]["granted_by"] == "duration_optimizer"
        assert "towards the target" in marks[-1]["reason"]

        index = refinement.SpeechIndex([], 0.35)
        untouched, snapped = refinement._snap_moment(moment, index, 2.5)
        assert snapped == 0 and grants.WIDENED_KEY not in untouched.metadata
