"""QA finds the game profile its caption-over-HUD check needs.

GAME_EVENTS runs once per recording, so every one of its jobs carries a
media_id. QA looked the job up *without* one, never found it, and so judged
captions against no profile at all -- "no HUD regions declared", a PASS for a
check that had never run.

Acceptance (item 1):
* with a game profile on record for the media, the HUD overlap check runs;
* a missing profile, or recordings that disagree on one, is NOT_MEASURED;
* the generic profile, which declares no regions, still legitimately passes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config.loader import load_config
from backend.core.ids import new_id
from backend.core.models.enums import JobStage, JobStatus, QAStatus, VideoMode
from backend.core.models.media import Media
from backend.core.models.project import ProjectCreate
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.pipeline.workers.qa_worker import QaWorker
from backend.qa import content
from backend.timeline.captions import Caption
from backend.timeline.models import Timeline

pytestmark = pytest.mark.unit

PROFILES = Path(__file__).resolve().parents[2] / "profiles"


@pytest.fixture
def project_id(project_manager) -> str:
    return project_manager.create(
        ProjectCreate(
            name="Session",
            target_duration_seconds=1200,
            mode=VideoMode.STORY,
            game="gta_v",
        )
    ).id


def _media(database, project_id: str, filename: str) -> str:
    now = datetime.now(timezone.utc)
    return (
        MediaRepository(database)
        .create(
            Media(
                id=new_id("media"),
                project_id=project_id,
                source_path=f"/recordings/{filename}",
                filename=filename,
                container=".mp4",
                size_bytes=1024,
                checksum=sha256(filename.encode()).hexdigest(),
                created_at=now,
                updated_at=now,
            )
        )
        .id
    )


@pytest.fixture
def game_events(database, job_manager, project_id):
    """Record one finished GAME_EVENTS job per recording, as the stage does."""

    def record(*profiles: str | None) -> None:
        jobs = JobRepository(database)
        for index, name in enumerate(profiles):
            media_id = _media(database, project_id, f"part_{index}.mp4")
            job = job_manager.queue(project_id, JobStage.GAME_EVENTS, media_id=media_id)
            result = {"events": 0} if name is None else {"events": 0, "game_profile": name}
            jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED, "result": result}))

    return record


@pytest.fixture
def resolve(database, project_id):
    def run():
        context = SimpleNamespace(database=database, project_id=project_id, profiles_dir=PROFILES)
        return QaWorker()._profile(context)

    return run


class TestTheLookup:
    def test_a_recordings_profile_is_found(self, game_events, resolve) -> None:
        game_events("gta_v")

        profile, unresolved = resolve()

        assert unresolved is None
        assert profile is not None and profile.id == "gta_v"
        assert profile.regions

    def test_recordings_that_agree_resolve_to_their_profile(self, game_events, resolve) -> None:
        game_events("gta_v", "gta_v")

        profile, unresolved = resolve()

        assert unresolved is None
        assert profile.id == "gta_v"

    def test_the_generic_profile_resolves_with_no_regions(self, game_events, resolve) -> None:
        game_events("generic")

        profile, unresolved = resolve()

        assert unresolved is None
        assert not profile.regions

    def test_no_game_events_at_all_is_unresolved(self, resolve) -> None:
        profile, unresolved = resolve()

        assert profile is None
        assert unresolved

    def test_a_recording_without_a_profile_is_unresolved(self, game_events, resolve) -> None:
        game_events("gta_v", None)

        profile, unresolved = resolve()

        assert profile is None
        assert unresolved

    def test_recordings_that_disagree_are_unresolved(self, game_events, resolve) -> None:
        game_events("gta_v", "generic")

        profile, unresolved = resolve()

        assert profile is None
        assert "generic" in unresolved and "gta_v" in unresolved


class TestTheCheck:
    CAPTION = Caption(
        id="cap-000000000000", index=0, timeline_start=1.0, timeline_end=2.0, text="no way"
    )

    def _check(self, **inputs):
        config = load_config()
        findings = content.inspect(
            Timeline(project_id="proj-aaaaaaaaaaaa"),
            [self.CAPTION],
            content.ContentInputs(**inputs),
            config=config.qa,
            caption_config=config.captions,
        )
        return next(item for item in findings if item.check == "caption_covers_hud")

    def test_an_unresolved_profile_is_not_measured(self) -> None:
        finding = self._check(profile_unresolved="no game-events result")

        assert finding.status is QAStatus.NOT_MEASURED
        assert finding.remedy

    def test_a_found_profile_runs_the_check(self, game_events, resolve) -> None:
        game_events("gta_v")
        profile, unresolved = resolve()

        finding = self._check(profile=profile, profile_unresolved=unresolved)

        # Whatever the verdict, it is the geometry's, not "nothing declared".
        assert finding.status in (QAStatus.PASSED, QAStatus.WARNING)
        assert "no HUD regions" not in finding.message

    def test_the_generic_profile_still_passes(self, game_events, resolve) -> None:
        game_events("generic")
        profile, unresolved = resolve()

        finding = self._check(profile=profile, profile_unresolved=unresolved)

        assert finding.status is QAStatus.PASSED
