"""Controlled tuning, and the six reasons it almost never fires (V2-P10).

This is the only mechanism in the project that can change a decision without a
person asking, so the tests that matter are the refusals. Each guard has one,
and each asserts on the sentence rather than the exception type: "the tuner
declined" is useless to whoever has to act on it.

The mechanism itself is tested with synthetic outcomes, and that is the honest
limit of what can be shown today -- nothing here has ever run on a measured
video, because no video has been measured. The tests prove the arithmetic and
the fences; they prove nothing about whether tuning improves a channel, and
they are not written as though they did.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend.config.schema import StyleLimit
from backend.core.ids import new_id
from backend.tuning.deltas import RefusedError, TuningLedger
from backend.tuning.proposer import propose

pytestmark = pytest.mark.unit

KEY = "pacing.band_scale"


@pytest.fixture
def tuned(config):
    """The shipped configuration with the switch on. Off is the default."""
    return _switch(config, enabled=True)


def _switch(config, **fields):
    tuning = config.style.tuning.model_copy(update=fields)
    return config.model_copy(
        update={"style": config.style.model_copy(update={"tuning": tuning})}
    )


def _evidence():
    return {"metric": "averageViewPercentage", "comparison": "median"}


class TestTheSwitch:
    def test_it_is_off_in_the_shipped_configuration(self, config) -> None:
        # The state this project ships in and stays in until someone decides
        # otherwise, deliberately.
        assert config.style.tuning.enabled is False

    def test_nothing_may_move_while_it_is_off(self, database, config) -> None:
        with pytest.raises(RefusedError, match="switched off"):
            TuningLedger(database, config).apply(
                style="best_moments",
                key=KEY,
                delta=0.05,
                reason="because",
                evidence=_evidence(),
                videos=100,
            )


class TestTheGuards:
    def _ledger(self, database, config):
        return TuningLedger(database, config)

    def test_a_key_with_no_declared_range_is_refused(self, database, tuned) -> None:
        with pytest.raises(RefusedError, match="no declared range"):
            self._ledger(database, tuned).apply(
                style="best_moments",
                key="pacing.invented_dial",
                delta=0.01,
                reason="because",
                evidence=_evidence(),
                videos=100,
            )

    def test_too_little_evidence_is_refused_with_the_count(
        self, database, tuned
    ) -> None:
        with pytest.raises(RefusedError, match="3 measured video"):
            self._ledger(database, tuned).apply(
                style="best_moments",
                key=KEY,
                delta=0.05,
                reason="because",
                evidence=_evidence(),
                videos=3,
            )

    def test_a_step_larger_than_a_tenth_of_the_range_is_refused(
        self, database, tuned
    ) -> None:
        # band_scale is fenced at 0.5..2.0, so a tenth of its range is 0.15.
        with pytest.raises(RefusedError, match="larger than"):
            self._ledger(database, tuned).apply(
                style="best_moments",
                key=KEY,
                delta=0.4,
                reason="because",
                evidence=_evidence(),
                videos=100,
            )

    def test_a_change_with_no_reason_is_refused(self, database, tuned) -> None:
        with pytest.raises(RefusedError, match="needs a reason"):
            self._ledger(database, tuned).apply(
                style="best_moments",
                key=KEY,
                delta=0.05,
                reason="   ",
                evidence=_evidence(),
                videos=100,
            )

    def test_a_change_with_no_evidence_is_refused(self, database, tuned) -> None:
        with pytest.raises(RefusedError, match="needs a reason"):
            self._ledger(database, tuned).apply(
                style="best_moments",
                key=KEY,
                delta=0.05,
                reason="because",
                evidence={},
                videos=100,
            )

    def test_a_value_that_would_leave_the_fence_is_refused(
        self, database, config
    ) -> None:
        """The fence wins even against a step the size guard allows.

        band_scale sits at 1.0. Narrow its range to 0.5..1.03 and a tenth of
        that range is 0.053 -- so a step of 0.05 passes the size guard and
        still lands at 1.05, outside the fence. Two guards, and the value has
        to satisfy both.
        """
        narrowed = _switch(config, enabled=True)
        narrowed = narrowed.model_copy(
            update={
                "style": narrowed.style.model_copy(
                    update={
                        "limits": {
                            **narrowed.style.limits,
                            KEY: StyleLimit(min=0.5, max=1.03),
                        }
                    }
                )
            }
        )

        with pytest.raises(RefusedError, match="outside its declared range"):
            TuningLedger(database, narrowed).apply(
                style="best_moments",
                key=KEY,
                delta=0.05,
                reason="because",
                evidence=_evidence(),
                videos=100,
            )

    def test_a_key_that_just_moved_waits(self, database, tuned) -> None:
        ledger = TuningLedger(database, tuned)
        ledger.apply(
            style="best_moments",
            key=KEY,
            delta=0.05,
            reason="the first",
            evidence=_evidence(),
            videos=20,
        )

        # Five more measured videos are required; twenty-two is not enough.
        with pytest.raises(RefusedError, match="3 more measured video"):
            ledger.apply(
                style="best_moments",
                key=KEY,
                delta=-0.05,
                reason="the second",
                evidence=_evidence(),
                videos=22,
            )


class TestTheLedger:
    def test_a_delta_is_always_relative_to_the_file(self, database, tuned) -> None:
        """Not to the previous delta.

        Cumulative steps creep: ten legal tenths would leave the fence while
        each one looked reasonable. Base-relative means the displacement is
        bounded by the declared range no matter how many times it moves.
        """
        ledger = TuningLedger(database, tuned)
        first = ledger.apply(
            style="best_moments",
            key=KEY,
            delta=0.1,
            reason="one",
            evidence=_evidence(),
            videos=20,
        )
        second = ledger.apply(
            style="best_moments",
            key=KEY,
            delta=0.1,
            reason="two",
            evidence=_evidence(),
            videos=40,
        )

        assert first.base_value == pytest.approx(second.base_value)
        assert second.value == pytest.approx(first.value), "not 1.2"
        assert len(ledger.active("best_moments")) == 1, "the first was superseded"

    def test_reverting_puts_the_file_back(self, database, tuned) -> None:
        ledger = TuningLedger(database, tuned)
        applied = ledger.apply(
            style="best_moments",
            key=KEY,
            delta=0.1,
            reason="one",
            evidence=_evidence(),
            videos=20,
        )

        assert ledger.revert(applied.id) is True
        assert ledger.offsets("best_moments") == {}
        assert ledger.revert(applied.id) is False, "reverting twice changes nothing"

    def test_reverting_everything_always_works(self, database, tuned) -> None:
        # The command that has to work whatever state the ledger is in.
        ledger = TuningLedger(database, tuned)
        for key, delta in ((KEY, 0.1), ("critique.hook_seconds", 2.0)):
            ledger.apply(
                style="best_moments",
                key=key,
                delta=delta,
                reason="one",
                evidence=_evidence(),
                videos=20,
            )

        assert ledger.revert_all() == 2
        assert ledger.offsets("best_moments") == {}


class TestAnAdjustmentReachesTheCut:
    """A ledger the resolver never reads would be the whole thing built inert."""

    def test_the_resolved_style_carries_the_adjustment(
        self, database, tuned
    ) -> None:
        from backend.style import bible

        before = bible.resolve(tuned, "best_moments", database=database)
        TuningLedger(database, tuned).apply(
            style="best_moments",
            key=KEY,
            delta=0.1,
            reason="one",
            evidence=_evidence(),
            videos=20,
        )
        after = bible.resolve(tuned, "best_moments", database=database)

        assert after.pacing.band_scale == pytest.approx(before.pacing.band_scale + 0.1)
        assert after.tuned == (KEY,)

    def test_a_tuned_edit_does_not_share_a_fingerprint_with_an_untuned_one(
        self, database, tuned
    ) -> None:
        # Otherwise P9's record would say two different edits were the same one.
        from backend.style import bible

        before = bible.resolve(tuned, "best_moments", database=database)
        TuningLedger(database, tuned).apply(
            style="best_moments",
            key=KEY,
            delta=0.1,
            reason="one",
            evidence=_evidence(),
            videos=20,
        )
        after = bible.resolve(tuned, "best_moments", database=database)

        assert after.digest != before.digest

    def test_without_a_database_the_file_is_the_whole_truth(self, tuned) -> None:
        from backend.style import bible

        assert bible.resolve(tuned, "best_moments").tuned == ()

    def test_the_fence_is_checked_again_when_the_value_is_read(
        self, database, tuned
    ) -> None:
        """The file can be edited after a delta was recorded.

        A step that was legal against yesterday's base need not be legal
        against today's, so the bound is applied at read time as well.
        """
        from backend.style import bible

        TuningLedger(database, tuned).apply(
            style="best_moments",
            key=KEY,
            delta=0.15,
            reason="one",
            evidence=_evidence(),
            videos=20,
        )
        narrowed = tuned.model_copy(
            update={
                "style": tuned.style.model_copy(
                    update={
                        "limits": {
                            **tuned.style.limits,
                            KEY: StyleLimit(min=0.5, max=1.05),
                        }
                    }
                )
            }
        )

        resolved = bible.resolve(narrowed, "best_moments", database=database)

        assert resolved.pacing.band_scale == pytest.approx(1.05)


class TestTheProposer:
    """Every call refuses today, and the refusals have to be worth reading."""

    def test_no_data_says_how_far_away_it_is(self, database, config) -> None:
        proposal = propose(database, config, style="best_moments", key=KEY)

        assert proposal.refusal == (
            "0 of 15 measured video(s) for 'best_moments'. Nothing can be "
            "compared yet."
        )
        assert proposal.has_step is False

    def test_one_value_tried_is_not_a_comparison(
        self, database, project_manager, config
    ) -> None:
        for index in range(16):
            _measured(database, project_manager, index, band_scale=1.0, score=50.0)

        proposal = propose(database, config, style="best_moments", key=KEY)

        assert "nothing to compare it against" in proposal.refusal

    def test_two_arms_of_one_video_are_two_anecdotes(
        self, database, project_manager, config
    ) -> None:
        for index in range(15):
            _measured(database, project_manager, index, band_scale=1.0, score=50.0)
        _measured(database, project_manager, 99, band_scale=1.2, score=90.0)

        proposal = propose(database, config, style="best_moments", key=KEY)

        assert "measured video(s) each" in proposal.refusal

    def test_a_real_contrast_proposes_the_smallest_bounded_step(
        self, database, project_manager, config
    ) -> None:
        # Synthetic outcomes. This proves the arithmetic and nothing about
        # whether tuning improves a channel.
        for index in range(8):
            _measured(database, project_manager, index, band_scale=1.0, score=40.0)
        for index in range(8, 16):
            _measured(database, project_manager, index, band_scale=1.4, score=60.0)

        proposal = propose(database, config, style="best_moments", key=KEY)

        assert proposal.refusal == ""
        assert proposal.has_step
        # The better arm is 0.4 away; a tenth of the 0.5..2.0 range is 0.15.
        assert proposal.delta == pytest.approx(0.15)
        assert "capped at" in " ".join(proposal.notes)
        assert "not a significance test" in proposal.evidence()["comparison"]

    def test_a_proposal_is_not_a_licence(
        self, database, project_manager, config
    ) -> None:
        """It changes nothing on its own, and the switch still refuses it."""
        for index in range(8):
            _measured(database, project_manager, index, band_scale=1.0, score=40.0)
        for index in range(8, 16):
            _measured(database, project_manager, index, band_scale=1.4, score=60.0)
        proposal = propose(database, config, style="best_moments", key=KEY)

        with pytest.raises(RefusedError, match="switched off"):
            TuningLedger(database, config).apply(
                style="best_moments",
                key=KEY,
                delta=proposal.delta,
                reason=proposal.reason(),
                evidence=proposal.evidence(),
                videos=proposal.videos,
            )


def _measured(database, project_manager, index: int, *, band_scale: float, score: float):
    """A project with a stamped style, a published video and one outcome."""
    from backend.core.models.project import ProjectCreate

    project = project_manager.create(
        ProjectCreate(name=f"Measured {index}", target_duration_seconds=600)
    )
    now = datetime.now(timezone.utc).isoformat()
    video_id = f"vid-{index}"
    database.execute(
        "INSERT INTO edit_styles (project_id, asked, style, version, digest, "
        "resolved, created_at) VALUES (?, 'best_moments', 'best_moments', 1, ?, ?, ?)",
        (
            project.id,
            f"digest-{band_scale}",
            json.dumps({"pacing": {"band_scale": band_scale}}),
            now,
        ),
    )
    database.execute(
        "INSERT INTO analysis_jobs (id, project_id, stage, status, progress, "
        "result, created_at, completed_at) "
        "VALUES (?, ?, 'publish', 'completed', 1.0, ?, ?, ?)",
        (
            new_id("job"),
            project.id,
            json.dumps({"target": "youtube", "external_id": video_id}),
            now,
            now,
        ),
    )
    database.execute(
        "INSERT INTO video_outcomes (id, project_id, video_id, start_date, "
        "end_date, fetched_at, average_view_percentage, raw) "
        "VALUES (?, ?, ?, '2026-08-01', '2026-08-28', ?, ?, '{}')",
        (new_id("job").replace("job-", "out-"), project.id, video_id, now, score),
    )
    return project
