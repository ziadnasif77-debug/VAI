"""generate-edit re-judges the edit it rebuilds.

The endpoint requeued STORY, EDL, CRITIQUE and RENDER but not QA or CRITIC2,
and requeueing keeps a job's old result. So the publish gate, render-status's
``blocked_by_qa``, the quality score and the report all read the verdict on
the *previous* edit as if it were about the new one. CRITIC2's snapshot of
the previous edit survived too, so a requeued critic could restore the old
clips over the new edit.

Acceptance (item 2):
* after generate-edit, QA and CRITIC2 are requeued (EDIT_STAGES);
* their old results are cleared, and GET /qa says pending while QA waits;
* the project's critic2 snapshots are cleared;
* a stale verdict is never shown as current.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.core.models.enums import JobStage, JobStatus
from backend.core.models.jobs import EDIT_STAGES
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.qa import QaRepository
from backend.database.repositories.renders import RenderRepository
from backend.qa.report import failure

pytestmark = pytest.mark.integration

STALE = {"status": "failed", "blocks_export": True, "quality_score": 4.2, "gate": "block"}


@pytest.fixture
def judged(api_client, app_state):
    """A project whose previous edit was rendered, judged and critiqued."""
    response = api_client.post(
        "/api/projects",
        json={"name": "Judged", "target_duration_seconds": 600, "mode": "story"},
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    database = app_state.database

    jobs = JobRepository(database)
    for stage, result in ((JobStage.QA, STALE), (JobStage.CRITIC2, {"pass": 1})):
        job = jobs.find(project_id, stage) or app_state.jobs.queue(project_id, stage)
        jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED, "result": result}))

    render_id = RenderRepository(database).start(
        project_id, resolution=1080, fps=60, encoder="libx264"
    )
    QaRepository(database).replace_for_render(
        project_id, render_id, [failure("duration", "too short", remedy="re-render")]
    )
    database.execute(
        "INSERT INTO critic2_snapshots "
        "(id, project_id, revision, quality_before, clips, effects, created_at) "
        "VALUES (?, ?, 1, 4.2, '[]', '[]', ?)",
        ("snap-000000000001", project_id, datetime.now(timezone.utc).isoformat()),
    )
    return project_id


def _job(app_state, project_id, stage):
    return JobRepository(app_state.database).find(project_id, stage)


class TestGenerateEdit:
    def test_qa_and_critic2_are_requeued(self, api_client, app_state, judged) -> None:
        response = api_client.post(f"/api/projects/{judged}/generate-edit")

        assert response.status_code == 200, response.text
        assert {"qa", "critic2"} <= set(response.json()["queued"])
        for stage in EDIT_STAGES:
            assert _job(app_state, judged, stage).status is JobStatus.QUEUED, stage

    def test_their_old_results_are_cleared(self, api_client, app_state, judged) -> None:
        api_client.post(f"/api/projects/{judged}/generate-edit")

        assert not _job(app_state, judged, JobStage.QA).result
        assert not _job(app_state, judged, JobStage.CRITIC2).result

    def test_critic2_snapshots_are_cleared(self, api_client, app_state, judged) -> None:
        api_client.post(f"/api/projects/{judged}/generate-edit")

        row = app_state.database.fetch_one(
            "SELECT COUNT(*) AS n FROM critic2_snapshots WHERE project_id = ?", (judged,)
        )
        assert row["n"] == 0


class TestNoStaleVerdict:
    def test_qa_says_pending_while_it_waits(self, api_client, judged) -> None:
        api_client.post(f"/api/projects/{judged}/generate-edit")

        body = api_client.get(f"/api/projects/{judged}/qa").json()

        assert body["pending"] is True
        assert body["findings"] == []
        assert body["blocks_export"] is False
        assert body["gate"] != "pass"

    def test_render_status_does_not_repeat_the_old_verdict(self, api_client, judged) -> None:
        api_client.post(f"/api/projects/{judged}/generate-edit")

        body = api_client.get(f"/api/projects/{judged}/render-status").json()

        assert body["blocked_by_qa"] is False

    def test_a_finished_qa_is_not_pending(self, api_client, judged) -> None:
        body = api_client.get(f"/api/projects/{judged}/qa").json()

        assert body["pending"] is False
        assert [item["check"] for item in body["findings"]] == ["duration"]
        assert body["blocks_export"] is True
