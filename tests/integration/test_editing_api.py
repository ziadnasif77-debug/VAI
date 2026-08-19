"""Phase 12: the endpoints the editing screens read (SPEC §57–§62, §76, §80).

The interface is only as good as what it can ask for, so these tests are about
shapes rather than plumbing: does the moments endpoint return the *reasoning*
§61 and §80 need on screen, does a timeline operation come back with the whole
re-flowed timeline, does a refused edit say why.

Two of them are about safety rather than features. `preview` and the file
endpoint hand a browser bytes from disk, and a request is a string from the
network even when the network is loopback — so a path that points outside the
project must be refused, and a video must be seekable or the Preview screen
appears frozen on anything large.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.models.enums import JobStage, MomentType
from backend.core.models.jobs import ANALYSIS_STAGES, EDIT_STAGES
from backend.core.models.media import Media, MediaMetadata
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.timeline.builder import PlannedClip, build_timeline

pytestmark = pytest.mark.integration


@pytest.fixture
def project(api_client):
    response = api_client.post(
        "/api/projects",
        json={"name": "Editing", "target_duration_seconds": 600, "mode": "story"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def media(app_state, project) -> Media:
    """A media row the timeline can reference. No file is decoded here."""
    from datetime import datetime, timezone

    from backend.core.ids import new_id

    now = datetime.now(timezone.utc)
    return MediaRepository(app_state.database).create(
        Media(
            id=new_id("media"),
            project_id=project["id"],
            source_path="D:/recordings/session.mkv",
            filename="session.mkv",
            container=".mkv",
            size_bytes=4096,
            checksum="0" * 64,
            metadata=MediaMetadata(duration_seconds=1200.0, width=1920, height=1080, fps=60.0),
            created_at=now,
            updated_at=now,
        )
    )


@pytest.fixture
def timeline(app_state, project, media, config):
    """Five clips, stored the way the EDL stage would have stored them."""
    clips = [
        PlannedClip(
            media_id=media.id,
            source_start=index * 100.0,
            source_end=index * 100.0 + 30.0,
            moment_type=MomentType.EPIC,
            score=0.5 + index / 20,
            role="hook" if index == 0 else "body",
        )
        for index in range(5)
    ]
    built = build_timeline(
        clips,
        project_id=project["id"],
        policy=config.output.duration_policy(),
        media_durations={media.id: 1200.0},
    )
    TimelineRepository(app_state.database).replace(project["id"], built.timeline)
    return built.timeline


class TestMomentsScreen:
    """§61: moment, timestamp, type, score, confidence, duration, why."""

    def test_an_empty_project_returns_an_empty_list(self, api_client, project) -> None:
        response = api_client.get(f"/api/projects/{project['id']}/moments")

        assert response.status_code == 200
        assert response.json() == {
            "total": 0,
            "returned": 0,
            "by_type": {},
            "items": [],
        }

    def test_an_unknown_project_is_a_404(self, api_client) -> None:
        response = api_client.get("/api/projects/proj-000000000000/moments")

        assert response.status_code == 404

    def test_the_filters_are_the_ones_the_screen_offers(self, api_client, project) -> None:
        # §61's filter row: by type and by score.
        response = api_client.get(
            f"/api/projects/{project['id']}/moments",
            params={"type": "epic", "min_score": 0.5, "limit": 10},
        )

        assert response.status_code == 200

    def test_an_unknown_type_is_rejected_rather_than_ignored(self, api_client, project) -> None:
        response = api_client.get(
            f"/api/projects/{project['id']}/moments", params={"type": "amazing"}
        )

        assert response.status_code == 422


class TestTimelineScreen:
    """§62: remove, restore, move, split, trim."""

    def test_the_timeline_comes_back_with_its_clips(self, api_client, project, timeline) -> None:
        response = api_client.get(f"/api/projects/{project['id']}/timeline")

        assert response.status_code == 200
        body = response.json()
        assert len(body["clips"]) == 5
        assert body["duration_seconds"] == pytest.approx(150.0)
        assert body["valid"] is True

    def test_deleting_a_clip_returns_the_re_flowed_timeline(
        self, api_client, project, timeline
    ) -> None:
        # Not an acknowledgement: every clip after this one just moved, so a
        # screen that patched one row would be showing the wrong positions.
        clip = timeline.video_clips()[1]

        response = api_client.post(
            f"/api/projects/{project['id']}/timeline/operations",
            json={"action": "delete", "clip_id": clip.id},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["duration_seconds"] == pytest.approx(120.0)
        disabled = [item for item in body["clips"] if not item["enabled"]]
        assert len(disabled) == 1
        assert disabled[0]["id"] == clip.id

    def test_a_deleted_clip_is_still_there_to_restore(self, api_client, project, timeline) -> None:
        # §78: the user has the last word, so "remove" must be undoable.
        clip = timeline.video_clips()[1]
        url = f"/api/projects/{project['id']}/timeline/operations"

        api_client.post(url, json={"action": "delete", "clip_id": clip.id})
        response = api_client.post(url, json={"action": "restore", "clip_id": clip.id})

        assert response.status_code == 200
        assert response.json()["duration_seconds"] == pytest.approx(150.0)

    def test_moving_a_clip_reorders_without_changing_the_length(
        self, api_client, project, timeline
    ) -> None:
        clip = timeline.video_clips()[4]

        response = api_client.post(
            f"/api/projects/{project['id']}/timeline/operations",
            json={"action": "move", "clip_id": clip.id, "to_index": 0},
        )

        body = response.json()
        assert body["clips"][0]["id"] == clip.id
        assert body["duration_seconds"] == pytest.approx(150.0)

    def test_splitting_produces_two_clips(self, api_client, project, timeline) -> None:
        clip = timeline.video_clips()[2]
        middle = (clip.timeline_start + clip.timeline_end) / 2

        response = api_client.post(
            f"/api/projects/{project['id']}/timeline/operations",
            json={"action": "split", "clip_id": clip.id, "at_seconds": middle},
        )

        assert response.status_code == 200
        assert len(response.json()["clips"]) == 6

    def test_an_impossible_edit_says_why(self, api_client, project, timeline) -> None:
        # A refused edit is the user asking for something impossible, not a
        # server fault.
        clip = timeline.video_clips()[0]

        response = api_client.post(
            f"/api/projects/{project['id']}/timeline/operations",
            json={"action": "split", "clip_id": clip.id, "at_seconds": 0.05},
        )

        assert response.status_code == 422
        assert "minimum" in response.json()["detail"].lower()

    def test_an_operation_missing_its_argument_is_rejected(
        self, api_client, project, timeline
    ) -> None:
        clip = timeline.video_clips()[0]

        response = api_client.post(
            f"/api/projects/{project['id']}/timeline/operations",
            json={"action": "move", "clip_id": clip.id},
        )

        assert response.status_code == 422

    def test_an_unknown_clip_is_a_404(self, api_client, project, timeline) -> None:
        response = api_client.post(
            f"/api/projects/{project['id']}/timeline/operations",
            json={"action": "delete", "clip_id": "clip-000000000000"},
        )

        assert response.status_code == 404


class TestRenderAndQa:
    def test_generate_edit_queues_the_stages_that_need_no_re_analysis(
        self, api_client, project
    ) -> None:
        # §127: changing the edit re-runs STORY onward against stored moments.
        response = api_client.post(f"/api/projects/{project['id']}/generate-edit")

        assert response.status_code == 200
        # Stated against EDIT_STAGES rather than a written-out list: what the
        # rule actually says is "nothing that would re-read the recording",
        # and a hardcoded list makes adding an edit stage look like a
        # regression (CRITIQUE did, in exactly this test).
        assert set(response.json()["queued"]) <= {stage.value for stage in EDIT_STAGES}
        assert not set(response.json()["queued"]) & {stage.value for stage in ANALYSIS_STAGES}

    def test_render_queues_the_render_and_its_qa(self, api_client, project) -> None:
        response = api_client.post(f"/api/projects/{project['id']}/render")

        assert response.status_code == 200
        assert set(response.json()["queued"]) <= {"render", "qa"}

    def test_render_status_before_anything_has_run(self, api_client, project) -> None:
        response = api_client.get(f"/api/projects/{project['id']}/render-status")

        assert response.status_code == 200
        body = response.json()
        assert body["latest"] is None
        assert body["blocked_by_qa"] is False

    def test_qa_before_a_render_is_empty_rather_than_an_error(self, api_client, project) -> None:
        response = api_client.get(f"/api/projects/{project['id']}/qa")

        assert response.status_code == 200
        assert response.json()["findings"] == []

    def test_the_stages_it_queued_really_are_queued(self, api_client, project, app_state) -> None:
        api_client.post(f"/api/projects/{project['id']}/render")
        stages = {job.stage for job in app_state.jobs.list_jobs(project["id"])}

        assert JobStage.RENDER in stages


class TestEvents:
    def test_an_empty_project_returns_no_events(self, api_client, project) -> None:
        response = api_client.get(f"/api/projects/{project['id']}/events")

        assert response.status_code == 200
        assert response.json() == {"total": 0, "by_type": {}, "items": []}


class TestServingFiles:
    """§50: local-first is a security property once a browser is involved."""

    def test_preview_before_a_render_is_a_404(self, api_client, project) -> None:
        response = api_client.get(f"/api/projects/{project['id']}/preview")

        assert response.status_code == 404

    def test_a_path_pointing_outside_the_project_is_refused(self, api_client, project) -> None:
        # A request is a string from the network even when the network is
        # loopback.
        response = api_client.get(
            f"/api/projects/{project['id']}/files/renders/..%2f..%2f..%2f..%2fWindows%2fwin.ini"
        )

        assert response.status_code in (403, 404)

    def test_a_missing_file_inside_the_project_is_a_404(self, api_client, project) -> None:
        response = api_client.get(f"/api/projects/{project['id']}/files/renders/absent.mp4")

        assert response.status_code == 404

    def test_a_file_inside_the_project_is_served(self, api_client, project, app_state) -> None:
        target = Path(project["project_directory"]) / "renders" / "note.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("hello", encoding="utf-8")

        response = api_client.get(f"/api/projects/{project['id']}/files/renders/note.txt")

        assert response.status_code == 200
        assert response.content == b"hello"

    def test_a_range_request_gets_partial_content(self, api_client, project) -> None:
        # Without this a player downloads a whole render before it will scrub.
        target = Path(project["project_directory"]) / "renders" / "clip.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(range(256)))

        response = api_client.get(
            f"/api/projects/{project['id']}/files/renders/clip.bin",
            headers={"Range": "bytes=10-19"},
        )

        assert response.status_code == 206
        assert response.content == bytes(range(10, 20))
        assert response.headers["content-range"] == "bytes 10-19/256"

    def test_a_suffix_range_returns_the_tail(self, api_client, project) -> None:
        target = Path(project["project_directory"]) / "renders" / "tail.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(range(256)))

        response = api_client.get(
            f"/api/projects/{project['id']}/files/renders/tail.bin",
            headers={"Range": "bytes=-8"},
        )

        assert response.status_code == 206
        assert response.content == bytes(range(248, 256))

    def test_an_impossible_range_is_416_with_the_real_length(self, api_client, project) -> None:
        target = Path(project["project_directory"]) / "renders" / "short.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"12345")

        response = api_client.get(
            f"/api/projects/{project['id']}/files/renders/short.bin",
            headers={"Range": "bytes=900-999"},
        )

        assert response.status_code == 416
        assert response.headers["content-range"] == "bytes */5"

    def test_a_whole_file_advertises_that_it_accepts_ranges(self, api_client, project) -> None:
        target = Path(project["project_directory"]) / "renders" / "whole.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"0123456789")

        response = api_client.get(f"/api/projects/{project['id']}/files/renders/whole.bin")

        assert response.headers.get("accept-ranges") == "bytes"
