"""Outcome data, and the rules that keep it evidence (V2-P9).

Two things could go wrong here in ways that matter more than a bug: storing a
number nobody can attribute to an edit, and showing an absence as a zero. Both
turn a record of what happened into a record of nothing, and both have their
own tests below.

Nothing in this module tests learning, because there is none. P9 collects and
places; P10 is the phase permitted to act.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend.analytics.projection import DIP_DROP, project
from backend.analytics.store import OutcomeStore
from backend.analytics.youtube import RetentionPoint, Totals, YouTubeAnalytics
from backend.core.errors import ValidationError
from backend.publishing.google_oauth import ANALYTICS_SCOPE, UPLOAD_SCOPE

pytestmark = pytest.mark.unit


class _Transport:
    """Answers with what it was given, and remembers being asked."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    def request(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append(url)
        status, payload = self.replies.pop(0) if self.replies else (200, {})
        return status, {}, json.dumps(payload).encode()


class _Tokens:
    """A grant with exactly the scopes it was built with."""

    def __init__(self, *scopes, authorised: bool = True) -> None:
        self._scopes = frozenset(scopes)
        self._authorised = authorised

    def is_authorised(self) -> bool:
        return self._authorised

    def granted_scopes(self) -> frozenset[str]:
        return self._scopes

    def may_read_analytics(self) -> bool:
        return ANALYTICS_SCOPE in self._scopes

    def access_token(self) -> str:
        return "token"


def _totals_reply(**metrics):
    names = list(metrics)
    return {
        "columnHeaders": [{"name": name} for name in names],
        "rows": [[metrics[name] for name in names]],
    }


class TestTheGrantIsInspectable:
    """A widened scope in the code does not widen a grant already on disk."""

    def test_an_older_grant_is_named_rather_than_discovered_as_a_403(self) -> None:
        analytics = YouTubeAnalytics(_Tokens(UPLOAD_SCOPE), transport=_Transport())

        assert analytics.is_ready() is False
        assert "predates the analytics scope" in (analytics.why_not() or "")

    def test_no_connection_says_so_differently(self) -> None:
        analytics = YouTubeAnalytics(
            _Tokens(authorised=False), transport=_Transport()
        )

        assert "not connected" in (analytics.why_not() or "")

    def test_a_full_grant_is_ready(self) -> None:
        analytics = YouTubeAnalytics(
            _Tokens(UPLOAD_SCOPE, ANALYTICS_SCOPE), transport=_Transport()
        )

        assert analytics.is_ready() is True
        assert analytics.why_not() is None

    def test_a_report_is_refused_before_the_network_is_touched(self) -> None:
        transport = _Transport((200, _totals_reply(views=10)))
        analytics = YouTubeAnalytics(_Tokens(UPLOAD_SCOPE), transport=transport)

        with pytest.raises(Exception, match="predates the analytics scope"):
            analytics.totals("vid", start_date="2026-08-01", end_date="2026-08-28")

        assert transport.calls == [], "a refusal must not ask YouTube anyway"


class TestReadingTheReport:
    def _client(self, *replies):
        return YouTubeAnalytics(
            _Tokens(UPLOAD_SCOPE, ANALYTICS_SCOPE), transport=_Transport(*replies)
        )

    def test_the_named_metrics_come_back_named(self) -> None:
        client = self._client((200, _totals_reply(views=120, averageViewPercentage=41.5)))

        totals = client.totals("vid", start_date="2026-08-01", end_date="2026-08-28")

        assert totals.get("views") == pytest.approx(120)
        assert totals.get("averageViewPercentage") == pytest.approx(41.5)

    def test_a_video_nobody_watched_reports_absence_not_zero(self) -> None:
        # The distinction this whole phase turns on: no rows means "not
        # measured", and writing 0 views would make an unwatched video and an
        # unmeasured one indistinguishable.
        client = self._client((200, {"columnHeaders": [{"name": "views"}], "rows": []}))

        totals = client.totals("vid", start_date="2026-08-01", end_date="2026-08-28")

        assert totals.get("views") is None
        assert totals.metrics == {}

    def test_the_curve_comes_back_in_order_and_inside_its_bounds(self) -> None:
        client = self._client(
            (
                200,
                {
                    "columnHeaders": [
                        {"name": "elapsedVideoTimeRatio"},
                        {"name": "audienceWatchRatio"},
                    ],
                    "rows": [[0.5, 0.6], [0.0, 1.0], [1.4, 0.2]],
                },
            )
        )

        points = client.retention("vid", start_date="2026-08-01", end_date="2026-08-28")

        assert [point.elapsed_ratio for point in points] == [0.0, 0.5, 1.0]

    def test_a_report_without_its_own_columns_is_empty_not_invented(self) -> None:
        client = self._client((200, {"columnHeaders": [{"name": "views"}], "rows": [[3]]}))

        assert client.retention("vid", start_date="a", end_date="b") == []


class TestAnOutcomeMustNameAnEdit:
    def test_a_video_this_system_never_published_is_refused(
        self, database, project_manager
    ) -> None:
        store = OutcomeStore(database)
        totals = Totals(video_id="unknown", start_date="2026-08-01", end_date="2026-08-28")

        with pytest.raises(ValidationError, match=r"[Nn]o completed publish job names video"):
            store.record(totals)

    def test_a_published_video_is_stored_against_its_project(
        self, database, project_manager
    ) -> None:
        project = _published(database, project_manager, "vid-1")
        store = OutcomeStore(database)

        outcome = store.record(
            Totals(
                video_id="vid-1",
                start_date="2026-08-01",
                end_date="2026-08-28",
                metrics={"views": 40.0},
            ),
            [RetentionPoint(0.0, 1.0), RetentionPoint(0.5, 0.7)],
        )

        assert outcome.project_id == project.id
        assert outcome.has_curve
        assert store.count() == 1

    def test_reading_the_same_window_twice_is_one_fact(
        self, database, project_manager
    ) -> None:
        _published(database, project_manager, "vid-1")
        store = OutcomeStore(database)
        window = {"start_date": "2026-08-01", "end_date": "2026-08-28"}

        store.record(Totals(video_id="vid-1", metrics={"views": 40.0}, **window))
        store.record(
            Totals(video_id="vid-1", metrics={"views": 90.0}, **window),
            [RetentionPoint(0.0, 1.0)],
        )

        assert store.count() == 1
        latest = store.latest_for_test = store.latest(store.published()[0]["project_id"])
        assert latest.metrics["views"] == pytest.approx(90.0)
        assert len(latest.points) == 1

    def test_two_windows_of_one_video_are_two_facts(
        self, database, project_manager
    ) -> None:
        _published(database, project_manager, "vid-1")
        store = OutcomeStore(database)

        store.record(
            Totals(video_id="vid-1", start_date="2026-08-01", end_date="2026-08-07")
        )
        store.record(
            Totals(video_id="vid-1", start_date="2026-08-01", end_date="2026-08-28")
        )

        assert store.count() == 2


class TestPlacingTheCurveOnTheEdit:
    """Correlation. The dip is a measurement; the shot under it is a fact."""

    def test_a_ratio_becomes_a_second_and_names_the_shot(
        self, database, project_manager, config
    ) -> None:
        _project, store = _with_edit(database, project_manager, config)
        outcome = store.record(
            Totals(video_id="vid-1", start_date="a", end_date="b"),
            [RetentionPoint(0.0, 1.0), RetentionPoint(0.5, 0.9)],
        )

        projection = project_curve(database, outcome, config)

        assert projection is not None
        assert projection.duration_seconds == pytest.approx(60.0)
        assert projection.readings[1].at_seconds == pytest.approx(30.0)
        assert projection.readings[1].matched
        assert projection.matched_fraction == pytest.approx(1.0)

    def test_a_dip_says_which_shot_was_on_screen(
        self, database, project_manager, config
    ) -> None:
        _project, store = _with_edit(database, project_manager, config)
        outcome = store.record(
            Totals(video_id="vid-1", start_date="a", end_date="b"),
            [
                RetentionPoint(0.0, 1.0),
                RetentionPoint(0.5, 1.0 - DIP_DROP * 3),
                RetentionPoint(0.75, 1.0 - DIP_DROP * 3.2),
            ],
        )

        projection = project_curve(database, outcome, config)

        assert projection.dips, "a drop of three times the threshold is a dip"
        assert projection.dips[0].reading.clip_index is not None
        assert "of the audience left" in projection.dips[0].describe()

    def test_without_a_rendered_length_it_refuses_rather_than_guesses(
        self, database, project_manager, config
    ) -> None:
        # A ratio is not a time until something says how long the video was.
        project = _published(database, project_manager, "vid-1")
        database.execute(
            "UPDATE renders SET duration_seconds = NULL WHERE project_id = ?",
            (project.id,),
        )
        store = OutcomeStore(database)
        outcome = store.record(
            Totals(video_id="vid-1", start_date="a", end_date="b"),
            [RetentionPoint(0.5, 0.8)],
        )

        assert project_curve(database, outcome, config) is None

    def test_an_edit_changed_since_the_render_is_refused(
        self, database, project_manager, config
    ) -> None:
        """The curve describes shots that are no longer there.

        Reading it against the current timeline would name a shot the audience
        never saw, which is worse than saying nothing.
        """
        project, store = _with_edit(database, project_manager, config)
        database.execute(
            "UPDATE renders SET duration_seconds = 300.0 WHERE project_id = ?",
            (project.id,),
        )
        outcome = store.record(
            Totals(video_id="vid-1", start_date="a", end_date="b"),
            [RetentionPoint(0.5, 0.8)],
        )

        assert project_curve(database, outcome, config) is None

    def test_a_curveless_outcome_projects_to_nothing(
        self, database, project_manager, config
    ) -> None:
        _project, store = _with_edit(database, project_manager, config)
        outcome = store.record(Totals(video_id="vid-1", start_date="a", end_date="b"))

        assert project_curve(database, outcome, config) is None


def project_curve(database, outcome, config):
    return project(database, outcome, config=config)


def _published(database, project_manager, video_id: str):
    """A project whose PUBLISH job came back with ``video_id``.

    Written the way the system actually records a publication: on the job row.
    The ``publications`` table exists in the first schema and nothing has ever
    filled it -- a fixture that used it would pass while the real thing found
    nothing for ever.
    """
    import json

    from backend.core.ids import new_id
    from backend.core.models.project import ProjectCreate

    project = project_manager.create(
        ProjectCreate(name="Published", target_duration_seconds=600)
    )
    now = datetime.now(timezone.utc).isoformat()
    render_id = new_id("job").replace("job-", "rnd-")
    database.execute(
        "INSERT INTO renders (id, project_id, status, resolution, fps, "
        "duration_seconds, completed_at, created_at) "
        "VALUES (?, ?, 'completed', 1080, 60, 60.0, ?, ?)",
        (render_id, project.id, now, now),
    )
    database.execute(
        "INSERT INTO analysis_jobs (id, project_id, stage, status, progress, "
        "result, created_at, completed_at) "
        "VALUES (?, ?, 'publish', 'completed', 1.0, ?, ?, ?)",
        (
            new_id("job"),
            project.id,
            json.dumps(
                {
                    "status": "completed",
                    "target": "youtube",
                    "external_id": video_id,
                    "external_url": f"https://youtu.be/{video_id}",
                    "render_id": render_id,
                }
            ),
            now,
            now,
        ),
    )
    return project


def _with_edit(database, project_manager, config):
    """A published project whose sixty-second edit is still on record."""
    project = _published(database, project_manager, "vid-1")
    media_id = _media(database, project.id)
    for index in range(4):
        database.execute(
            "INSERT INTO timeline_clips (id, project_id, media_id, track, clip_index, "
            "source_in, source_out, timeline_start, timeline_end, enabled, metadata) "
            "VALUES (?, ?, ?, 'video', ?, ?, ?, ?, ?, 1, '{}')",
            (
                f"clip-{index}",
                project.id,
                media_id,
                index,
                index * 100.0,
                index * 100.0 + 15.0,
                index * 15.0,
                index * 15.0 + 15.0,
            ),
        )
    return project, OutcomeStore(database)


def _media(database, project_id: str) -> str:
    from datetime import datetime as _dt

    from backend.core.ids import new_id
    from backend.core.models.media import Media, MediaMetadata
    from backend.database.repositories.media import MediaRepository

    now = _dt.now(timezone.utc)
    media = MediaRepository(database).create(
        Media(
            id=new_id("media"),
            project_id=project_id,
            source_path="D:/recordings/session.mp4",
            filename="session.mp4",
            container=".mp4",
            size_bytes=1024,
            checksum="0" * 64,
            metadata=MediaMetadata(duration_seconds=1000.0),
            created_at=now,
            updated_at=now,
        )
    )
    return media.id
