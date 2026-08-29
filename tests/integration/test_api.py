"""API integration tests (SPEC sections 89, 98 acceptance).

Phase 1's acceptance criterion is: **create project -> import video -> persist
metadata**. ``TestPhase1Acceptance`` at the end of this module is that test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def create_project(client, **overrides) -> dict:
    payload = {
        "name": "Ranked session",
        "target_duration_seconds": 1200,
        "mode": "story",
        "game": "valorant",
        "resolution": 1080,
        "fps": 60,
    }
    payload.update(overrides)
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestIdentity:
    """The launcher's takeover check (§57).

    Restarting its own server on every start is what "start the app" means to
    someone who just closed the tab; stopping whatever happens to hold port
    8765 is a different and much worse thing to do on their machine. This
    endpoint is what tells the two apart, and it has to be cheap enough to
    answer while the application is still warming up.
    """

    def test_it_says_who_is_listening(self, api_client) -> None:
        body = api_client.get("/api/identity").json()

        assert body["application"] == "vai"
        assert body["version"]

    def test_it_probes_nothing(self, api_client, monkeypatch) -> None:
        # The bug: identity was read from /health, which probes FFmpeg, the
        # GPU and Ollama. Cold that takes twenty seconds against a three-second
        # timeout, so the launcher refused to restart the server it had just
        # started. Identity must not depend on anything that can be slow.
        import backend.services.health as health_module

        def explode(*args, **kwargs):
            raise AssertionError("identity ran an environment probe")

        for name in dir(health_module.HealthService):
            if name.startswith("check_"):
                monkeypatch.setattr(health_module.HealthService, name, explode)

        assert api_client.get("/api/identity").status_code == 200


class TestSystemEndpoints:
    def test_health_reports_every_check(self, api_client) -> None:
        response = api_client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        names = {check["name"] for check in body["checks"]}
        assert {"python", "ffmpeg", "ffprobe", "sqlite", "gpu", "nvenc", "scenes"} <= names

    def test_missing_gpu_does_not_fail_the_report(self, api_client) -> None:
        # §52/§95: no GPU means CPU fallback, not a broken installation.
        #
        # The assertion is about the GPU check specifically, not about the
        # overall status: on a machine without FFmpeg the report is legitimately
        # "failed", and asserting otherwise would turn an honest environment
        # report into a test failure.
        body = api_client.get("/api/health").json()
        gpu = next(check for check in body["checks"] if check["name"] == "gpu")
        assert gpu["required"] is False
        blocking = {
            check["name"]
            for check in body["checks"]
            if check["required"] and check["status"] == "failed"
        }
        assert "gpu" not in blocking
        assert "nvenc" not in blocking

    def test_capabilities_expose_the_import_options(self, api_client) -> None:
        body = api_client.get("/api/capabilities").json()
        assert body["min_duration_seconds"] == 600
        assert body["max_duration_seconds"] == 3600
        assert set(body["modes"]) == {"story", "best_moments", "compilation"}
        assert body["resolutions"] == [720, 1080]

    def test_capabilities_list_only_implemented_publish_targets(self, api_client) -> None:
        body = api_client.get("/api/capabilities").json()
        assert body["publish_targets"] == ["local_file"]


class TestProjectEndpoints:
    def test_create_and_read(self, api_client) -> None:
        created = create_project(api_client)
        fetched = api_client.get(f"/api/projects/{created['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Ranked session"

    @pytest.mark.parametrize("seconds", [599, 3601, 0, -100])
    def test_rejects_out_of_band_duration(self, api_client, seconds: int) -> None:
        response = api_client.post(
            "/api/projects", json={"name": "x", "target_duration_seconds": seconds}
        )
        assert response.status_code == 422

    def test_rejects_a_blank_name(self, api_client) -> None:
        response = api_client.post(
            "/api/projects", json={"name": "   ", "target_duration_seconds": 1200}
        )
        assert response.status_code == 422

    def test_rejects_unknown_fields(self, api_client) -> None:
        response = api_client.post(
            "/api/projects",
            json={"name": "x", "target_duration_seconds": 1200, "surprise": 1},
        )
        assert response.status_code == 422

    def test_missing_project_returns_typed_404(self, api_client) -> None:
        response = api_client.get("/api/projects/proj-000000000000")
        assert response.status_code == 404
        assert response.json()["error_code"] == "PROJECT_NOT_FOUND"

    def test_list_and_count(self, api_client) -> None:
        create_project(api_client, name="First")
        create_project(api_client, name="Second")
        body = api_client.get("/api/projects").json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_update_target_duration(self, api_client) -> None:
        project = create_project(api_client)
        response = api_client.patch(
            f"/api/projects/{project['id']}", json={"target_duration_seconds": 900}
        )
        assert response.status_code == 200
        assert response.json()["target_duration_seconds"] == 900
        assert response.json()["version"] == project["version"] + 1

    def test_update_rejects_out_of_band_duration(self, api_client) -> None:
        project = create_project(api_client)
        response = api_client.patch(
            f"/api/projects/{project['id']}", json={"target_duration_seconds": 60}
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "INVALID_TARGET_DURATION"

    def test_delete(self, api_client) -> None:
        project = create_project(api_client)
        assert api_client.delete(f"/api/projects/{project['id']}").status_code == 204
        assert api_client.get(f"/api/projects/{project['id']}").status_code == 404

    def test_status_lists_every_stage(self, api_client) -> None:
        project = create_project(api_client)
        body = api_client.get(f"/api/projects/{project['id']}/status").json()
        stages = [item["stage"] for item in body["stages"]]
        assert stages[0] == "import"
        assert "publish" in stages
        assert body["project"]["id"] == project["id"]


class TestMediaEndpoints:
    def test_import_and_list(self, api_client, sample_video: Path) -> None:
        project = create_project(api_client)
        response = api_client.post(
            f"/api/projects/{project['id']}/media", json={"path": str(sample_video)}
        )
        assert response.status_code == 201, response.text
        media = response.json()
        assert media["state"] == "registered"
        assert media["checksum"]

        listed = api_client.get(f"/api/projects/{project['id']}/media").json()
        assert listed["total"] == 1

    def test_import_queues_the_pipeline(self, api_client, sample_video: Path) -> None:
        project = create_project(api_client)
        api_client.post(f"/api/projects/{project['id']}/media", json={"path": str(sample_video)})
        body = api_client.get(f"/api/projects/{project['id']}/jobs").json()
        assert body["total"] > 0
        assert {job["stage"] for job in body["items"]} >= {"import", "probe", "transcript"}

    def test_duplicate_import_returns_409(self, api_client, sample_video: Path) -> None:
        project = create_project(api_client)
        payload = {"path": str(sample_video)}
        api_client.post(f"/api/projects/{project['id']}/media", json=payload)
        response = api_client.post(f"/api/projects/{project['id']}/media", json=payload)
        assert response.status_code == 409
        assert response.json()["error_code"] == "MEDIA_ALREADY_IMPORTED"

    def test_missing_file_returns_404(self, api_client, tmp_path: Path) -> None:
        project = create_project(api_client)
        absent = tmp_path / "nowhere" / "clip.mp4"
        response = api_client.post(
            f"/api/projects/{project['id']}/media", json={"path": str(absent)}
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "MEDIA_NOT_FOUND"

    def test_unsupported_container_returns_422(self, api_client, tmp_path: Path) -> None:
        project = create_project(api_client)
        bad = tmp_path / "readme.txt"
        bad.write_text("nope", encoding="utf-8")
        response = api_client.post(f"/api/projects/{project['id']}/media", json={"path": str(bad)})
        assert response.status_code == 422
        assert response.json()["error_code"] == "UNSUPPORTED_CONTAINER"


class TestPipelineEndpoints:
    def test_analyze_queues_project_stages(self, api_client) -> None:
        project = create_project(api_client)
        body = api_client.post(f"/api/projects/{project['id']}/analyze").json()
        # The project-wide stages: those that reason across every file at once.
        # Per-media stages, up to and including MOMENTS, are queued at import.
        assert "story" in body["queued_stages"]
        assert "moments" not in body["queued_stages"]
        # Delivery is never queued automatically (§51).
        assert "publish" not in body["queued_stages"]

    def test_analyze_is_idempotent(self, api_client, sample_video: Path) -> None:
        project = create_project(api_client)
        api_client.post(f"/api/projects/{project['id']}/media", json={"path": str(sample_video)})
        api_client.post(f"/api/projects/{project['id']}/analyze")
        first = api_client.get(f"/api/projects/{project['id']}/jobs").json()["total"]
        api_client.post(f"/api/projects/{project['id']}/analyze")
        second = api_client.get(f"/api/projects/{project['id']}/jobs").json()["total"]
        assert first == second

    def test_cancel(self, api_client, sample_video: Path) -> None:
        project = create_project(api_client)
        api_client.post(f"/api/projects/{project['id']}/media", json={"path": str(sample_video)})
        body = api_client.post(f"/api/projects/{project['id']}/cancel").json()
        assert body["cancelled_jobs"] > 0

    def test_reanalyze_preserves_upstream_stages(self, api_client, sample_video: Path) -> None:
        project = create_project(api_client)
        api_client.post(f"/api/projects/{project['id']}/media", json={"path": str(sample_video)})
        api_client.post(f"/api/projects/{project['id']}/analyze")

        body = api_client.post(
            f"/api/projects/{project['id']}/reanalyze", params={"stage": "vision"}
        ).json()

        assert "vision" in body["invalidated_stages"]
        assert "game_events" in body["invalidated_stages"]
        assert "transcript" not in body["invalidated_stages"]


class TestPhase1Acceptance:
    """§98: create project -> import video -> persist metadata."""

    def test_full_phase_1_flow(self, api_client, app_state, sample_video: Path) -> None:
        # 1. Create the project.
        project = create_project(api_client, name="Phase 1 acceptance")
        project_id = project["id"]

        # 2. Import a recording.
        media = api_client.post(
            f"/api/projects/{project_id}/media", json={"path": str(sample_video)}
        ).json()

        # 3. Metadata is persisted and survives a fresh read from the database.
        stored_project = app_state.projects.get(project_id)
        assert stored_project.name == "Phase 1 acceptance"
        assert stored_project.target_duration_seconds == 1200

        stored_media = app_state.media.get_media(media["id"])
        assert stored_media.checksum == media["checksum"]
        assert stored_media.size_bytes == sample_video.stat().st_size

        # The §43 directory tree exists, with the manifest.
        paths = app_state.projects.paths_for(project_id)
        assert paths.manifest.is_file()
        for name in ("source", "analysis", "moments", "timeline", "renders"):
            assert (paths.root / name).is_dir()

        # The manifest carries version provenance (§49).
        manifest = app_state.projects.read_manifest(project_id)
        assert manifest.project.analysis_version >= 1
        assert manifest.project.application_version

        # The pipeline is queued and starts at IMPORT.
        status = api_client.get(f"/api/projects/{project_id}/status").json()
        by_stage = {item["stage"]: item for item in status["stages"]}
        assert by_stage["import"]["status"] == "queued"


class TestServingTheInterface:
    """One command has to serve the whole application (§57).

    Without this the finished product needs a Python process and a Node
    process side by side, which is fine for a developer and not fine for
    someone who wants to edit a video.
    """

    def test_the_root_serves_the_interface_when_it_is_built(self, api_client) -> None:
        from backend.api.app import INTERFACE_DIR

        response = api_client.get("/")

        if (INTERFACE_DIR / "index.html").is_file():
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
        else:
            # Not built is not an error: the API is usable on its own, and in
            # development the interface is Vite's dev server on another port.
            assert response.status_code == 404

    def test_the_api_still_answers_as_json(self, api_client) -> None:
        response = api_client.get("/api/health")

        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]

    def test_an_unknown_path_is_not_swallowed_by_the_interface(self, api_client) -> None:
        # The obvious single-page mount is a catch-all returning the shell for
        # anything unmatched. It was tried, and it turned a path-traversal
        # refusal into a 200: nothing leaked, but a refusal reporting success
        # is how a real leak stays unnoticed.
        for path in ("/no-such-screen", "/api/no-such-endpoint", "/Windows/win.ini"):
            assert api_client.get(path).status_code == 404, path


class TestTheFilePicker:
    """The native dialog, without ever opening one (§50, §95).

    A suite that pops file dialogs cannot run unattended, so the subprocess
    helper is substituted and what is tested is everything around it: the
    contract, the cancel path, the one-at-a-time rule, and the degradation on
    a machine that cannot show a dialog at all.
    """

    def test_a_chosen_path_is_returned(self, api_client, monkeypatch) -> None:
        from backend.api.routers import system

        monkeypatch.setattr(system, "_pick_file", lambda initial: r"D:\Gaming\clip.mkv")

        body = api_client.post("/api/system/pick-file", json={}).json()

        assert body["path"] == r"D:\Gaming\clip.mkv"

    def test_cancelling_is_an_answer_not_an_error(self, api_client, monkeypatch) -> None:
        from backend.api.routers import system

        monkeypatch.setattr(system, "_pick_file", lambda initial: None)

        response = api_client.post("/api/system/pick-file", json={})

        assert response.status_code == 200
        assert response.json()["path"] is None

    def test_the_initial_directory_reaches_the_dialog(self, api_client, monkeypatch) -> None:
        from backend.api.routers import system

        seen: list[str | None] = []
        monkeypatch.setattr(system, "_pick_file", lambda initial: seen.append(initial))
        api_client.post("/api/system/pick-file", json={"initial_dir": "D:/Gaming 2026"})

        assert seen == ["D:/Gaming 2026"]

    def test_the_picker_home_is_the_exclusive_source(self, tmp_path) -> None:
        # Pure arithmetic of where the dialog opens under the owner's rule.
        from backend.api.routers.system import _picker_home

        vault = tmp_path / "vault"
        vault.mkdir()
        inside = vault / "sessions"
        inside.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        roots = [str(vault)]

        assert _picker_home(str(inside), roots) == str(inside)
        assert _picker_home(str(outside), roots) == str(vault)
        assert _picker_home(None, roots) == str(vault)
        assert _picker_home(str(outside), []) == str(outside)
        assert _picker_home(None, [str(tmp_path / "missing")]) is None

    def test_only_one_dialog_at_a_time(self, api_client, monkeypatch) -> None:
        from backend.api.routers import system

        monkeypatch.setattr(system, "_pick_file", lambda initial: "x")
        assert system._dialog_lock.acquire(blocking=False)
        try:
            response = api_client.post("/api/system/pick-file", json={})
        finally:
            system._dialog_lock.release()

        assert response.status_code == 501
        assert response.json()["error_code"] == "FILE_PICKER_UNAVAILABLE"

    def test_a_machine_without_a_dialog_degrades_with_a_typed_code(
        self, api_client, monkeypatch
    ) -> None:
        from backend.api.routers import system
        from backend.core.errors import ErrorCode, GamingEditorError

        def headless(initial):
            raise GamingEditorError(
                "no display", code=ErrorCode.FILE_PICKER_UNAVAILABLE, recoverable=True
            )

        monkeypatch.setattr(system, "_pick_file", headless)

        response = api_client.post("/api/system/pick-file", json={})

        assert response.status_code == 501
        assert response.json()["error_code"] == "FILE_PICKER_UNAVAILABLE"


class TestPublishingEndpoints:
    """The delivery surface (§50, §51): status, sign-in, and the explicit queue."""

    def test_targets_reports_local_ready_and_youtube_unconfigured(self, api_client) -> None:
        body = api_client.get("/api/publishing/targets").json()

        by_name = {item["target"]: item for item in body["targets"]}
        assert by_name["local_file"]["connected"] is True
        # No OAuth client in the test configuration: available must say so
        # rather than letting a publish fail minutes in.
        assert by_name["youtube"]["available"] is False
        assert by_name["youtube"]["connected"] is False

    def test_auth_start_without_a_client_is_a_clear_501(self, api_client) -> None:
        response = api_client.post("/api/publishing/youtube/auth/start")

        assert response.status_code == 501
        assert "client" in response.json()["message"].lower()

    def test_auth_poll_without_a_flow_says_none(self, api_client) -> None:
        assert api_client.post("/api/publishing/youtube/auth/poll").json()["status"] == "none"

    def test_publish_queues_a_job_carrying_the_instruction(self, api_client) -> None:
        project = api_client.post(
            "/api/projects",
            json={"name": "Deliver", "target_duration_seconds": 900, "mode": "story"},
        ).json()

        response = api_client.post(
            f"/api/projects/{project['id']}/publish",
            json={"target": "local_file", "metadata": {"title": "My video"}},
        )

        assert response.status_code == 200
        job_id = response.json()["job_id"]
        jobs = api_client.get(f"/api/projects/{project['id']}/jobs").json()["items"]
        row = next(item for item in jobs if item["id"] == job_id)
        assert row["stage"] == "publish"
        assert row["status"] == "queued"

    def test_a_second_publish_replaces_the_instruction(self, api_client) -> None:
        project = api_client.post(
            "/api/projects",
            json={"name": "Deliver twice", "target_duration_seconds": 900, "mode": "story"},
        ).json()
        first = api_client.post(
            f"/api/projects/{project['id']}/publish",
            json={"target": "local_file", "metadata": {"title": "Old title"}},
        ).json()

        second = api_client.post(
            f"/api/projects/{project['id']}/publish",
            json={"target": "local_file", "metadata": {"title": "New title"}},
        ).json()

        # Same job row, new payload: publishing twice with yesterday's title
        # is nobody's intent.
        assert second["job_id"] == first["job_id"]
