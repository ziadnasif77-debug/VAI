"""The owner's daily production & publishing policy (2026-08-29).

One long video and two Reels a day, produced at 02:00 Europe/Oslo and public
at 10:00 Europe/Oslo, never twice, never early, restart-proof. The clock
arithmetic, the ledger's state machine and the platform-side scheduling each
get pinned here; the pipeline the policy drives is every other test file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.services.daily_producer import DailyProducer, tick

pytestmark = pytest.mark.unit

OSLO = ZoneInfo("Europe/Oslo")


def _producer(database, paths, config, *, vault: Path | None = None) -> DailyProducer:
    application = config.application.model_copy(
        update={"media_source_roots": [str(vault)] if vault else []}
    )
    daily = config.daily.model_copy(update={"enabled": True})
    return DailyProducer(
        database,
        paths,
        config.model_copy(update={"application": application, "daily": daily}),
    )


def _recording(vault: Path, name: str, sample_video: Path, *, mtime: float) -> Path:
    vault.mkdir(parents=True, exist_ok=True)
    target = vault / name
    target.write_bytes(sample_video.read_bytes())
    import os

    os.utime(target, (mtime, mtime))
    return target


class TestOsloClock:
    """§2/§5/§9: Norway's clock, summer and winter, never raw UTC."""

    def test_production_is_due_at_two_oslo_not_two_utc(
        self, database, paths, config
    ) -> None:
        producer = _producer(database, paths, config)
        # Winter: 01:30 UTC is 02:30 Oslo (+01:00) -- due.
        winter = datetime(2026, 1, 15, 1, 30, tzinfo=timezone.utc).astimezone(OSLO)
        assert producer.production_due(winter)
        # Summer: 01:30 UTC is 03:30 Oslo (+02:00) -- due; but 23:30 UTC the
        # evening before is 01:30 Oslo -- not due.
        summer_early = datetime(2026, 7, 14, 23, 30, tzinfo=timezone.utc).astimezone(OSLO)
        assert not producer.production_due(summer_early)

    def test_the_publish_instant_follows_norwegian_dst(
        self, database, paths, config
    ) -> None:
        producer = _producer(database, paths, config)
        # 10:00 Oslo in January is 09:00 UTC; in July it is 08:00 UTC.
        assert producer.publish_instant_utc("2026-01-15") == datetime(
            2026, 1, 15, 9, 0, tzinfo=timezone.utc
        )
        assert producer.publish_instant_utc("2026-07-15") == datetime(
            2026, 7, 15, 8, 0, tzinfo=timezone.utc
        )


class TestLedger:
    """§1/§4: actually look, remember everything, reprocess nothing."""

    def test_discovery_registers_every_recording_once(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        _recording(vault, "a.mkv", sample_video, mtime=1_000)
        _recording(vault, "b.mp4", sample_video, mtime=2_000)
        (vault / "notes.txt").write_text("not a recording")
        producer = _producer(database, paths, config, vault=vault)

        found, fresh = producer.discover("2026-08-29")
        again_found, again_fresh = producer.discover("2026-08-30")

        assert (found, fresh) == (2, 2)
        assert (again_found, again_fresh) == (2, 0), "a known file is never re-registered"

    def test_a_pre_policy_import_is_seeded_done_not_reproduced(
        self, database, paths, config, sample_video, tmp_path, media_service, project_manager
    ) -> None:
        from backend.core.models.media import MediaImport
        from backend.core.models.project import ProjectCreate

        vault = tmp_path / "vault"
        clip = _recording(vault, "old.mkv", sample_video, mtime=1_000)
        project = project_manager.create(
            ProjectCreate(name="PrePolicy", target_duration_seconds=600)
        )
        media_service.import_media(project.id, MediaImport(path=str(clip)))
        producer = _producer(database, paths, config, vault=vault)

        producer.discover("2026-08-29")

        row = database.fetch_one(
            "SELECT state, note FROM production_ledger WHERE source_path = ?",
            (str(clip.resolve()),),
        )
        assert row["state"] == "edited"
        assert "pre-policy" in row["note"]
        assert producer.produce("2026-08-29") is None, "nothing eligible remains"


class TestDailyCaps:
    """§3/§8: one long video, two Reels, and the day stops there."""

    def test_one_recording_is_chosen_and_the_rest_wait(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        _recording(vault, "older.mkv", sample_video, mtime=1_000)
        newest = _recording(vault, "newest.mkv", sample_video, mtime=9_000)
        producer = _producer(database, paths, config, vault=vault)
        producer.discover("2026-08-29")

        chosen = producer.produce("2026-08-29")

        assert chosen == str(newest.resolve()), "newest first, per configuration"
        assert producer.produce("2026-08-29") is None, "the cap is one a day"
        states = {
            row["source_path"]: row["state"]
            for row in database.fetch_all("SELECT source_path, state FROM production_ledger", ())
        }
        assert states[str(newest.resolve())] == "processing"
        assert states[str((vault / "older.mkv").resolve())] == "new", "kept for tomorrow"

    def test_a_failed_day_frees_no_extra_slot(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        # The cap counts what was *attempted* against the day; only a row
        # marked failed releases nothing today -- a second produce on the
        # same day still refuses, because retrying is tomorrow's business.
        vault = tmp_path / "vault"
        clip = _recording(vault, "only.mkv", sample_video, mtime=1_000)
        producer = _producer(database, paths, config, vault=vault)
        producer.discover("2026-08-29")
        assert producer.produce("2026-08-29") == str(clip.resolve())

        long_videos, _ = producer.counts_for("2026-08-29")
        assert long_videos == 1


class TestSafeRetry:
    """§7: a failure keeps its files and gets tomorrow, three times at most."""

    def test_yesterdays_failure_is_eligible_again_today(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        clip = _recording(vault, "one.mkv", sample_video, mtime=1_000)
        producer = _producer(database, paths, config, vault=vault)
        producer.discover("2026-08-28")
        producer.produce("2026-08-28")
        producer._set_state(str(clip.resolve()), "failed", note="boom")

        producer.discover("2026-08-29")

        row = database.fetch_one("SELECT state, attempts FROM production_ledger", ())
        assert row["state"] == "new"

    def test_todays_failure_stays_failed_today(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        clip = _recording(vault, "one.mkv", sample_video, mtime=1_000)
        producer = _producer(database, paths, config, vault=vault)
        producer.discover("2026-08-29")
        producer.produce("2026-08-29")
        producer._set_state(str(clip.resolve()), "failed", note="boom")

        producer.discover("2026-08-29")

        assert (
            database.fetch_one("SELECT state FROM production_ledger", ())["state"]
            == "failed"
        )

    def test_three_strikes_and_the_file_rests(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        clip = _recording(vault, "one.mkv", sample_video, mtime=1_000)
        producer = _producer(database, paths, config, vault=vault)
        producer.discover("2026-08-28")
        producer._set_state(str(clip.resolve()), "failed", attempts=3, produced_day="2026-08-28")

        producer.discover("2026-08-29")

        assert (
            database.fetch_one("SELECT state FROM production_ledger", ())["state"]
            == "failed"
        )


class TestIdempotence:
    """§11: run it twice, crash it, restart it -- nothing happens twice."""

    def test_the_day_can_be_claimed_exactly_once(self, database, paths, config) -> None:
        producer = _producer(database, paths, config)
        assert producer.start_day("2026-08-29") is True
        assert producer.start_day("2026-08-29") is False

    def test_a_second_tick_on_the_same_day_produces_nothing_new(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        _recording(vault, "one.mkv", sample_video, mtime=1_000)
        _recording(vault, "two.mkv", sample_video, mtime=2_000)
        producer = _producer(database, paths, config, vault=vault)
        after_two = datetime(2026, 8, 29, 2, 5, tzinfo=OSLO)

        tick(producer, after_two)
        first = {
            row["source_path"]: row["state"]
            for row in database.fetch_all("SELECT source_path, state FROM production_ledger", ())
        }
        tick(producer, after_two.replace(minute=10))
        second = {
            row["source_path"]: row["state"]
            for row in database.fetch_all("SELECT source_path, state FROM production_ledger", ())
        }

        assert first == second
        assert sum(1 for state in first.values() if state == "processing") == 1

    def test_before_production_time_a_tick_claims_nothing(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        _recording(vault, "one.mkv", sample_video, mtime=1_000)
        producer = _producer(database, paths, config, vault=vault)

        tick(producer, datetime(2026, 8, 29, 1, 45, tzinfo=OSLO))

        assert not producer.day_started("2026-08-29")
        assert database.fetch_all("SELECT * FROM production_ledger", ()) == []


class TestScheduledPublication:
    """§5: ready at 10:00, on the platform's own clock, never before."""

    def test_qa_green_queues_a_publish_scheduled_for_ten_oslo(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        _recording(vault, "one.mkv", sample_video, mtime=1_000)
        producer = _producer(database, paths, config, vault=vault)
        day = "2026-08-29"
        producer.discover(day)
        producer.produce(day)
        row = database.fetch_one("SELECT project_id FROM production_ledger", ())
        project_id = row["project_id"]
        database.execute(
            "INSERT INTO analysis_jobs (id, project_id, stage, status, attempt, "
            "max_attempts, created_at, result) VALUES (?, ?, 'qa', 'completed', 1, 3, ?, ?)",
            ("job-qa-x", project_id, "2026-08-29T00:30:00Z", json.dumps({"quality_score": 88})),
        )

        producer.advance(datetime(2026, 8, 29, 3, 0, tzinfo=OSLO))  # -> edited
        producer.advance(datetime(2026, 8, 29, 3, 1, tzinfo=OSLO))  # -> ready

        ledger = database.fetch_one("SELECT * FROM production_ledger", ())
        assert ledger["state"] == "ready"
        publish = database.fetch_one(
            "SELECT payload, status FROM analysis_jobs WHERE project_id = ? AND stage = 'publish'",
            (project_id,),
        )
        payload = json.loads(publish["payload"])
        assert payload["publish_at_utc"] == "2026-08-29T08:00:00+00:00"  # 10:00 CEST
        assert publish["status"] == "queued"

    def test_below_the_floor_the_day_holds_and_says_why(
        self, database, paths, config, sample_video, tmp_path
    ) -> None:
        vault = tmp_path / "vault"
        _recording(vault, "one.mkv", sample_video, mtime=1_000)
        producer = _producer(database, paths, config, vault=vault)
        day = "2026-08-29"
        producer.discover(day)
        producer.produce(day)
        project_id = database.fetch_one("SELECT project_id FROM production_ledger", ())[
            "project_id"
        ]
        database.execute(
            "INSERT INTO analysis_jobs (id, project_id, stage, status, attempt, "
            "max_attempts, created_at, result) VALUES (?, ?, 'qa', 'completed', 1, 3, ?, ?)",
            ("job-qa-y", project_id, "2026-08-29T00:30:00Z", json.dumps({"quality_score": 30})),
        )

        producer.advance(datetime(2026, 8, 29, 3, 0, tzinfo=OSLO))
        producer.advance(datetime(2026, 8, 29, 3, 1, tzinfo=OSLO))

        ledger = database.fetch_one("SELECT * FROM production_ledger", ())
        assert ledger["state"] == "edited"
        assert "under the floor" in ledger["note"]
        assert (
            database.fetch_one(
                "SELECT id FROM analysis_jobs WHERE project_id = ? AND stage = 'publish'",
                (project_id,),
            )
            is None
        )

    def test_the_upload_body_carries_publish_at_and_goes_private(self) -> None:
        from backend.core.models.publishing import VideoMetadata
        from backend.publishing.youtube import _snippet as _upload_body

        metadata = VideoMetadata(
            title="t",
            visibility="public",
            publish_at=datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc),
        )

        body = _upload_body(metadata)

        assert body["status"]["publishAt"] == "2026-08-29T08:00:00Z"
        assert body["status"]["privacyStatus"] == "private", (
            "YouTube's contract: scheduled means private until the platform flips it"
        )

    def test_without_a_schedule_the_body_is_unchanged(self) -> None:
        from backend.core.models.publishing import VideoMetadata
        from backend.publishing.youtube import _snippet as _upload_body

        body = _upload_body(VideoMetadata(title="t", visibility="public"))

        assert body["status"]["privacyStatus"] == "public"
        assert "publishAt" not in body["status"]


class TestDailyReport:
    """§12: the day accounts for itself, including for doing nothing."""

    def test_an_empty_day_reports_the_reason(
        self, database, paths, config, tmp_path
    ) -> None:
        producer = _producer(database, paths, config, vault=tmp_path / "empty")
        (tmp_path / "empty").mkdir()

        tick(producer, datetime(2026, 8, 29, 2, 5, tzinfo=OSLO))

        row = database.fetch_one("SELECT report FROM daily_runs WHERE day = ?", ("2026-08-29",))
        report = json.loads(row["report"])
        assert report["produced_long"] == 0
        assert any("no eligible recordings" in reason for reason in report["reasons"])
        assert any(
            "no output directory" in reason for reason in report["reasons"]
        ), "the unset output folder is said out loud, never assumed"
