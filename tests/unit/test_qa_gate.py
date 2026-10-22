"""NOT_MEASURED and the gate (PASS / WARN / NEEDS_REVIEW / BLOCK).

A check that could not run is not a pass. The gate is an adapter over the
verdicts QA already gives -- ``blocks_export``, ``needs_review``, ``status`` --
so a project where every check ran is judged exactly as before; only a check
that did not run changes what the gate says, and never to BLOCK.

Acceptance (item 3):
* no existing verdict changes for a report whose every check ran;
* a check that did not run is never PASS;
* BLOCK is exactly today's blocking set (``blocks_export``);
* enum, API schema and the GET /qa response carry the new status.
"""

from __future__ import annotations

import itertools

import pytest

from backend.config.loader import load_config
from backend.core.models.enums import QAGate, QAStatus
from backend.qa.report import build_report, failure, not_measured, passed, warning


@pytest.fixture(scope="module")
def qa_config():
    return load_config().qa


def _permissive(config):
    return config.model_copy(
        update={"policy": config.policy.model_copy(update={"fail_on_technical_error": False})}
    )


def _kinds(*names):
    made = []
    for index, name in enumerate(names):
        check = f"check_{index}"
        if name == "pass":
            made.append(passed(check, "fine"))
        elif name == "warn":
            made.append(warning(check, "odd", remedy="look"))
        elif name == "fail":
            made.append(failure(check, "broken", remedy="fix"))
        else:
            made.append(not_measured(check, "could not run", remedy="measure it"))
    return made


class TestNotMeasured:
    def test_the_status_exists_and_is_neither_failure_nor_warning(self) -> None:
        finding = not_measured("loudness", "no audio decoder", remedy="install one")

        assert finding.status is QAStatus.NOT_MEASURED
        assert QAStatus("not_measured") is QAStatus.NOT_MEASURED
        assert not finding.failed
        assert not finding.warned
        assert finding.unmeasured
        assert finding.as_row()["status"] == "not_measured"

    def test_a_check_that_did_not_run_is_never_pass(self, qa_config) -> None:
        report = build_report(_kinds("pass", "not_measured"), qa_config)

        assert report.gate is not QAGate.PASS
        assert report.gate is QAGate.NEEDS_REVIEW
        assert [item.check for item in report.not_measured] == ["check_1"]

    def test_not_measured_alone_never_blocks(self, qa_config) -> None:
        report = build_report(_kinds("not_measured", "not_measured"), qa_config)

        assert not report.blocks_export
        assert report.gate is QAGate.NEEDS_REVIEW

    def test_the_summary_names_what_was_not_measured(self, qa_config) -> None:
        summary = build_report(_kinds("pass", "not_measured"), qa_config).summary()

        assert summary["gate"] == "needs_review"
        assert summary["not_measured"] == ["[not_measured] check_1: could not run"]
        assert summary["checks"] == 2

    def test_the_explanation_lists_it(self, qa_config) -> None:
        lines = build_report(_kinds("not_measured"), qa_config).explain()

        assert lines[0].startswith("[not_measured] check_0")

    def test_the_stage_message_does_not_claim_every_check_passed(self, qa_config) -> None:
        from backend.pipeline.workers.qa_worker import _message

        message = _message(build_report(_kinds("pass", "not_measured"), qa_config))

        assert "all checks passed" not in message
        assert "not measured" in message


class TestAdapter:
    """Where every check ran, nothing that existed before changes."""

    @pytest.mark.parametrize(
        "kinds",
        [
            combo
            for size in (1, 2, 3)
            for combo in itertools.product(("pass", "warn", "fail"), repeat=size)
        ],
    )
    @pytest.mark.parametrize("permissive", [False, True])
    def test_the_gate_follows_the_existing_verdicts(self, qa_config, kinds, permissive) -> None:
        config = _permissive(qa_config) if permissive else qa_config
        report = build_report(_kinds(*kinds), config)

        # The verdicts as they always were.
        has_fail, has_warn = "fail" in kinds, "warn" in kinds
        assert report.status is (
            QAStatus.FAILED if has_fail else QAStatus.WARNING if has_warn else QAStatus.PASSED
        )
        assert report.blocks_export is (has_fail and not permissive)

        # The gate is read off them, BLOCK exactly when export is blocked.
        assert (report.gate is QAGate.BLOCK) is report.blocks_export
        if not report.blocks_export:
            if report.needs_review:
                assert report.gate is QAGate.NEEDS_REVIEW
            elif has_warn:
                assert report.gate is QAGate.WARN
            else:
                assert report.gate is QAGate.PASS

    def test_a_blocking_failure_beats_not_measured(self, qa_config) -> None:
        report = build_report(_kinds("fail", "not_measured"), qa_config)

        assert report.status is QAStatus.FAILED
        assert report.blocks_export
        assert report.gate is QAGate.BLOCK


@pytest.mark.integration
class TestApi:
    """GET /qa reads a stored ``not_measured`` row rather than crashing on it."""

    @pytest.fixture
    def seeded(self, api_client, app_state):
        from backend.database.repositories.qa import QaRepository
        from backend.database.repositories.renders import RenderRepository

        response = api_client.post(
            "/api/projects",
            json={"name": "Gate", "target_duration_seconds": 600, "mode": "story"},
        )
        assert response.status_code == 201, response.text
        project_id = response.json()["id"]
        renders = RenderRepository(app_state.database)
        render_id = renders.start(project_id, resolution=1080, fps=60, encoder="libx264")

        def seed(*kinds):
            QaRepository(app_state.database).replace_for_render(
                project_id, render_id, _kinds(*kinds)
            )
            return api_client.get(f"/api/projects/{project_id}/qa")

        return seed

    def test_a_not_measured_row_is_reported_as_such(self, seeded) -> None:
        response = seeded("pass", "not_measured")

        assert response.status_code == 200, response.text
        body = response.json()
        statuses = {item["check"]: item["qa_status"] for item in body["findings"]}
        assert statuses == {"check_0": "passed", "check_1": "not_measured"}
        # GET /qa keeps its blocking rule; the gate says a person should look.
        assert body["blocks_export"] is False
        assert body["gate"] == "needs_review"

    def test_where_every_check_ran_the_response_is_as_before(self, seeded) -> None:
        body = seeded("pass", "warn").json()
        assert (body["qa_status"], body["blocks_export"], body["needs_review"]) == (
            "warning",
            False,
            True,
        )
        assert body["gate"] == "needs_review"

        body = seeded("pass", "fail").json()
        assert (body["qa_status"], body["blocks_export"], body["needs_review"]) == (
            "failed",
            True,
            True,
        )
        assert body["gate"] == "block"

        body = seeded("pass", "pass").json()
        assert (body["qa_status"], body["blocks_export"], body["needs_review"]) == (
            "passed",
            False,
            False,
        )
        assert body["gate"] == "pass"
