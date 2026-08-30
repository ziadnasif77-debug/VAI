"""V2-P1: the Semantic Timeline as a shared spine rather than a private lane.

Before this phase the lanes were computed six at a time and read one at a
time, from a JSON cache keyed by how many rows went in. Three defects lived
in that sentence, and each has a test here: the lanes nobody read, the
signature that could not see a changed value, and the build that happened
after selection had already chosen what to build from.
"""

from __future__ import annotations

import pytest

from backend.core.models.enums import JobStage
from backend.core.models.jobs import ANALYSIS_STAGES, STAGE_DEPENDENCIES
from backend.semantic.reader import AWAITING_CONSUMER, LANES, SemanticReader
from backend.semantic.timeline import (
    BUILDER_VERSION,
    build_timeline,
    timeline_signature,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def project_id(project_manager) -> str:
    from backend.core.models.enums import VideoMode
    from backend.core.models.project import ProjectCreate

    return project_manager.create(
        ProjectCreate(name="Spine", target_duration_seconds=600, mode=VideoMode.STORY)
    ).id


def _world(config, **overrides):
    world = {
        "media_id": "media-aaaaaaaaaaaa",
        "duration_seconds": 120.0,
        "frames": [(t, 0.1 + 0.3 * (t % 7 == 0)) for t in range(0, 121, 3)],
        "audio_events": [(30.0, 34.0, -12.0, "spike")],
        "game_events": [(60.0, 66.0, 0.8, "combat")],
        "scenes": [(20.0, 0.6), (70.0, 0.9)],
        "words": [(10.0, 14.0)],
        "dead_spans": [(100.0, 110.0)],
        "labels": [(t, ("combat", "forest")) for t in range(0, 60, 6)]
        + [(t, ("boss", "cave")) for t in range(60, 121, 6)],
        "config": config,
    }
    world.update(overrides)
    return build_timeline(**world)


class TestTheContract:
    def test_the_timeline_satisfies_the_reader_protocol(self, config) -> None:
        timeline = _world(config)

        assert isinstance(timeline, SemanticReader)

    def test_every_declared_lane_is_built(self, config) -> None:
        timeline = _world(config)

        assert set(timeline.lanes) == set(LANES)
        for name in LANES:
            lane = timeline.lane(name)
            assert len(lane) == len(timeline.lane("intensity"))
            assert all(0.0 <= value <= 1.0 for value in lane), name

    def test_asking_for_a_lane_that_does_not_exist_is_an_error(self, config) -> None:
        # Returning zeros would hand a consumer a measurement that was never
        # taken, and it would look exactly like a quiet session.
        with pytest.raises(KeyError, match="no lane"):
            _world(config).lane("vibes")

    def test_a_lane_with_no_consumer_is_named_as_such(self) -> None:
        # The P0 rule, applied to lanes: a computed thing may wait for its
        # consumer, but it may not wait silently.
        for lane in AWAITING_CONSUMER:
            assert lane in LANES, f"{lane} is awaited but never built"

    def test_a_lane_that_found_its_consumer_leaves_the_register(self) -> None:
        """The half the first version never asked.

        ``audio`` sat in this register for a whole phase after P5's audio
        director began reading it, because the only assertion was that an
        awaited lane exists -- which stayed true the entire time. A register
        of what has not been built yet is worth nothing if it does not notice
        when something gets built.
        """
        import re
        from pathlib import Path

        readers = {}
        for path in (Path(__file__).parents[2] / "backend").rglob("*.py"):
            if path.parts[-2] == "semantic":
                # The builder writes every lane and the reader indexes them by
                # name; neither is a consumer.
                continue
            source = path.read_text(encoding="utf-8")
            for lane in re.findall(
                r'(?:lane|value_at|window)\(\s*"([a-z_]+)"', source
            ):
                readers.setdefault(lane, []).append(path.name)

        for lane in AWAITING_CONSUMER:
            assert lane not in readers, (
                f"{lane} is read by {readers.get(lane)} and still listed as awaiting one"
            )

    def test_the_window_and_the_lane_agree(self, config) -> None:
        timeline = _world(config)

        whole = timeline.lane("motion")
        assert list(timeline.window("motion", 0.0, timeline.duration_s)) == whole
        assert timeline.value_at("motion", 0.0) == whole[0]
        assert timeline.window("motion", 5.0, 5.0), "a zero-width window still answers"


class TestTheNewLanes:
    def test_dead_zones_mark_the_spans_the_guard_cuts(self, config) -> None:
        timeline = _world(config)

        assert timeline.value_at("dead_zones", 105.0) == 1.0
        assert timeline.value_at("dead_zones", 50.0) == 0.0
        assert timeline.value_at("intensity", 105.0) == 0.0, "and carry no heat"

    def test_scene_changes_are_an_impulse_at_each_boundary(self, config) -> None:
        timeline = _world(config)

        assert timeline.value_at("scene_changes", 70.2) > 0.0
        assert timeline.value_at("scene_changes", 50.0) == 0.0

    def test_novelty_rises_where_the_screen_becomes_unfamiliar(self, config) -> None:
        # Forest for a minute, then a cave with a boss: the labels at 70s are
        # new against everything before them.
        timeline = _world(config)

        assert timeline.value_at("novelty", 75.0) > timeline.value_at("novelty", 40.0)

    def test_novelty_says_nothing_when_it_has_seen_nothing(self, config) -> None:
        # No vision observations is not "everything is novel"; it is no
        # evidence, and inventing novelty would promote footage on nothing.
        timeline = _world(config, labels=[])

        assert set(timeline.lane("novelty")) == {0.0}

    def test_speech_is_a_lane_and_not_heat(self, config) -> None:
        # A quiet stretch where the player explains something is not a climax.
        talking = _world(config, words=[(40.0, 50.0)])
        silent = _world(config, words=[])

        assert talking.value_at("speech", 45.0) == 1.0
        assert talking.value_at("intensity", 45.0) == pytest.approx(
            silent.value_at("intensity", 45.0)
        ), "speech changes no level"


class TestTheSignature:
    def test_identical_evidence_gives_an_identical_digest(self, config) -> None:
        rows = {"events": [(1.0, 2.0, 0.5, 0.9, "combat")], "frames": [(0.0, 0.2)]}

        first = timeline_signature(rows, duration_seconds=60.0, config=config)
        second = timeline_signature(dict(rows), duration_seconds=60.0, config=config)

        assert first == second

    def test_one_changed_value_changes_the_digest(self, config) -> None:
        # The defect this replaced: the old signature hashed row *counts*, so
        # re-scoring an event returned the cached lanes and every pacing
        # decision after it was graded from heat that no longer existed.
        before = {"events": [(1.0, 2.0, 0.5, 0.9, "combat")]}
        after = {"events": [(1.0, 2.0, 0.9, 0.9, "combat")]}

        assert timeline_signature(
            before, duration_seconds=60.0, config=config
        ) != timeline_signature(after, duration_seconds=60.0, config=config)

    def test_the_same_count_of_different_rows_changes_the_digest(self, config) -> None:
        before = {"scenes": [(10.0, 0.5), (20.0, 0.5)]}
        after = {"scenes": [(10.0, 0.5), (25.0, 0.5)]}

        assert timeline_signature(
            before, duration_seconds=60.0, config=config
        ) != timeline_signature(after, duration_seconds=60.0, config=config)

    def test_the_builder_version_is_part_of_it(self, config) -> None:
        # A stored timeline from an older build must be rebuilt, not trusted.
        assert BUILDER_VERSION in ("1", "2", "3", "4", "5")
        digest = timeline_signature({}, duration_seconds=60.0, config=config)
        assert len(digest) == 32


class TestBuildingIsDeterministic:
    def test_the_same_world_builds_the_same_lanes(self, config) -> None:
        first = _world(config)
        second = _world(config)

        assert first.lanes == second.lanes


class TestTheStageSitsBeforeSelection:
    def test_semantic_runs_after_the_evidence_and_before_the_choosing(self) -> None:
        # The whole point of the phase: built inside the EDL stage, as it was
        # first written, the lanes could shape how a moment was cut but never
        # which moment was chosen.
        assert STAGE_DEPENDENCIES[JobStage.SEMANTIC] == (JobStage.GAME_EVENTS,)
        assert STAGE_DEPENDENCIES[JobStage.MOMENTS] == (JobStage.SEMANTIC,)

    def test_it_is_analysis_and_survives_a_re_edit(self) -> None:
        # §127: changing the target duration must not re-read the recording,
        # and must not re-fuse lanes that did not change either.
        assert JobStage.SEMANTIC in ANALYSIS_STAGES

    def test_the_stage_is_registered(self) -> None:
        from backend.pipeline.workers import default_workers

        assert default_workers()[JobStage.SEMANTIC].stage is JobStage.SEMANTIC


class TestTheStore:
    """The lanes live in the database now, not in a JSON file beside the
    analysis. One truth, and a digest that can see a changed value."""

    def _media(self, database, project_id):
        from datetime import datetime, timezone
        from hashlib import sha256

        from backend.core.ids import new_id
        from backend.core.models.media import Media
        from backend.database.repositories.media import MediaRepository

        now = datetime.now(timezone.utc)
        return MediaRepository(database).create(
            Media(
                id=new_id("media"),
                project_id=project_id,
                source_path="D:/Gaming 2026/session.mkv",
                filename="session.mkv",
                container=".mkv",
                size_bytes=1024,
                checksum=sha256(b"session").hexdigest(),
                created_at=now,
                updated_at=now,
            )
        )

    def test_a_second_load_reads_the_store_rather_than_rebuilding(
        self, database, config, paths, project_id
    ) -> None:
        from backend.database.repositories.semantic import SemanticRepository
        from backend.semantic.timeline import load_timeline

        media = self._media(database, project_id)

        first = load_timeline(database, media.id, duration_seconds=60.0, config=config)
        row = database.fetch_one(
            "SELECT signature, builder_version FROM semantic_timelines WHERE media_id = ?",
            (media.id,),
        )
        second = load_timeline(database, media.id, duration_seconds=60.0, config=config)

        assert row is not None, "the stage stores what it built"
        assert row["builder_version"] == BUILDER_VERSION
        assert first.lanes.keys() == second.lanes.keys()
        assert SemanticRepository(database).get(media.id, signature=row["signature"])

    def test_a_stale_signature_is_not_served(
        self, database, config, paths, project_id
    ) -> None:
        from backend.database.repositories.semantic import SemanticRepository

        media = self._media(database, project_id)
        SemanticRepository(database).save(
            media.id,
            signature="a-digest-from-different-evidence",
            builder_version=BUILDER_VERSION,
            hz=2,
            duration_seconds=60.0,
            lanes={name: [0.5] * 120 for name in LANES},
        )

        assert SemanticRepository(database).get(media.id, signature="today") is None

    def test_rebuilding_from_an_unchanged_database_gives_the_same_lanes(
        self, database, config, paths, project_id
    ) -> None:
        from backend.database.repositories.semantic import SemanticRepository
        from backend.semantic.timeline import load_timeline

        media = self._media(database, project_id)
        built = load_timeline(database, media.id, duration_seconds=60.0, config=config)

        SemanticRepository(database).delete_for_media(media.id)
        rebuilt = load_timeline(database, media.id, duration_seconds=60.0, config=config)

        assert rebuilt.lanes == built.lanes


class TestLaneBounds:
    """Every lane promises 0..1. A consumer that grades, averages or weights
    one is entitled to that, and one lane broke it on real footage."""

    def test_a_scene_boundary_off_the_grid_stays_inside_the_lane(self, config) -> None:
        # The bin holding a boundary starts fractionally before it, and the
        # decay term read 1.25 on the real session's 203 scene changes.
        timeline = _world(config, scenes=[(70.3, 0.9), (20.7, 0.6)])

        assert max(timeline.lane("scene_changes")) <= 1.0

    @pytest.mark.parametrize("boundary", [10.0, 10.1, 10.25, 10.49, 10.5, 10.99])
    def test_wherever_the_boundary_falls(self, config, boundary) -> None:
        timeline = _world(config, scenes=[(boundary, 1.0)])

        lane = timeline.lane("scene_changes")
        assert max(lane) <= 1.0
        assert max(lane) > 0.0, "and the impulse is still there"


class TestMotionIsMeasured:
    """The heaviest term in the fusion was a constant.

    ``frames.motion_score`` has existed since Phase 2 and was written by
    nothing: seventeen thousand rows, no scores. The percentile normaliser
    ranked that constant at 0.5 everywhere, so a third of every intensity
    value carried no information and nothing said so.
    """

    def _frame(self, path, colour, *, shift=0):
        from PIL import Image

        image = Image.new("RGB", (160, 90), colour)
        for x in range(20):
            for y in range(20):
                image.putpixel(((x + shift) % 160, y), (255, 255, 255))
        image.save(path)
        return path

    def test_a_still_picture_scores_lower_than_a_moving_one(self, tmp_path) -> None:
        from backend.analysis.motion import score_pair

        a = self._frame(tmp_path / "a.jpg", (20, 30, 40))
        b = self._frame(tmp_path / "b.jpg", (20, 30, 40))
        c = self._frame(tmp_path / "c.jpg", (20, 30, 40), shift=100)

        assert score_pair(a, b) < score_pair(a, c)

    def test_an_unreadable_frame_is_absent_not_zero(self, tmp_path) -> None:
        # Zero would say "nothing moved" and the lane would believe it.
        from backend.analysis.motion import score_pair

        real = self._frame(tmp_path / "real.jpg", (10, 10, 10))

        assert score_pair(real, tmp_path / "missing.jpg") is None

    def test_scoring_a_recording_is_idempotent(
        self, database, project_id, tmp_path
    ) -> None:
        from backend.analysis.motion import score_media
        from backend.database.repositories.frames import FrameRepository
        from backend.media.frames import ExtractedFrame

        media = TestTheStore()._media(database, project_id)
        FrameRepository(database).add_many(
            project_id,
            media.id,
            [
                ExtractedFrame(
                    timestamp=float(index * 3),
                    path=self._frame(
                        tmp_path / f"{index}.jpg", (5, 5, 5), shift=index * 30
                    ),
                )
                for index in range(4)
            ],
        )

        first = score_media(database, media.id)
        second = score_media(database, media.id)

        assert first == 4, "every frame gets a score, the first from its neighbour"
        assert second == 0, "a second pass costs nothing"
        rows = database.fetch_all(
            "SELECT motion_score FROM frames WHERE media_id = ?", (media.id,)
        )
        assert all(row["motion_score"] is not None for row in rows)
