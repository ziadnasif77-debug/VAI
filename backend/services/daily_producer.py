"""The owner's daily production & publishing policy, as a running machine.

One long video and at most two Reels per Europe/Oslo day. Production fires at
02:00, publication lands at 10:00 -- scheduled on YouTube itself via
``publishAt``, so the video goes public at that instant and an app crash
after upload cannot publish early. Every decision reads the production
ledger first, which is what makes a restart, a crash or a double firing
produce nothing twice: the policy is idempotent by construction, not by
hope.

The scheduler thread mirrors :class:`~backend.services.worker.JobWorker`:
it owns its database connection and takes **one safe action per tick** --
discover, pick, import, then watch the pipeline the ordinary worker is
already running, then queue the scheduled publish, then verify. The heavy
lifting stays with the worker, so §83's one-job-at-a-time is never broken
from a second thread.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from backend.config.paths import Paths
from backend.config.schema import AppConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, JobStatus
from backend.database.connection import Database

logger = get_logger("services.daily", LogChannel.PIPELINE)

#: Recordings the scan considers. The same containers the importer accepts.
_SCAN_SUFFIXES: Final[frozenset[str]] = frozenset({".mkv", ".mp4", ".mov", ".avi"})
#: How often the scheduler wakes to look at the clock and the ledger.
TICK_SECONDS: Final[float] = 30.0

_NEW: Final = "new"
_PROCESSING: Final = "processing"
_EDITED: Final = "edited"
_READY: Final = "ready"
_PUBLISHED: Final = "published"
_FAILED: Final = "failed"


@dataclass
class DailyReport:
    """§12: what the day found, made, moved and shipped -- or why not."""

    day: str
    found: int = 0
    eligible: int = 0
    produced_long: int = 0
    produced_reels: int = 0
    ready: int = 0
    published: int = 0
    moved: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "found": self.found,
            "eligible": self.eligible,
            "produced_long": self.produced_long,
            "produced_reels": self.produced_reels,
            "ready": self.ready,
            "published": self.published,
            "moved": self.moved,
            "archived": self.archived,
            "errors": self.errors,
            "reasons": self.reasons,
        }


class DailyProducer:
    """The policy's state machine over the production ledger."""

    def __init__(self, database: Database, paths: Paths, config: AppConfig) -> None:
        self._db = database
        self._paths = paths
        self._config = config
        self._zone = ZoneInfo(config.daily.timezone)

    # -- clock ----------------------------------------------------------

    def now_local(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(self._zone)

    def today(self, now: datetime | None = None) -> str:
        return (now or self.now_local()).date().isoformat()

    def _at(self, day: str, hhmm: str) -> datetime:
        hour, minute = (int(part) for part in hhmm.split(":"))
        return datetime.combine(
            datetime.fromisoformat(day).date(), clock_time(hour, minute), tzinfo=self._zone
        )

    def production_due(self, now: datetime) -> bool:
        return now >= self._at(self.today(now), self._config.daily.production_time)

    def publish_instant_utc(self, day: str) -> datetime:
        """10:00 Europe/Oslo of ``day``, as the UTC instant YouTube wants."""
        return self._at(day, self._config.daily.publish_time).astimezone(timezone.utc)

    # -- ledger ---------------------------------------------------------

    def _row(self, source_path: str) -> Any:
        return self._db.fetch_one(
            "SELECT * FROM production_ledger WHERE source_path = ?", (source_path,)
        )

    def _set_state(self, source_path: str, state: str, **fields: Any) -> None:
        columns = ", ".join(f"{name} = ?" for name in fields)
        parameters: list[Any] = [state, *fields.values()]
        sql = "UPDATE production_ledger SET state = ?"
        if columns:
            sql += ", " + columns
        sql += ", updated_at = ? WHERE source_path = ?"
        parameters += [datetime.now(timezone.utc).isoformat(), source_path]
        self._db.execute(sql, parameters)

    def counts_for(self, day: str) -> tuple[int, int]:
        """(long videos, reels) already counted against ``day``'s caps."""
        rows = self._db.fetch_all(
            "SELECT state, reels_produced FROM production_ledger WHERE produced_day = ?",
            (day,),
        )
        long_videos = sum(1 for row in rows if row["state"] != _FAILED)
        reels = sum(int(row["reels_produced"] or 0) for row in rows)
        return long_videos, reels

    # -- discovery (§1) --------------------------------------------------

    def discover(self, day: str) -> tuple[int, int]:
        """Register every recording in the exclusive source; return (found, new).

        Files already known keep their state. Files already imported by a
        pre-policy project are seeded as done rather than re-produced --
        §4's no-reprocessing reaches backwards in time too.
        """
        # §7's safe retry: yesterday's failure gets another day, up to three
        # attempts, against a fresh daily cap. Today's failure stays failed --
        # retrying into the same day would be a second slot the cap forbids.
        self._db.execute(
            "UPDATE production_ledger SET state = ?, updated_at = ? "
            "WHERE state = ? AND attempts < 3 AND (produced_day IS NULL OR produced_day != ?)",
            (_NEW, datetime.now(timezone.utc).isoformat(), _FAILED, day),
        )
        roots = self._config.application.media_source_roots
        found = 0
        fresh = 0
        for root in roots:
            base = Path(root)
            if not base.is_dir():
                logger.warning("The exclusive source is missing", extra={"root": root})
                continue
            for item in sorted(base.rglob("*")):
                if not item.is_file() or item.suffix.lower() not in _SCAN_SUFFIXES:
                    continue
                if self._under_archive(item):
                    # The done-shelf is not an inbox: a recording resting in
                    # Ferdig must never be mistaken for new work.
                    continue
                found += 1
                key = str(item.resolve())
                if self._row(key) is not None:
                    continue
                stat = item.stat()
                state, note = self._pre_policy_state(item)
                self._db.execute(
                    "INSERT INTO production_ledger (source_path, signature, state, "
                    "discovered_day, note, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        f"{stat.st_size}:{int(stat.st_mtime)}",
                        state,
                        day,
                        note,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                if state == _NEW:
                    fresh += 1
        return found, fresh

    def _under_archive(self, item: Path) -> bool:
        directory = self._config.daily.archive_directory
        if not directory:
            return False
        archive = os.path.normcase(str(Path(directory).expanduser().resolve()))
        candidate = os.path.normcase(str(item.resolve()))
        return candidate == archive or candidate.startswith(archive + os.sep)

    def archive_published(self, report: DailyReport | None = None) -> None:
        """The owner's done-shelf: a published recording moves to Ferdig.

        Runs every tick and is idempotent: only rows whose file still sits
        outside the archive are touched, the folder is created on first
        use, and a failure leaves the file where it is with the reason on
        the row -- the next tick simply tries again. The ledger key and the
        media rows follow the file, so no later discovery, re-render or
        §47 resume ever loses it. Moving is not deleting (§7): the
        original survives, relocated.
        """
        directory = self._config.daily.archive_directory
        if not directory:
            return
        rows = self._db.fetch_all(
            "SELECT source_path FROM production_ledger WHERE state = ?", (_PUBLISHED,)
        )
        for row in rows:
            source = Path(row["source_path"])
            if self._under_archive(source):
                continue
            if not source.is_file():
                continue
            try:
                moved = self._move_to_archive(source, Path(directory))
            except Exception as error:
                logger.exception(
                    "Could not move a finished recording to the archive",
                    extra={"source": str(source)},
                )
                self._db.execute(
                    "UPDATE production_ledger SET note = ?, updated_at = ? "
                    "WHERE source_path = ?",
                    (
                        f"archive move failed: {str(error)[:200]}",
                        datetime.now(timezone.utc).isoformat(),
                        row["source_path"],
                    ),
                )
                continue
            if report is not None:
                report.archived.append(str(moved))

    def _move_to_archive(self, source: Path, directory: Path) -> Path:
        import shutil

        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / source.name
        if destination.exists():
            if destination.stat().st_size == source.stat().st_size:
                # Already there from an interrupted earlier move: finish the
                # bookkeeping, do not copy again.
                source.unlink()
            else:
                stem, suffix = source.stem, source.suffix
                counter = 2
                while destination.exists():
                    destination = directory / f"{stem} ({counter}){suffix}"
                    counter += 1
                shutil.move(str(source), str(destination))
        else:
            shutil.move(str(source), str(destination))
        if not destination.is_file():
            raise OSError(f"the move did not land: {destination}")

        old_key = str(source)
        new_key = str(destination.resolve())
        self._db.execute(
            "UPDATE production_ledger SET source_path = ?, note = ?, updated_at = ? "
            "WHERE source_path = ?",
            (
                new_key,
                f"archived from {old_key}",
                datetime.now(timezone.utc).isoformat(),
                old_key,
            ),
        )
        # The media rows follow the file, so a later re-render or resume
        # still finds its source.
        self._db.execute(
            "UPDATE media SET source_path = ? WHERE source_path IN (?, ?)",
            (new_key, old_key, str(source.resolve()) if source.exists() else old_key),
        )
        logger.info(
            "Finished recording archived",
            extra={"from": old_key, "to": new_key},
        )
        return destination

    def _pre_policy_state(self, item: Path) -> tuple[str, str | None]:
        row = self._db.fetch_one(
            "SELECT project_id FROM media WHERE source_path = ? OR source_path = ?",
            (str(item), str(item.resolve())),
        )
        if row is None:
            return _NEW, None
        published = self._db.fetch_one(
            "SELECT id FROM analysis_jobs WHERE project_id = ? AND stage = 'publish' "
            "AND status = 'completed'",
            (row["project_id"],),
        )
        if published is not None:
            return _PUBLISHED, "pre-policy: already published before the ledger existed"
        return _EDITED, "pre-policy: already imported before the ledger existed"

    # -- the cycle (§10) -------------------------------------------------

    def start_day(self, day: str) -> bool:
        """Claim the day. False means another firing already owns it (§11)."""
        try:
            self._db.execute(
                "INSERT INTO daily_runs (day, started_at) VALUES (?, ?)",
                (day, datetime.now(timezone.utc).isoformat()),
            )
            return True
        except Exception:
            return False

    def day_started(self, day: str) -> bool:
        return (
            self._db.fetch_one("SELECT day FROM daily_runs WHERE day = ?", (day,))
            is not None
        )

    def produce(self, day: str) -> str | None:
        """Steps 1-8: pick the one eligible recording and start its pipeline.

        Returns the chosen source path, or ``None`` with the reason left for
        the report. Everything heavy runs on the ordinary job worker; this
        only creates the project, imports the file and lets the queue fill.
        """
        long_videos, _reels = self.counts_for(day)
        daily = self._config.daily
        if long_videos >= daily.max_long_videos:
            logger.info(
                "Daily cap already met; nothing was started",
                extra={"day": day, "long_videos": long_videos},
            )
            return None
        candidates = self._db.fetch_all(
            "SELECT source_path, signature FROM production_ledger WHERE state = ?",
            (_NEW,),
        )
        existing = [row for row in candidates if Path(row["source_path"]).is_file()]
        if not existing:
            # The cheap check came first and found nothing, so nothing else
            # runs: no project, no import, no probe, and -- by §13's lazy
            # construction -- no model was ever built. The owner's rule:
            # confirm the video exists before starting anything at all.
            logger.info(
                "Daily check found no eligible recording; nothing was started",
                extra={"day": day},
            )
            return None
        newest_first = daily.selection == "newest"
        existing.sort(
            key=lambda row: Path(row["source_path"]).stat().st_mtime,
            reverse=newest_first,
        )
        chosen = existing[0]["source_path"]

        from backend.core.models.media import MediaImport
        from backend.core.models.project import ProjectCreate
        from backend.services.media_ingestion import MediaIngestionService
        from backend.services.project_manager import ProjectManager

        self._set_state(chosen, _PROCESSING, attempts=self._attempts(chosen) + 1)
        try:
            projects = ProjectManager(self._db, self._paths, self._config)
            stem = Path(chosen).stem
            project = projects.create(
                ProjectCreate(
                    name=f"Daily {day} — {stem}",
                    target_duration_seconds=600,
                    captions_enabled=False,
                    # The policy schedules its own publication at 10:00; the
                    # immediate auto path stays off for daily projects.
                    auto_publish=False,
                    output_directory=daily.output_directory,
                )
            )
            MediaIngestionService(self._db, self._paths, self._config).import_media(
                project.id, MediaImport(path=chosen)
            )
            self._set_state(chosen, _PROCESSING, project_id=project.id, produced_day=day)
            logger.info(
                "Daily production started",
                extra={"day": day, "source": chosen, "project_id": project.id},
            )
            return chosen
        except Exception as error:
            self._set_state(chosen, _FAILED, note=str(error)[:500])
            logger.exception("Daily production could not start", extra={"source": chosen})
            return None

    def _attempts(self, source_path: str) -> int:
        row = self._row(source_path)
        return int(row["attempts"] or 0) if row is not None else 0

    # -- watching the pipeline -------------------------------------------

    def advance(self, now: datetime) -> None:
        """One safe step for every in-flight ledger row."""
        rows = self._db.fetch_all(
            "SELECT * FROM production_ledger WHERE state IN (?, ?, ?)",
            (_PROCESSING, _EDITED, _READY),
        )
        for row in rows:
            try:
                self._advance_one(row, now)
            except Exception:
                logger.exception(
                    "Advancing a daily item failed; it keeps its state",
                    extra={"source": row["source_path"]},
                )

    def _stage_status(self, project_id: str, stage: JobStage) -> str | None:
        row = self._db.fetch_one(
            "SELECT status FROM analysis_jobs WHERE project_id = ? AND stage = ?",
            (project_id, stage.value),
        )
        return None if row is None else str(row["status"])

    def _advance_one(self, row: Any, now: datetime) -> None:
        source = row["source_path"]
        project_id = row["project_id"]
        if not project_id:
            return
        state = row["state"]

        if state == _PROCESSING:
            qa = self._stage_status(project_id, JobStage.QA)
            if qa == JobStatus.FAILED.value:
                self._set_state(source, _FAILED, note="the pipeline failed at QA")
                return
            failed = self._db.fetch_one(
                "SELECT stage FROM analysis_jobs WHERE project_id = ? AND status = ? "
                "AND attempt >= max_attempts",
                (project_id, JobStatus.FAILED.value),
            )
            if failed is not None:
                self._set_state(
                    source, _FAILED, note=f"the pipeline failed at {failed['stage']}"
                )
                return
            if qa == JobStatus.COMPLETED.value:
                reels = self._reels_count(project_id)
                self._set_state(source, _EDITED, reels_produced=reels)
            return

        if state == _EDITED:
            self._queue_scheduled_publish(row, now)
            return

        if state == _READY:
            self._confirm_published(row, now)
            return

    def _reels_count(self, project_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT result FROM analysis_jobs WHERE project_id = ? AND stage = 'shorts' "
            "AND status = 'completed'",
            (project_id,),
        )
        if row is None or not row["result"]:
            return 0
        try:
            return len(json.loads(row["result"]).get("shorts") or [])
        except (ValueError, AttributeError):
            return 0

    def _queue_scheduled_publish(self, row: Any, now: datetime) -> None:
        """§5: schedule the publication for 10:00 Oslo, on the platform."""
        source = row["source_path"]
        project_id = row["project_id"]
        qa_row = self._db.fetch_one(
            "SELECT result FROM analysis_jobs WHERE project_id = ? AND stage = 'qa'",
            (project_id,),
        )
        score = None
        if qa_row is not None and qa_row["result"]:
            score = json.loads(qa_row["result"]).get("quality_score")
        floor = self._config.publishing.auto_publish_minimum_score
        if isinstance(score, (int, float)) and score < floor:
            self._set_state(
                source,
                _EDITED,
                note=f"held: quality {score:.0f} is under the floor {floor}",
            )
            logger.warning(
                "Daily video held under the quality floor",
                extra={"project_id": project_id, "score": score, "floor": floor},
            )
            return

        day = row["produced_day"] or self.today(now)
        instant = self.publish_instant_utc(day)
        if instant <= now.astimezone(timezone.utc):
            # 10:00 already passed (a late produce, a long render): the next
            # same-clock instant tomorrow, never an immediate publish.
            instant = self.publish_instant_utc(
                (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat()
            )

        from backend.services.job_manager import JobManager

        jobs = JobManager(self._db, self._config)
        existing = self._db.fetch_one(
            "SELECT id, status FROM analysis_jobs WHERE project_id = ? AND stage = 'publish'",
            (project_id,),
        )
        payload = {
            "target": "youtube",
            "auto": True,
            "publish_at_utc": instant.isoformat(),
        }
        if existing is None:
            jobs.queue(project_id, JobStage.PUBLISH, payload=payload)
        elif existing["status"] in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
            pass  # already on its way; queueing again is how duplicates happen
        else:
            jobs.requeue(existing["id"], payload=payload)
        self._set_state(
            source, _READY, scheduled_publish_utc=instant.isoformat(), note=None
        )
        logger.info(
            "Daily publication scheduled",
            extra={"project_id": project_id, "publish_at_utc": instant.isoformat()},
        )

    def _confirm_published(self, row: Any, now: datetime) -> None:
        """After the appointed instant, believe the record, then say so."""
        publish = self._db.fetch_one(
            "SELECT status, result FROM analysis_jobs WHERE project_id = ? AND stage = 'publish'",
            (row["project_id"],),
        )
        if publish is None:
            return
        if publish["status"] == JobStatus.FAILED.value:
            self._set_state(
                row["source_path"], _FAILED, note="the upload failed; the files are kept"
            )
            return
        if publish["status"] != JobStatus.COMPLETED.value:
            return
        url = None
        if publish["result"]:
            url = json.loads(publish["result"]).get("external_url")
        scheduled = row["scheduled_publish_utc"]
        instant = datetime.fromisoformat(scheduled) if scheduled else None
        if instant is not None and now.astimezone(timezone.utc) < instant:
            return  # uploaded and scheduled; public only when the clock says
        self._set_state(row["source_path"], _PUBLISHED, video_url=url)
        logger.info(
            "Daily video is public", extra={"source": row["source_path"], "url": url}
        )

    # -- output move (§6) ------------------------------------------------

    def verify_delivery(self, report: DailyReport) -> None:
        """§6: the copy is only a copy once the file is really there."""
        directory = self._config.daily.output_directory
        if not directory:
            report.reasons.append(
                "no output directory is configured yet; the copy step waits for one"
            )
            return
        rows = self._db.fetch_all(
            "SELECT source_path, project_id FROM production_ledger "
            "WHERE produced_day = ? AND state IN (?, ?, ?)",
            (report.day, _EDITED, _READY, _PUBLISHED),
        )
        for row in rows:
            render = self._db.fetch_one(
                "SELECT result FROM analysis_jobs WHERE project_id = ? AND stage = 'render' "
                "AND status = 'completed'",
                (row["project_id"],),
            )
            if render is None or not render["result"]:
                continue
            delivered = json.loads(render["result"]).get("delivered_to")
            if delivered and Path(delivered).is_file():
                report.moved.append(delivered)
            else:
                report.errors.append(
                    f"the finished file was not found in the output directory "
                    f"({delivered or 'no delivery recorded'})"
                )

    # -- report (§12) -----------------------------------------------------

    def write_report(self, report: DailyReport) -> None:
        self._db.execute(
            "UPDATE daily_runs SET finished_at = ?, report = ? WHERE day = ?",
            (
                datetime.now(timezone.utc).isoformat(),
                json.dumps(report.as_dict(), ensure_ascii=False),
                report.day,
            ),
        )
        directory = self._paths.data_root / "reports" / "daily"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{report.day}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(path)
        logger.info("Daily report written", extra=report.as_dict())

    def snapshot_report(self, day: str) -> DailyReport:
        report = DailyReport(day=day)
        rows = self._db.fetch_all("SELECT * FROM production_ledger", ())
        report.found = len(rows)
        report.eligible = sum(1 for row in rows if row["state"] == _NEW)
        today = [row for row in rows if row["produced_day"] == day]
        report.produced_long = sum(1 for row in today if row["state"] != _FAILED)
        report.produced_reels = sum(int(row["reels_produced"] or 0) for row in today)
        report.ready = sum(1 for row in today if row["state"] == _READY)
        report.published = sum(1 for row in today if row["state"] == _PUBLISHED)
        for row in rows:
            if row["note"] and row["state"] in (_FAILED, _EDITED) and row["produced_day"] == day:
                report.reasons.append(f"{Path(row['source_path']).name}: {row['note']}")
            if row["state"] == _FAILED and row["produced_day"] == day:
                report.errors.append(f"{Path(row['source_path']).name}: {row['note'] or 'failed'}")
        if report.produced_long == 0:
            report.reasons.append(
                "nothing was produced today"
                + (": no eligible recordings" if report.eligible == 0 else "")
            )
        return report


class DailyScheduler:
    """The 02:00 clock, on a thread shaped exactly like the job worker."""

    def __init__(self, config: AppConfig, paths: Paths) -> None:
        self._config = config
        self._paths = paths
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._config.daily.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="vai-daily", daemon=True)
        self._thread.start()
        logger.info(
            "Daily scheduler started",
            extra={
                "timezone": self._config.daily.timezone,
                "production_time": self._config.daily.production_time,
                "publish_time": self._config.daily.publish_time,
            },
        )

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def run(self) -> None:
        database = Database(self._paths.database_path, self._config.application.database)
        producer = DailyProducer(database, self._paths, self._config)
        try:
            while not self._stop.is_set():
                try:
                    tick(producer)
                except Exception:
                    logger.exception("Daily tick failed; the next one will look again")
                self._stop.wait(TICK_SECONDS)
        finally:
            database.close()


def tick(producer: DailyProducer, now: datetime | None = None) -> None:
    """One heartbeat: claim the day when due, then advance whatever is live.

    Split from the thread so a test -- or a manual run -- drives it with its
    own clock. Every branch is safe to repeat; that is the §11 contract.
    """
    now = now or producer.now_local()
    day = producer.today(now)
    if (
        producer.production_due(now)
        and not producer.day_started(day)
        and producer.start_day(day)
    ):
            found, fresh = producer.discover(day)
            chosen = producer.produce(day)
            report = producer.snapshot_report(day)
            report.found = found
            report.eligible = fresh if chosen is None else fresh - 1
            producer.write_report(report)
    producer.advance(now)
    # The report is rewritten as the day progresses, so the last state of
    # every item -- ready, published, failed -- is what the file shows.
    if producer.day_started(day):
        report = producer.snapshot_report(day)
        producer.verify_delivery(report)
        producer.archive_published(report)
        producer.write_report(report)
    else:
        # The done-shelf sweep does not wait for a claimed day: a video
        # published yesterday whose move failed gets its retry on the very
        # next tick.
        producer.archive_published(None)


__all__ = ["TICK_SECONDS", "DailyProducer", "DailyReport", "DailyScheduler", "tick"]
