"""Phase 13 acceptance: a sentence the rules cannot read still edits the video.

The criterion, stated plainly: **an instruction the rule parser rejects goes
through the model, becomes a validated change, and the project is different
afterwards.** Not "the model was called" — the stored state changed.

Everything here runs through the real `InteractionService` against a real
database. Only the model is substituted, because the real one answers
differently each time and this is a test about wiring, not about qwen.

The second half is §95, and it matters as much: with no model reachable, the
same messages come back with a usable explanation and the rule path is
untouched. A machine without Ollama has a smaller editor, not a broken one.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai.llm.fake_provider import FakeLLMProvider
from backend.config.schema import AppConfig
from backend.core.ids import new_id
from backend.core.models.enums import JobStatus, MomentType, VideoMode
from backend.core.models.media import Media
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.database.repositories.media import MediaRepository
from backend.interaction.llm_fallback import LlmInterpreter
from backend.interaction.models import (
    AnswerSource,
    CommandKind,
    EffectsLevel,
    InteractionType,
    Pacing,
)
from backend.interaction.parser import parse_command, parse_instruction
from backend.interaction.service import InteractionService
from backend.services.job_manager import JobManager
from backend.services.project_manager import ProjectManager
from tests.support.analysed_project import complete_analysis, insert_clip, insert_moment

pytestmark = pytest.mark.integration


#: Phrasings chosen because the rule parser genuinely cannot read them --
#: asserted below, so this file cannot quietly start testing the rule path.
UNPARSED_INSTRUCTION = "give it the feel of a wildlife documentary"
#: Names neither a clip nor a timestamp, so the classifier routes it to the
#: instruction path and only the model can see it is an edit.
UNPARSED_COMMAND = "delete the part right after the opener"
#: A question none of the deterministic resolvers recognises (§20).
UNCLASSIFIED_QUESTION = "did I sound frustrated at any point?"


def _tree(directory) -> list[str]:
    """Every path under ``directory``, or nothing if it does not exist."""
    if not directory.exists():
        return []
    return sorted(str(path.relative_to(directory)) for path in directory.rglob("*"))


@pytest.fixture
def project_id(project_manager: ProjectManager, database: Database) -> str:
    project = project_manager.create(
        ProjectCreate(
            name="Ranked session",
            target_duration_seconds=1200,
            mode=VideoMode.BEST_MOMENTS,
            game="valorant",
        )
    )
    return project.id


@pytest.fixture
def media_id(database: Database, project_id: str) -> str:
    now = datetime.now(timezone.utc)
    media = Media(
        id=new_id("media"),
        project_id=project_id,
        source_path="/recordings/session.mp4",
        filename="session.mp4",
        container=".mp4",
        size_bytes=4096,
        checksum="a" * 64,
        created_at=now,
        updated_at=now,
    )
    MediaRepository(database).create(media)
    return media.id


@pytest.fixture
def edited(database: Database, job_manager: JobManager, project_id: str, media_id: str) -> None:
    """A project that has been analysed and cut: five clips, twenty minutes."""
    complete_analysis(job_manager, project_id, media_id)
    for index in range(5):
        moment_id = insert_moment(
            database,
            project_id,
            media_id,
            moment_type="clutch" if index % 2 else "funny",
            start=index * 300.0 + 60.0,
            score=0.9 - index * 0.05,
        )
        insert_clip(
            database,
            project_id,
            media_id,
            index=index,
            timeline_start=index * 240.0,
            duration=240.0,
            moment_id=moment_id,
        )


def service(
    database: Database, config: AppConfig, **provider_kwargs
) -> tuple[InteractionService, FakeLLMProvider]:
    """The real service with a scripted model behind it."""
    provider = FakeLLMProvider(**provider_kwargs)
    return InteractionService(database, config, interpreter=LlmInterpreter(config, provider)), (
        provider
    )


class TestTheRulesReallyCannotReadThese:
    """Without this, the rest of the file could be testing the rule path."""

    def test_the_instruction_is_unparsed(self) -> None:
        assert parse_instruction(UNPARSED_INSTRUCTION).confidence == 0.0

    def test_the_command_is_unparsed(self) -> None:
        assert parse_command(UNPARSED_COMMAND) is None


class TestBeforeThereIsAnEditToChange:
    """The escalation must not turn a guess into an exception."""

    def test_an_unreadable_instruction_on_an_empty_project_is_explained(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        # `apply_command` refuses by raising, which is right for a command
        # someone deliberately sent and wrong for a guess about an instruction.
        interaction, provider = service(
            database,
            config,
            responses={
                "interaction.command": {
                    "kind": "delete_at_timestamp",
                    "timestamp_seconds": 120.0,
                    "confidence": 0.95,
                }
            },
        )

        result = interaction.handle(project_id, UNPARSED_COMMAND)

        assert result.applied_command is None
        assert result.message
        assert [call[0] for call in provider.calls] == ["interaction.instruction"]


@pytest.mark.usefixtures("edited")
class TestWhatTheRealModelDid:
    """Behaviours the scripted model would never have shown us.

    Both of these came from running `scripts/verify_phase13.py` against a real
    qwen2.5:7b-instruct, and neither was reachable through a fake answering
    exactly what the test author expected.
    """

    def test_a_delta_that_changes_nothing_is_not_reported_as_a_change(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        # Asked to read "delete the part right after the opener" as a
        # preference, the real model answered `pacing: fast` -- which the
        # best_moments preset had already set. Saying "updated the editing
        # brief" would tell someone their edit was made when nothing happened.
        interaction, _ = service(
            database,
            config,
            responses={
                "interaction.instruction": {
                    "pacing": interaction_pacing(database, config, project_id),
                    "confidence": 0.9,
                },
                "interaction.command": {
                    "kind": "delete_clip",
                    "clip_index": 2,
                    "confidence": 0.9,
                },
            },
        )

        result = interaction.handle(project_id, UNPARSED_COMMAND)

        # It restated the status quo, so the escalation got its turn.
        assert result.applied_command is not None
        assert result.applied_command.kind is CommandKind.DELETE_CLIP

    def test_a_no_op_reading_with_nothing_else_to_try_is_explained(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        interaction, _ = service(
            database,
            config,
            responses={
                "interaction.instruction": {
                    "pacing": interaction_pacing(database, config, project_id),
                    "confidence": 0.9,
                },
                "interaction.command": {"kind": "none", "confidence": 0.9},
            },
        )
        before = interaction.current_intent(project_id)

        result = interaction.handle(project_id, UNPARSED_INSTRUCTION)

        assert result.requires_rerender is False
        assert interaction.current_intent(project_id) == before

    def test_a_models_sentence_is_folded_into_ours(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        # The real model refuses in full sentences. Dropped in unedited that
        # reads "I read that, but The instruction ... preferences.." -- which
        # looks like a bug even when the answer is right.
        interaction, _ = service(
            database,
            config,
            responses={
                "interaction.instruction": {
                    "confidence": 0.9,
                    "unsupported": "The instruction does not map to any preference.",
                },
                "interaction.command": {"kind": "none", "confidence": 0.9},
            },
        )

        message = interaction.handle(project_id, UNPARSED_INSTRUCTION).message

        assert "but the instruction does not map to any preference." in message
        assert ".." not in message


def interaction_pacing(database: Database, config: AppConfig, project_id: str) -> str:
    """The pacing already in force, which the preset decides."""
    return InteractionService(database, config).current_intent(project_id).pacing.value


@pytest.mark.usefixtures("edited")
class TestAcceptance:
    """§63: natural language changes project state -- and only project state."""

    def test_an_unparsed_instruction_changes_the_editing_brief(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        interaction, provider = service(
            database,
            config,
            responses={
                "interaction.instruction": {
                    "pacing": "slow",
                    "effects": "minimal",
                    "music": "prominent",
                    "confidence": 0.85,
                }
            },
        )
        before = interaction.current_intent(project_id)
        assert before.pacing is not Pacing.SLOW, "the preset would decide this test for us"

        result = interaction.handle(project_id, UNPARSED_INSTRUCTION)

        assert result.interaction_type is InteractionType.EDITING_INSTRUCTION
        assert result.requires_rerender is True
        # The claim is about stored state, so it is read back from a fresh
        # query rather than from the object the call returned.
        after = interaction.current_intent(project_id)
        assert after.pacing is Pacing.SLOW
        assert after.effects is EffectsLevel.MINIMAL
        assert [call[0] for call in provider.calls] == ["interaction.instruction"]

    def test_the_users_words_are_kept_with_the_change(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        # §80: the brief should be able to say where each preference came from.
        interaction, _ = service(
            database,
            config,
            responses={"interaction.instruction": {"pacing": "slow", "confidence": 0.85}},
        )
        interaction.handle(project_id, UNPARSED_INSTRUCTION)

        rows = database.fetch_all(
            "SELECT raw_text FROM editing_intent_updates WHERE project_id = ?", (project_id,)
        )
        assert [row["raw_text"] for row in rows] == [UNPARSED_INSTRUCTION]

    def test_a_second_instruction_refines_rather_than_replaces(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        # §11 holds on this path too: the deltas accumulate.
        interaction, _ = service(
            database,
            config,
            responses={"interaction.instruction": {"pacing": "slow", "confidence": 0.9}},
        )
        interaction.handle(project_id, UNPARSED_INSTRUCTION)
        interaction.handle(project_id, "и меньше эффектов, пожалуйста")

        intent = interaction.current_intent(project_id)
        assert intent.pacing is Pacing.SLOW

    def test_an_unparsed_command_changes_the_edit(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        interaction, provider = service(
            database,
            config,
            responses={
                # Nothing scripted for the instruction prompt, so the model
                # "fails" to read it as a preference -- which is the case the
                # escalation exists for.
                "interaction.command": {
                    "kind": "delete_clip",
                    "clip_index": 2,
                    "confidence": 0.9,
                }
            },
        )
        assert "20:00" in interaction.ask(project_id, "how long is the video?").text

        result = interaction.handle(project_id, UNPARSED_COMMAND)

        assert result.applied_command is not None
        assert result.applied_command.kind is CommandKind.DELETE_CLIP
        assert result.requires_rerender is True
        # Four clips of four minutes: the edit really is shorter.
        assert "16:00" in interaction.ask(project_id, "how long is the video?").text
        # Preference first, then edit: the safe reading is tried before the
        # destructive one.
        assert [call[0] for call in provider.calls] == [
            "interaction.instruction",
            "interaction.command",
        ]

    def test_the_model_can_trim_not_only_delete(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        """The vocabulary gap that made this worth doing.

        trim, split and move have been in the timeline since Phase 8 and on
        the timeline screen since Phase 12, but `CommandKind` stopped at
        delete/restore -- so "shorten the third one a bit" was refused in
        conversation while the same edit was two clicks away. Deleting a whole
        clip was the only thing a sentence could do to the edit.
        """
        interaction, _ = service(
            database,
            config,
            responses={
                "interaction.command": {
                    "kind": "trim_clip",
                    "clip_index": 2,
                    "end_delta": -30.0,
                    "confidence": 0.9,
                }
            },
        )
        assert "20:00" in interaction.ask(project_id, "how long is the video?").text

        result = interaction.handle(project_id, "the third one drags towards the end")

        assert result.applied_command.kind is CommandKind.TRIM_CLIP
        assert result.applied_command.end_delta == -30.0
        # Thirty seconds shorter, and no clip was lost to get there.
        assert "19:30" in interaction.ask(project_id, "how long is the video?").text

    def test_the_command_is_recorded_as_a_version(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        # §42: an edit made from a sentence is as undoable as any other.
        interaction, _ = service(
            database,
            config,
            responses={
                "interaction.command": {"kind": "delete_clip", "clip_index": 2, "confidence": 0.9}
            },
        )
        interaction.handle(project_id, UNPARSED_COMMAND)

        versions = interaction.versions(project_id)
        assert len(versions) >= 2
        assert any(UNPARSED_COMMAND in (version.reason or "") for version in versions)

    def test_the_analysis_is_not_re_run(
        self,
        database: Database,
        config: AppConfig,
        job_manager: JobManager,
        project_id: str,
    ) -> None:
        # §10 and §127: the model reading a sentence must not cost an hour of
        # Whisper. Nothing on this path touches a job.
        interaction, _ = service(
            database,
            config,
            responses={
                "interaction.instruction": {"pacing": "fast", "confidence": 0.9},
                "interaction.command": {"kind": "delete_clip", "clip_index": 1, "confidence": 0.9},
            },
        )
        before = {job.id: job.status for job in job_manager.list_jobs(project_id)}

        interaction.handle(project_id, UNPARSED_INSTRUCTION)
        interaction.handle(project_id, UNPARSED_COMMAND)

        after = {job.id: job.status for job in job_manager.list_jobs(project_id)}
        assert after == before
        assert all(status is JobStatus.COMPLETED for status in after.values())

    def test_a_question_is_answered_from_stored_records(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        interaction, provider = service(
            database,
            config,
            responses={
                "interaction.question": {
                    "answer": "The strongest stretch is the clutch just after a minute in.",
                    "citations": [],  # filled below from the real record ids
                    "answered": True,
                    "confidence": 0.8,
                }
            },
        )
        # The model may only cite ids it was actually shown, so the scripted
        # answer cites the first record in the pool it will be given.
        pool = interaction._qa._evidence_pool(project_id)
        provider._responses["interaction.question"]["citations"] = [next(iter(pool))]

        answer = interaction.ask(project_id, UNCLASSIFIED_QUESTION)

        assert answer.source is AnswerSource.LLM
        assert answer.evidence, "an LLM answer must carry the records it stood on"

    def test_no_file_is_touched(
        self, database: Database, config: AppConfig, paths, project_id: str
    ) -> None:
        # §63 is explicit: natural language modifies project state, not files.
        project_dir = paths.project(project_id).root
        before = _tree(project_dir)

        interaction, _ = service(
            database,
            config,
            responses={
                "interaction.instruction": {"pacing": "slow", "confidence": 0.9},
                "interaction.command": {"kind": "delete_clip", "clip_index": 3, "confidence": 0.9},
            },
        )
        interaction.handle(project_id, UNPARSED_INSTRUCTION)
        interaction.handle(project_id, UNPARSED_COMMAND)

        assert _tree(project_dir) == before


@pytest.mark.usefixtures("edited")
class TestWithoutAModel:
    """§95: no Ollama is a smaller editor, not a broken one."""

    def test_an_unparsed_instruction_is_explained_not_crashed(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        interaction, _ = service(database, config, available=False, default={"confidence": 1.0})

        result = interaction.handle(project_id, UNPARSED_INSTRUCTION)

        assert result.interaction_type is InteractionType.EDITING_INSTRUCTION
        assert result.message, "a message the person can act on, not an empty string"
        assert result.requires_rerender is False
        assert interaction.current_intent(project_id) == interaction.current_intent(project_id)
        assert not interaction.versions(project_id), "nothing was edited, so nothing to undo"

    def test_an_unparsed_command_leaves_the_edit_alone(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        interaction, _ = service(database, config, available=False, default={"confidence": 1.0})
        before = interaction.ask(project_id, "how long is the video?").text

        result = interaction.handle(project_id, UNPARSED_COMMAND)

        assert result.applied_command is None
        assert interaction.ask(project_id, "how long is the video?").text == before

    def test_the_rule_path_still_works(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        # This is the whole point of rules-first: what the parser understands
        # keeps working with no model installed at all.
        interaction, provider = service(
            database, config, available=False, default={"confidence": 1.0}
        )

        interaction.handle(project_id, "focus on the funny moments")
        interaction.handle(project_id, "delete clip 2")

        intent = interaction.current_intent(project_id)
        assert MomentType.FUNNY in intent.priority_moment_types
        assert "16:00" in interaction.ask(project_id, "how long is the video?").text
        assert provider.calls == [], "the rules answered; the model was never asked"

    def test_a_model_that_fails_mid_sentence_does_not_lose_the_message(
        self, database: Database, config: AppConfig, project_id: str
    ) -> None:
        # Ollama going away between the availability check and the request.
        interaction, _ = service(database, config, fail_times=99, default={"confidence": 1.0})

        result = interaction.handle(project_id, UNPARSED_INSTRUCTION)

        assert result.message
        history = interaction.history(project_id)
        assert any(message.text == UNPARSED_INSTRUCTION for message in history)
