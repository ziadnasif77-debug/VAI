"""Interaction API integration tests.

Covers the §27 definition of done that is reachable in this phase: write an
instruction, ask a question, refuse to guess before analysis, and confirm the
system still works end to end for a user who never opens the chat.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def create_project(client, **overrides) -> dict:
    payload = {
        "name": "Ranked session",
        "target_duration_seconds": 1200,
        "mode": "best_moments",
        "game": "valorant",
    }
    payload.update(overrides)
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestPreferences:
    """Phase F, over the API: a preference nobody can see is a bug."""

    def test_a_fresh_install_has_learned_nothing_and_says_so(self, api_client) -> None:
        project = create_project(api_client)
        response = api_client.get(f"/api/projects/{project['id']}/preferences")

        assert response.status_code == 200
        body = response.json()
        assert body["learned"] == []
        # "Nothing was learned" and "there was nothing to learn from" are
        # different statements, and only one of them is worth showing.
        assert "considered" in body

    def test_a_repeated_request_appears_with_the_words_that_produced_it(self, api_client) -> None:
        for index in range(3):
            other = create_project(api_client, name=f"session {index}")
            api_client.post(f"/api/projects/{other['id']}/chat", json={"text": "make it slower"})

        project = create_project(api_client, name="the next one")
        body = api_client.get(f"/api/projects/{project['id']}/preferences").json()

        learned = {item["dimension"]: item for item in body["learned"]}
        assert "pacing" in learned, body
        assert learned["pacing"]["projects"] == 3
        # The person's own words, not the editor's summary of them.
        assert "make it slower" in learned["pacing"]["examples"]


class TestPresets:
    def test_lists_every_preset(self, api_client) -> None:
        body = api_client.get("/api/presets").json()
        names = {item["name"] for item in body["items"]}
        assert {
            "best_moments",
            "fast_gaming",
            "cinematic",
            "story_driven",
            "funny_gaming",
            "competitive",
            "minimal_effects",
        } <= names
        assert body["default"] in names

    def test_select_a_preset(self, api_client) -> None:
        project = create_project(api_client)
        response = api_client.post(
            f"/api/projects/{project['id']}/intent/preset", json={"preset": "cinematic"}
        )
        assert response.status_code == 200
        assert response.json()["preset"] == "cinematic"

    def test_unknown_preset_is_rejected(self, api_client) -> None:
        project = create_project(api_client)
        response = api_client.post(
            f"/api/projects/{project['id']}/intent/preset", json={"preset": "nope"}
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "BUSINESS_VALIDATION_FAILED"


class TestIntent:
    def test_default_intent_needs_no_instruction(self, api_client) -> None:
        # §1: the system works without the user writing anything.
        project = create_project(api_client)
        body = api_client.get(f"/api/projects/{project['id']}/intent").json()
        assert body["target_duration_seconds"] == 1200
        assert body["preset"]

    def test_instruction_updates_the_intent(self, api_client) -> None:
        project = create_project(api_client)
        response = api_client.post(
            f"/api/projects/{project['id']}/chat",
            json={"text": "ركز على الـclutch واحذف الممل"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["interaction_type"] == "editing_instruction"
        assert "clutch" in body["intent"]["priority_moment_types"]
        assert body["intent"]["dead_time_policy"] == "aggressive"

    def test_follow_up_refines_rather_than_replaces(self, api_client) -> None:
        project = create_project(api_client)
        api_client.post(f"/api/projects/{project['id']}/chat", json={"text": "ركز على الـclutch"})
        api_client.post(
            f"/api/projects/{project['id']}/chat", json={"text": "ولا تستخدم كثير effects"}
        )
        body = api_client.post(
            f"/api/projects/{project['id']}/chat", json={"text": "اجعله 25 دقيقة"}
        ).json()

        intent = body["intent"]
        assert "clutch" in intent["priority_moment_types"]
        assert intent["effects"] != "heavy"
        assert intent["target_duration_seconds"] == 1500

    def test_out_of_band_duration_is_refused(self, api_client) -> None:
        project = create_project(api_client)
        response = api_client.post(
            f"/api/projects/{project['id']}/chat", json={"text": "make it 5 minutes"}
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "INVALID_TARGET_DURATION"

    def test_reset_returns_to_the_preset(self, api_client) -> None:
        project = create_project(api_client)
        api_client.post(f"/api/projects/{project['id']}/chat", json={"text": "make it cinematic"})
        body = api_client.post(f"/api/projects/{project['id']}/intent/reset").json()
        assert body["style"] != "cinematic"


class TestQuestions:
    def test_refuses_before_analysis(self, api_client) -> None:
        # §17: no invented answers.
        project = create_project(api_client)
        body = api_client.post(
            f"/api/projects/{project['id']}/ask", json={"question": "ما أفضل لحظة؟"}
        ).json()
        assert body["requires_analysis"] is True
        assert body["confidence"] == 0.0

    def test_duration_question_works_without_analysis(self, api_client) -> None:
        project = create_project(api_client)
        body = api_client.post(
            f"/api/projects/{project['id']}/ask", json={"question": "how long is the video?"}
        ).json()
        assert body["requires_analysis"] is False
        assert body["evidence"]

    def test_chat_routes_a_question(self, api_client) -> None:
        project = create_project(api_client)
        body = api_client.post(
            f"/api/projects/{project['id']}/chat", json={"text": "ما أفضل لحظة؟"}
        ).json()
        assert body["interaction_type"] == "question"
        assert body["answer"] is not None


class TestConversation:
    def test_history_is_recorded(self, api_client) -> None:
        project = create_project(api_client)
        api_client.post(f"/api/projects/{project['id']}/chat", json={"text": "focus on clutches"})
        api_client.post(
            f"/api/projects/{project['id']}/chat", json={"text": "how long is the video?"}
        )
        body = api_client.get(f"/api/projects/{project['id']}/chat/history").json()
        assert body["total"] == 4
        assert [item["role"] for item in body["items"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]

    def test_history_is_per_project(self, api_client) -> None:
        first = create_project(api_client, name="First")
        second = create_project(api_client, name="Second")
        api_client.post(f"/api/projects/{first['id']}/chat", json={"text": "focus on clutches"})
        body = api_client.get(f"/api/projects/{second['id']}/chat/history").json()
        assert body["total"] == 0

    def test_empty_message_is_rejected(self, api_client) -> None:
        project = create_project(api_client)
        response = api_client.post(f"/api/projects/{project['id']}/chat", json={"text": "   "})
        assert response.status_code == 422


class TestChatIsOptional:
    """§22, §27: the editor works fully without the chat."""

    def test_project_reaches_a_queued_pipeline_without_any_instruction(
        self, api_client, sample_video
    ) -> None:
        project = create_project(api_client)
        api_client.post(f"/api/projects/{project['id']}/media", json={"path": str(sample_video)})
        api_client.post(f"/api/projects/{project['id']}/analyze")

        status = api_client.get(f"/api/projects/{project['id']}/status").json()
        queued = [item["stage"] for item in status["stages"] if item["status"] == "queued"]
        assert "import" in queued
        assert "story" in queued

        # And the intent the pipeline will read exists, from the default preset.
        intent = api_client.get(f"/api/projects/{project['id']}/intent").json()
        assert intent["target_duration_seconds"] == 1200

    def test_no_edit_versions_until_something_changes(self, api_client) -> None:
        project = create_project(api_client)
        body = api_client.get(f"/api/projects/{project['id']}/edit-versions").json()
        assert body["total"] == 0
