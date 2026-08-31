"""The critic that watches the finished video (V2-P7).

Every test here is a bug this stage actually shipped with. It ran once on a
real 250-second render and reported six findings and eleven applied
corrections, of which nine had removed nothing at all: the effects were read
in the wrong coordinate system, the targets it named did not exist, the guard
that keeps a composition whole was inspecting an attribute the object does not
have, and the rollback it promised queried a table the pipeline never writes.

A stage permitted to change an edit without a person asking is exactly the
place where "it reported success" and "it did the thing" have to be the same
sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from backend.critic2.models import ACTIONS, ANSWERS, DEFECTS, Finding
from backend.critic2.watch import EFFECTS_PER_TEN_SECONDS, corrections
from backend.effects.models import PlacedEffect
from backend.pipeline.workers.critic2_worker import Critic2Worker


@dataclass
class _Shot:
    """The little of a clip that a correction needs."""

    id: str
    timeline_start: float
    duration: float
    score: float

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration


def _run(clips, *, at=0.0, ids=(), shots=3, confidence=1.0) -> Finding:
    return Finding(
        code="repetition",
        at_seconds=at,
        detail="three shots in a row show the same thing",
        confidence=confidence,
        measured={"shots": shots, "shared_labels": ["inventory"], "clip_ids": list(ids)},
    )


def _pile(at: float, count: int, confidence: float = 1.0) -> Finding:
    return Finding(
        code="effect_overuse",
        at_seconds=at,
        detail=f"{count} effects inside ten seconds",
        confidence=confidence,
        measured={"effects": count, "window_seconds": 10.0},
    )


def _effect(identifier: str, at: float, *, composition=None, strength=1.0) -> PlacedEffect:
    from backend.core.models.enums import EffectType

    return PlacedEffect(
        id=identifier,
        effect=EffectType.IMPACT,
        timeline_start=at,
        duration_seconds=0.5,
        clip_id="clip-1",
        composition_id=composition,
        strength=strength,
    )


class TestTheVocabularyIsHonest:
    """§P0: a verb that is promised and not implemented is the whole sin."""

    def test_every_answered_defect_has_a_verb_the_worker_implements(self) -> None:
        # The map said repetition could be answered by a trim and effect_overuse
        # by a weakening. Neither existed, so a real run produced findings and
        # no corrections, and the report read as though nothing was wrong.
        import inspect

        source = inspect.getsource(Critic2Worker._apply)
        for code, verbs in ANSWERS.items():
            for verb in verbs:
                assert f'correction.action == "{verb}"' in source, (
                    f"{code} is answered by {verb!r}, which _apply cannot do"
                )
        for action in ACTIONS:
            assert f'correction.action == "{action}"' in source, (
                f"{action!r} is offered by the vocabulary and cannot be performed"
            )

    def test_a_defect_with_no_verb_is_reported_and_never_invented_around(self) -> None:
        clips = [_Shot(f"clip-{index}", index * 5.0, 5.0, 0.5) for index in range(8)]
        weak_hook = Finding(
            code="weak_hook", at_seconds=0.0, detail="the opening is quiet", confidence=0.9
        )

        made, refused = corrections([weak_hook], clips=clips)

        assert made == []
        assert any("no verb" in reason for reason in refused)

    def test_there_is_no_verb_for_reordering(self) -> None:
        # Chronology is the one rule the critic may not touch, and the way to
        # keep it is that the vocabulary cannot say it.
        assert set(ACTIONS) == {"trim_start", "trim_end", "drop", "remove_effect"}
        assert not {"reorder", "move", "swap", "insert"} & set(ACTIONS)

    def test_every_defect_code_is_answered_or_explicitly_not(self) -> None:
        assert set(ANSWERS) == set(DEFECTS)


class TestEffectsAreReadWhereTheViewerMeetsThem:
    """The coordinate bug: stored times are clip-relative, findings are not."""

    def test_a_stored_effect_is_placed_in_programme_time(self, database, project_manager, config) -> None:
        from backend.database.repositories.timeline import TimelineRepository

        project, _repository, timeline = _seed(database, project_manager, config)
        clip = timeline.video_clips()[2]
        database.execute(
            "INSERT INTO timeline_effects "
            "(id, project_id, clip_id, effect_type, start_seconds, duration_seconds) "
            "VALUES (?, ?, ?, 'impact', 1.5, 0.5)",
            ("fx-1", project.id, clip.id),
        )

        placed = TimelineRepository(database).list_placed(project.id)

        assert len(placed) == 1
        # Not 1.5: that is where it sits inside its clip, and the critic reads
        # the finished video, where the clip starts much later.
        assert placed[0].timeline_start == pytest.approx(clip.timeline_start + 1.5)
        assert placed[0].timeline_start > 1.5

    def test_a_pile_is_counted_across_the_programme_not_inside_the_clips(self) -> None:
        from backend.critic2.watch import _effect_overuse

        # Thirteen effects spread over four minutes: this is the gate project,
        # and it was reported as thirteen effects inside ten seconds because
        # every stored time was small.
        spread = [_effect(f"fx-{index}", 20.0 * index) for index in range(13)]

        assert _effect_overuse(spread, 260.0) == []

    def test_a_real_pile_is_still_found(self) -> None:
        from backend.critic2.watch import _effect_overuse

        packed = [_effect(f"fx-{index}", 100.0 + index) for index in range(6)]

        found = _effect_overuse(packed, 260.0)

        assert [item.code for item in found] == ["effect_overuse"]
        assert found[0].measured["effects"] > EFFECTS_PER_TEN_SECONDS


class TestASentenceStaysWhole:
    """P4 admits a composition atomically; the critic may not undo that."""

    def test_a_composition_member_is_never_thinned(self) -> None:
        window = [
            _effect("fx-free-1", 100.0, strength=0.2),
            _effect("fx-free-2", 101.0, strength=0.9),
            _effect("fx-a", 102.0, composition="payoff_heavy"),
            _effect("fx-b", 103.0, composition="payoff_heavy"),
            _effect("fx-c", 104.0, composition="payoff_heavy"),
            _effect("fx-d", 105.0, composition="payoff_heavy"),
        ]

        made, _refused = corrections(
            [_pile(100.0, len(window))], clips=[], effects=window
        )

        removed = {item.target for item in made}
        assert removed <= {"fx-free-1", "fx-free-2"}
        assert not any(target.startswith("fx-") and target in {"fx-a", "fx-b"} for target in removed)

    def test_the_weakest_free_effect_goes_first(self) -> None:
        window = [
            _effect("fx-loud", 100.0, strength=0.9),
            _effect("fx-quiet", 101.0, strength=0.1),
            _effect("fx-mid", 102.0, strength=0.5),
            _effect("fx-x", 103.0, composition="payoff_light"),
            _effect("fx-y", 104.0, composition="payoff_light"),
            _effect("fx-z", 105.0, composition="payoff_light"),
        ]

        made, _refused = corrections([_pile(100.0, 6)], clips=[], effects=window)

        assert [item.target for item in made] == ["fx-quiet", "fx-mid"]

    def test_a_pile_that_is_entirely_composed_is_refused_with_its_reason(self) -> None:
        window = [
            _effect(f"fx-{index}", 100.0 + index, composition="payoff_heavy")
            for index in range(6)
        ]

        made, refused = corrections([_pile(100.0, 6)], clips=[], effects=window)

        assert made == []
        assert any("composed" in reason for reason in refused)


class TestRepetitionDropsOneShotNotTheRun:
    def test_the_weakest_shot_of_the_run_is_the_one_dropped(self) -> None:
        clips = [_Shot(f"clip-{index}", index * 5.0, 5.0, 0.9) for index in range(8)]
        clips[3].score = 0.1

        made, _refused = corrections(
            [_run(clips, ids=[clip.id for clip in clips[2:5]])], clips=clips
        )

        assert [item.action for item in made] == ["drop"]
        assert made[0].target == "clip-3"

    def test_an_edit_at_the_floor_is_left_alone(self) -> None:
        clips = [_Shot(f"clip-{index}", index * 5.0, 5.0, 0.5) for index in range(4)]

        made, refused = corrections(
            [_run(clips, ids=[clip.id for clip in clips[:3]])], clips=clips, min_clips=4
        )

        assert made == []
        assert any("shots already" in reason for reason in refused)

    def test_a_run_whose_clips_have_gone_is_not_guessed_at(self) -> None:
        clips = [_Shot(f"clip-{index}", index * 5.0, 5.0, 0.5) for index in range(8)]

        made, refused = corrections(
            [_run(clips, ids=["clip-gone-1", "clip-gone-2", "clip-gone-3"])], clips=clips
        )

        assert made == []
        assert any("no longer in the edit" in reason for reason in refused)


class TestTheEditCanBePutBack:
    """The third lock: a correction that lowers quality must be revertible."""

    def test_a_restore_re_enables_clips_and_brings_back_deleted_effects(
        self, database, project_manager, config
    ) -> None:
        project, repository, timeline = _seed(database, project_manager, config)
        clip = timeline.video_clips()[2]
        database.execute(
            "INSERT INTO timeline_effects "
            "(id, project_id, clip_id, effect_type, start_seconds, duration_seconds) "
            "VALUES (?, ?, ?, 'impact', 1.0, 0.5)",
            ("fx-1", project.id, clip.id),
        )
        context = SimpleNamespace(database=database, job=SimpleNamespace(project_id=project.id))
        context.project_id = project.id
        worker = Critic2Worker()

        assert worker._snapshot(context, 82.0) is True
        repository.set_enabled(project.id, clip.id, enabled=False)
        database.execute("DELETE FROM timeline_effects WHERE id = 'fx-1'")

        assert worker._restore(context) is True

        assert repository.load(project.id).clip(clip.id).enabled is True
        assert database.fetch_one("SELECT id FROM timeline_effects WHERE id = 'fx-1'")

    def test_the_captions_of_surviving_clips_are_not_collateral(
        self, database, project_manager, config
    ) -> None:
        # SQLite's REPLACE deletes before it inserts, and captions reference
        # timeline_clips ON DELETE CASCADE: restoring clips that way would take
        # the captions of every clip the correction never touched.
        project, repository, timeline = _seed(database, project_manager, config)
        keeper = timeline.video_clips()[0]
        database.execute(
            "INSERT INTO captions "
            "(id, project_id, clip_id, caption_index, timeline_start, timeline_end, text) "
            "VALUES (?, ?, ?, 0, 0.5, 1.5, 'the words stay')",
            ("cap-1", project.id, keeper.id),
        )
        context = SimpleNamespace(database=database, project_id=project.id)
        worker = Critic2Worker()
        worker._snapshot(context, 82.0)
        repository.set_enabled(project.id, timeline.video_clips()[3].id, enabled=False)

        worker._restore(context)

        assert database.fetch_one("SELECT text FROM captions WHERE id = 'cap-1'")

    def test_a_missing_snapshot_is_reported_rather_than_assumed(
        self, database, project_manager, config
    ) -> None:
        project, _repository, _timeline = _seed(database, project_manager, config)
        context = SimpleNamespace(database=database, project_id=project.id)

        # The first version returned True here, having done nothing, and the
        # log said the previous edit was restored.
        assert Critic2Worker()._restore(context) is False


class TestOneCycle:
    """V2 is the last version, and the counter has to survive a reload."""

    def test_the_counter_is_not_kept_where_it_would_be_discarded(
        self, database, project_manager, config
    ) -> None:
        project, repository, timeline = _seed(database, project_manager, config)
        edited = timeline.model_copy(update={"metadata": {"critic2_revision": 1}})
        repository.save_edit(project.id, edited)

        # The repository builds a timeline from its clip rows; nothing carries
        # the timeline's own metadata, so a counter kept there reads zero on
        # the next run and the stage corrects for ever.
        assert repository.load(project.id).metadata.get("critic2_revision") is None

    def test_the_cycle_is_spent_once_the_snapshot_exists(
        self, database, project_manager, config
    ) -> None:
        project, _repository, _timeline = _seed(database, project_manager, config)
        context = SimpleNamespace(database=database, project_id=project.id)
        worker = Critic2Worker()

        assert worker._spent(context) is None

        worker._snapshot(context, 86.0)

        assert worker._spent(context) == pytest.approx(86.0)


def _seed(database, project_manager, config):
    """A stored project with a real timeline behind it."""
    from datetime import datetime, timezone

    from backend.core.ids import new_id
    from backend.core.models.enums import MomentType
    from backend.core.models.media import Media, MediaMetadata
    from backend.core.models.project import ProjectCreate
    from backend.database.repositories.media import MediaRepository
    from backend.database.repositories.timeline import TimelineRepository
    from backend.timeline.builder import PlannedClip, build_timeline

    project = project_manager.create(
        ProjectCreate(name="Critic2", target_duration_seconds=600)
    )
    now = datetime.now(timezone.utc)
    media = MediaRepository(database).create(
        Media(
            id=new_id("media"),
            project_id=project.id,
            source_path="D:/recordings/session.mp4",
            filename="session.mp4",
            container=".mp4",
            size_bytes=1024,
            checksum="0" * 64,
            metadata=MediaMetadata(duration_seconds=10_000.0),
            created_at=now,
            updated_at=now,
        )
    )
    clips = [
        PlannedClip(
            media_id=media.id,
            source_start=index * 100.0,
            source_end=index * 100.0 + 40.0,
            moment_type=MomentType.EPIC,
            score=0.5 + index / 100.0,
            role="hook" if index == 0 else "body",
        )
        for index in range(6)
    ]
    timeline = build_timeline(
        clips,
        project_id=project.id,
        policy=config.output.duration_policy(),
        media_durations={media.id: 10_000.0},
    ).timeline
    repository = TimelineRepository(database)
    repository.replace(project.id, timeline)
    return project, repository, timeline


class TestTheRollbackDoesNotSpin:
    """The recovery path is a loop unless something closes it."""

    def test_an_edit_is_restored_once_and_not_again(
        self, database, project_manager, config
    ) -> None:
        # _second_pass re-queues the render after a rollback, and that render
        # brings this stage back with the snapshot still in place. If the
        # restored edit scored below the stored figure -- encoder variance is
        # enough -- the pair would trade a restore for a re-render for ever.
        project, repository, timeline = _seed(database, project_manager, config)
        context = SimpleNamespace(database=database, project_id=project.id)
        worker = Critic2Worker()
        worker._snapshot(context, 90.0)
        repository.set_enabled(project.id, timeline.video_clips()[2].id, enabled=False)

        assert worker._restore(context) is True
        assert worker._restore(context) is False

    def test_a_second_look_says_it_was_already_restored_not_that_it_failed(
        self, database, project_manager, config
    ) -> None:
        project, repository, timeline = _seed(database, project_manager, config)
        context = SimpleNamespace(database=database, project_id=project.id)
        worker = Critic2Worker()
        worker._snapshot(context, 90.0)
        repository.set_enabled(project.id, timeline.video_clips()[2].id, enabled=False)
        worker._restore(context)

        assert worker._already_restored(context) is True

    def test_an_attempt_that_changed_nothing_does_not_spend_the_cycle(
        self, database, project_manager, config
    ) -> None:
        from backend.critic2.models import EditCorrection, Evidence

        project, repository, timeline = _seed(database, project_manager, config)
        context = SimpleNamespace(database=database, project_id=project.id)
        worker = Critic2Worker()
        nowhere = EditCorrection(
            action="remove_effect",
            target="fx-that-is-not-there",
            reason="effect_overuse",
            evidence=Evidence(at_seconds=10.0),
        )

        assert worker._apply(context, repository, timeline, [nowhere]) == 0
        # Otherwise the stage retires for this project having changed nothing.
        assert worker._spent(context) is None


class TestTheSecondPassHappensWithoutBeingAsked:
    """The loop the first version could not close.

    ``_requeue`` re-queued the render and QA and its comment claimed CRITIC2
    would return "by construction" because it depends on QA. A dependency
    gates whether a stage *may* run, not whether a completed job goes back in
    the queue -- so on a real pipeline the second pass never happened, and the
    no-degradation lock never fired on a video anyone would watch.
    """

    def test_a_correction_owes_a_verdict(
        self, database, project_manager, config
    ) -> None:
        from backend.pipeline.workers.critic2_worker import owes_a_second_look

        project, _repository, _timeline = _seed(database, project_manager, config)
        context = SimpleNamespace(database=database, project_id=project.id)
        assert owes_a_second_look(database, project.id) is False

        Critic2Worker()._snapshot(context, 80.0)
        _finished(database, project.id, {"revision": 1, "applied": 3})

        assert owes_a_second_look(database, project.id) is True

    def test_a_verdict_is_owed_only_once(
        self, database, project_manager, config
    ) -> None:
        from backend.pipeline.workers.critic2_worker import owes_a_second_look

        project, _repository, _timeline = _seed(database, project_manager, config)
        context = SimpleNamespace(database=database, project_id=project.id)
        Critic2Worker()._snapshot(context, 80.0)
        # Pass two always writes "kept", and that is the whole termination
        # argument: the pair cannot trade a render for a re-render for ever.
        _finished(database, project.id, {"revision": 1, "kept": True})

        assert owes_a_second_look(database, project.id) is False

    def test_an_uncorrected_edit_owes_nothing(
        self, database, project_manager, config
    ) -> None:
        from backend.pipeline.workers.critic2_worker import owes_a_second_look

        project, _repository, _timeline = _seed(database, project_manager, config)
        _finished(database, project.id, {"revision": 0, "findings": []})

        assert owes_a_second_look(database, project.id) is False


def _finished(database, project_id: str, result: dict) -> None:
    """A completed CRITIC2 job carrying this result."""
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    database.execute(
        "INSERT INTO analysis_jobs "
        "(id, project_id, stage, status, progress, result, created_at, completed_at) "
        "VALUES (?, ?, 'critic2', 'completed', 1.0, ?, ?, ?)",
        (f"job-{project_id[-8:]}", project_id, json.dumps(result), now, now),
    )


class TestTheCriticChecksTheStyleItWasCutAs:
    """V2-P11: did the machine do what it said it would?

    A different question from whether the result is good, and measured against
    the doctrine the edit was *actually* made under -- P8's stamp, not the
    brief as it reads today.
    """

    def _clips(self, seconds: float, count: int = 20):
        return [
            _Shot(f"clip-{index}", index * seconds, seconds, 0.5)
            for index in range(count)
        ]

    def test_a_patient_style_cut_fast_is_a_violation(self, config) -> None:
        from backend.critic2.watch import _style_violations
        from backend.style import bible

        patient = bible.resolve(config, "cinematic")
        found = _style_violations(self._clips(1.2), (), 120.0, patient)

        assert [item.code for item in found] == ["style_violation"]
        assert "faster than it was meant to be" in found[0].detail

    def test_the_same_edit_is_not_a_violation_of_a_fast_style(self, config) -> None:
        from backend.critic2.watch import _style_violations
        from backend.style import bible

        fast = bible.resolve(config, "gaming_fast")

        assert _style_violations(self._clips(1.9), (), 120.0, fast) == []

    def test_a_style_that_wants_no_effects_and_got_some_is_a_violation(
        self, config
    ) -> None:
        from backend.critic2.watch import _style_violations
        from backend.style import bible

        minimal = bible.resolve(config, "minimal")
        placed = [_effect(f"fx-{index}", index * 5.0) for index in range(8)]
        found = _style_violations(self._clips(2.4), placed, 120.0, minimal)

        assert any("effects a minute" in item.detail for item in found)

    def test_restraint_is_not_a_violation(self, config) -> None:
        # A style that wanted effects and got fewer has been restrained, which
        # the emphasis engine is allowed to do when the footage does not earn
        # them. Only the other direction is a violation.
        from backend.critic2.watch import _style_violations
        from backend.style import bible

        house = bible.resolve(config, "best_moments")

        assert _style_violations(self._clips(2.4), (), 120.0, house) == []

    def test_with_no_style_there_is_nothing_to_violate(self) -> None:
        from backend.critic2.watch import _style_violations

        assert _style_violations(self._clips(2.4), (), 120.0, None) == []

    def test_it_is_reported_and_never_acted_on(self) -> None:
        """§42 has no verb that answers a style violation, and says so.

        Trimming a shot does not make a style right and re-selecting is a
        different video. Inventing a correction here would be exactly the
        failure P7 exists to prevent.
        """
        assert ANSWERS["style_violation"] == ()

        finding = Finding(
            code="style_violation",
            at_seconds=0.0,
            detail="cut faster than it was meant to be",
            confidence=0.9,
        )
        made, refused = corrections([finding], clips=[])

        assert made == []
        assert any("no verb" in reason for reason in refused)
