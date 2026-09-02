"""A moment's events survive the round trip (V2-P2.0 data integrity).

`GameEventType.UNKNOWN_EVENT` was called `unexpected_event` until V2-P2. That
phase migrated the `game_events` table and did not migrate the `event_ids` JSON
on `moments`, and the loader filtered stored names against the enum:

    types = [value for value in loads(row["event_ids"]) if value in known]

Anything the enum no longer recognised was dropped, silently, on every read
since. Measured on this machine before the fix: **623 of 1,119 event references
gone, and 210 of 435 moments loading with no events at all** -- every one of
them a moment whose events were all unnamed, which is the entire population the
`surprise` label describes.

It reached further than a missing count. `Moment.confidence` is
`max(event.confidence, default=0.0)`, so a moment stripped of its events read
**0.059** where its stored confidence was **0.816** -- and V2-P1.8's audit drew
a conclusion from that gap before noticing where the number came from.

Two rules keep it from happening again, and both are tested here rather than
described:

* every valid reference survives `database -> repository -> moment`;
* an invalid one is **never dropped in silence**.
"""

from __future__ import annotations

import pytest

from backend.config.schema import DatabaseConfig
from backend.core.models.enums import GameEventType, MomentType
from backend.database.connection import Database, dumps
from backend.database.migrator import migrate
from backend.database.repositories.gaming import GameEventRepository
from backend.database.repositories.moments import (
    _RENAMED_EVENT_TYPES,
    MomentRepository,
    _stored_types,
)
from backend.gaming.correlation import GameEvent

pytestmark = pytest.mark.unit


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "integrity.db", DatabaseConfig())
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


def _event(kind: GameEventType, start: float, end: float, importance: float) -> GameEvent:
    return GameEvent(
        event_type=kind,
        start_seconds=start,
        end_seconds=end,
        confidence=0.9,
        importance=importance,
        sources=("audio", "vision"),
    )


def _store_events(database, events) -> None:
    GameEventRepository(database).replace_for_media("proj-1", "media-1", events)


def _store_moment(database, *, types, start=100.0, end=160.0, confidence=0.816):
    """Write a moment row directly, so the stored `event_ids` can be exact."""
    database.execute(
        "INSERT INTO moments (id, project_id, media_id, moment_type, start_seconds, "
        "end_seconds, context_start, context_end, score, confidence, dead_time_score, "
        "repetition_score, score_breakdown, explanation, event_ids, needs_review, "
        "user_state, thumbnail_path, analysis_version, created_at, phases) VALUES "
        "('mom-1', 'proj-1', 'media-1', 'surprise', ?, ?, ?, ?, 0.5, ?, 0.0, 0.0, "
        "'{}', '[]', ?, 0, 'auto', NULL, 1, '2026-01-01', '[]')",
        (start, end, start, end, confidence, dumps(list(types))),
    )


class TestTheRenameIsHonoured:
    """The migration that was never written, applied at read time."""

    def test_the_old_name_still_resolves(self) -> None:
        assert _RENAMED_EVENT_TYPES["unexpected_event"] == GameEventType.UNKNOWN_EVENT.value

    def test_a_row_written_before_the_rename_loads_its_events(self, database) -> None:
        _store_events(
            database,
            [
                _event(GameEventType.UNKNOWN_EVENT, 110.0, 120.0, 0.4),
                _event(GameEventType.UNKNOWN_EVENT, 130.0, 140.0, 0.4),
            ],
        )
        _store_moment(database, types=["unexpected_event", "unexpected_event"])

        moment = MomentRepository(database).list_for_project("proj-1")[0]

        assert len(moment.events) == 2
        assert {e.event_type for e in moment.events} == {GameEventType.UNKNOWN_EVENT}

    def test_a_moment_of_only_unnamed_events_is_not_left_empty(self, database) -> None:
        """The 210-of-435 case. A moment stripped of its events reports a
        confidence of zero, because confidence is a max over them."""
        _store_events(database, [_event(GameEventType.UNKNOWN_EVENT, 110.0, 150.0, 0.4)])
        _store_moment(database, types=["unexpected_event"], confidence=0.816)

        moment = MomentRepository(database).list_for_project("proj-1")[0]

        assert moment.events, "the moment loaded with no events at all"
        assert moment.confidence == pytest.approx(0.9)


class TestNothingIsDroppedInSilence:
    def test_an_unresolvable_reference_is_reported_rather_than_removed(self) -> None:
        """The rule the original loader broke. A name this build cannot resolve
        is a fact about the database worth surfacing, not a line to skip."""

        class _Row:
            def __getitem__(self, key):
                return dumps(["kill", "not_a_real_event_type", "unexpected_event"])

        resolved, unresolvable = _stored_types(_Row())

        assert resolved == ["kill", GameEventType.UNKNOWN_EVENT.value]
        assert unresolvable == ["not_a_real_event_type"]

    def test_every_stored_reference_is_accounted_for(self) -> None:
        """Resolved plus unresolvable equals what was stored. No third
        outcome, which is where the 623 went."""

        class _Row:
            def __getitem__(self, key):
                return dumps(["kill", "bogus", "unexpected_event", "victory"])

        resolved, unresolvable = _stored_types(_Row())

        assert len(resolved) + len(unresolvable) == 4


class TestTheRoundTripCarriesTheRealEvents:
    def test_timestamps_importance_and_sources_all_survive(self, database) -> None:
        """`Moment.importance` read 0.0 for every loaded moment on this machine,
        and every event reported the moment's own span."""
        _store_events(
            database,
            [
                _event(GameEventType.UNKNOWN_EVENT, 105.0, 112.0, 0.40),
                _event(GameEventType.VICTORY, 130.0, 141.0, 0.90),
            ],
        )
        _store_moment(database, types=["unexpected_event", "victory"])

        moment = MomentRepository(database).list_for_project("proj-1")[0]

        assert len(moment.events) == 2
        victory = next(e for e in moment.events if e.event_type is GameEventType.VICTORY)
        assert victory.start_seconds == 130.0, "the event's own span, not the moment's"
        assert victory.end_seconds == 141.0
        assert victory.importance == pytest.approx(0.90)
        assert victory.sources == ("audio", "vision")
        assert moment.importance == pytest.approx(0.90)

    def test_events_come_back_in_time_order(self, database) -> None:
        _store_events(
            database,
            [
                _event(GameEventType.VICTORY, 140.0, 150.0, 0.9),
                _event(GameEventType.KILL, 105.0, 110.0, 0.5),
            ],
        )
        _store_moment(database, types=["victory", "kill"])

        moment = MomentRepository(database).list_for_project("proj-1")[0]

        assert [e.start_seconds for e in moment.events] == [105.0, 140.0]

    def test_every_read_path_hydrates(self, database) -> None:
        """Three methods read moments and all three must agree; the review
        screen and the timeline would otherwise see different footage."""
        _store_events(database, [_event(GameEventType.VICTORY, 130.0, 141.0, 0.9)])
        _store_moment(database, types=["victory"])
        repository = MomentRepository(database)

        for moments in (
            repository.list_for_project("proj-1"),
            repository.list_for_media("media-1"),
            repository.in_time_order("media-1"),
        ):
            assert moments[0].events[0].importance == pytest.approx(0.9)


class TestTheGuardRefusesToGuess:
    """`event_ids` records types, not identifiers, so the match is by span and
    by type multiset together. A partial match means the stores have moved
    under this moment, and substituting a different event there would be the
    reinterpretation this repository is not allowed to do."""

    def test_a_different_event_in_the_span_is_not_substituted(self, database) -> None:
        _store_events(database, [_event(GameEventType.DEATH, 130.0, 141.0, 0.9)])
        _store_moment(database, types=["victory"])

        moment = MomentRepository(database).list_for_project("proj-1")[0]

        assert [e.event_type for e in moment.events] == [GameEventType.VICTORY]
        assert moment.events[0].start_seconds == 100.0, "fell back to the moment's span"
        assert moment.events[0].importance == 0.0

    def test_a_missing_event_falls_back_rather_than_shortening(self, database) -> None:
        _store_events(database, [_event(GameEventType.VICTORY, 500.0, 510.0, 0.9)])
        _store_moment(database, types=["victory"])

        moment = MomentRepository(database).list_for_project("proj-1")[0]

        assert len(moment.events) == 1, "the type is still described"
        assert moment.events[0].importance == 0.0, "but nothing was invented for it"

    def test_a_moment_with_no_references_stays_empty(self, database) -> None:
        _store_events(database, [_event(GameEventType.VICTORY, 130.0, 141.0, 0.9)])
        _store_moment(database, types=[])

        assert MomentRepository(database).list_for_project("proj-1")[0].events == ()


class TestTheEditorialLayerSeesTheSameEvents:
    def test_a_moment_reaches_the_evidence_layer_with_its_events(self, database) -> None:
        """The last link of `database -> repository -> moment -> evidence`.

        `ShotSemantics` counts named events and reads where the first one
        starts; with the events stripped it was reading a moment that appeared
        to contain nothing.
        """
        _store_events(
            database,
            [
                _event(GameEventType.UNKNOWN_EVENT, 105.0, 112.0, 0.4),
                _event(GameEventType.VICTORY, 130.0, 141.0, 0.9),
            ],
        )
        _store_moment(database, types=["unexpected_event", "victory"])

        moment = MomentRepository(database).list_for_project("proj-1")[0]
        named = [e for e in moment.events if e.event_type is not GameEventType.UNKNOWN_EVENT]

        assert named, "the editorial layer would read this shot as empty"
        assert min(e.start_seconds for e in named) == 130.0
        assert moment.moment_type is MomentType.SURPRISE
