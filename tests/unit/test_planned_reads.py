"""V2-P0.4: which base frames the EDL stage reads, and how the reads are kept.

The selection is pure and owns the rules here. The wire through the worker --
frames read with the fake engine, reads appended beside the OCR stage's own,
frames marked so a second run reads nothing -- lives in
``tests/integration/test_edl_pipeline.py::TestPlannedFrameReads``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai.providers.base import TextDetection
from backend.database.repositories.frames import FrameRepository
from backend.database.repositories.gaming import OcrRepository
from backend.gaming import planned_reads

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Frame:
    timestamp: float
    analyzed: bool = False
    id: str = ""


def _frames(*timestamps: float, analyzed: bool = False) -> list[_Frame]:
    return [_Frame(at, analyzed, f"frame-{at:.0f}") for at in timestamps]


class TestWhichFramesAreRead:
    def test_frames_inside_a_planned_clip_are_chosen(self) -> None:
        chosen = planned_reads.select(
            _frames(100.0, 103.0, 106.0, 200.0), [(101.0, 107.0)], [], margin_seconds=0.0
        )
        assert [f.timestamp for f in chosen] == [103.0, 106.0]

    def test_the_frame_just_before_the_clip_is_chosen_with_the_margin(self) -> None:
        # The benchmark's pause menu: the clip opened at 525.267 and the base
        # frame that showed the menu in full sat at 525.0, outside by 0.27 s.
        clips = [(525.267, 534.0)]
        strict = planned_reads.select(_frames(525.0, 528.0), clips, [], margin_seconds=0.0)
        reaching = planned_reads.select(_frames(525.0, 528.0), clips, [], margin_seconds=3.0)
        assert [f.timestamp for f in strict] == [528.0]
        assert [f.timestamp for f in reaching] == [525.0, 528.0]

    def test_a_frame_a_stored_sample_already_speaks_for_is_skipped(self) -> None:
        looked = [104.0]
        near = planned_reads.select(_frames(103.0), [(100.0, 110.0)], looked, min_gap_seconds=2.0)
        far = planned_reads.select(_frames(106.5), [(100.0, 110.0)], looked, min_gap_seconds=2.0)
        every = planned_reads.select(_frames(103.0), [(100.0, 110.0)], looked, min_gap_seconds=0.0)
        assert near == []
        assert [f.timestamp for f in far] == [106.5]
        assert [f.timestamp for f in every] == [103.0]

    def test_a_frame_already_read_is_never_read_again(self) -> None:
        chosen = planned_reads.select(
            _frames(103.0, analyzed=True) + _frames(106.0), [(100.0, 110.0)], []
        )
        assert [f.timestamp for f in chosen] == [106.0]

    def test_nothing_planned_means_nothing_read(self) -> None:
        assert planned_reads.select(_frames(1.0, 2.0), [], []) == []
        assert planned_reads.select([], [(0.0, 10.0)], []) == []

    def test_the_result_is_in_time_order_whatever_the_input_order(self) -> None:
        chosen = planned_reads.select(_frames(9.0, 3.0, 6.0), [(0.0, 10.0)], [])
        assert [f.timestamp for f in chosen] == [3.0, 6.0, 9.0]


@pytest.fixture
def database(tmp_path):
    """A migrated database with the project and media rows the tables key on."""
    from backend.config.schema import DatabaseConfig
    from backend.database.connection import Database
    from backend.database.migrator import migrate

    db = Database(tmp_path / "planned.db", DatabaseConfig())
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


class TestHowTheReadsAreKept:
    """The repository verbs the pass depends on."""

    @staticmethod
    def _read(text: str, at: float) -> TextDetection:
        return TextDetection(text=text, confidence=0.9, timestamp=at, region=None)

    def test_adding_reads_keeps_the_stage_s_own(self, database) -> None:
        repository = OcrRepository(database)
        with database.transaction():
            repository.replace_for_media("proj-1", "media-1", [self._read("VICTORY", 10.0)])
            repository.add_for_media("proj-1", "media-1", [self._read("RESUME", 13.0)])
        texts = sorted(item.text for item in repository.list_for_media("media-1"))
        assert texts == ["RESUME", "VICTORY"]

    def test_replacing_forgets_the_added_ones_too(self, database) -> None:
        # The OCR stage re-running is a fresh start for the recording's text;
        # the planned pass will read its frames again afterwards.
        repository = OcrRepository(database)
        with database.transaction():
            repository.replace_for_media("proj-1", "media-1", [self._read("VICTORY", 10.0)])
            repository.add_for_media("proj-1", "media-1", [self._read("RESUME", 13.0)])
            repository.replace_for_media("proj-1", "media-1", [self._read("DEFEAT", 20.0)])
        assert [item.text for item in repository.list_for_media("media-1")] == ["DEFEAT"]

    def test_marked_frames_can_be_reset_by_level(self, database) -> None:
        from pathlib import Path

        from backend.media.frames import ExtractedFrame

        repository = FrameRepository(database)
        with database.transaction():
            repository.add_many(
                "proj-1",
                "media-1",
                [
                    ExtractedFrame(timestamp=float(index * 3), path=Path(f"/n/{index}.jpg"), level=level)
                    for index, level in enumerate(("base", "base", "candidate"))
                ],
            )
            stored = repository.list_for_media("media-1")
            repository.mark_analyzed([frame.id for frame in stored])
        assert all(f.analyzed for f in repository.list_for_media("media-1"))

        with database.transaction():
            reset = repository.reset_analyzed("media-1", level="base")
        assert reset == 2
        unread = repository.list_for_media("media-1", analyzed=False)
        assert [f.timestamp for f in unread] == [0.0, 3.0]
        assert repository.list_for_media("media-1", level="candidate")[0].analyzed
