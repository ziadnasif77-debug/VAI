"""Interaction layer tests (SPEC §2-§20, §26).

The §26 test list is covered here: a question answered from stored data, an
instruction that changes intent, a command that changes the EDL, a follow-up
that refines rather than replaces, a refusal before analysis exists, and a
guarantee that editing never invalidates analysis.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, ValidationError
from backend.core.ids import new_id
from backend.core.models.enums import (
    GameEventType,
    JobStage,
    JobStatus,
    MomentType,
    VideoMode,
)
from backend.core.models.media import Media
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.database.repositories.media import MediaRepository
from backend.interaction.intent import IntentResolver
from backend.interaction.knowledge import VideoKnowledgeBase
from backend.interaction.models import (
    AnswerSource,
    CommandKind,
    DeadTimePolicy,
    EditCommand,
    EditingIntent,
    EffectsLevel,
    IntentDelta,
    InteractionType,
    Level,
    ListDelta,
    Pacing,
)
from backend.interaction.parser import (
    classify,
    parse_command,
    parse_instruction,
    shift_effects,
    shift_pacing,
)
from backend.interaction.qa import QuestionKind
from backend.interaction.service import InteractionService
from backend.services.job_manager import JobManager
from backend.services.project_manager import ProjectManager
from tests.support.analysed_project import (
    complete_analysis,
    insert_clip,
    insert_event,
    insert_moment,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures: a project with analysis results already stored.
# ---------------------------------------------------------------------------


@pytest.fixture
def project_id(project_manager: ProjectManager) -> str:
    return project_manager.create(
        ProjectCreate(
            name="Ranked session",
            target_duration_seconds=1200,
            mode=VideoMode.BEST_MOMENTS,
            game="valorant",
        )
    ).id


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
def interaction(database: Database, config: AppConfig) -> InteractionService:
    return InteractionService(database, config)


@pytest.fixture
def knowledge(database: Database) -> VideoKnowledgeBase:
    return VideoKnowledgeBase(database)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        "text",
        [
            "ما أفضل لحظة؟",
            "what was the best moment?",
            "كم مرة مت فيها",
            "why did you pick this clip?",
            "how long is the video",
        ],
    )
    def test_questions(self, text: str) -> None:
        assert classify(text) is InteractionType.QUESTION

    @pytest.mark.parametrize(
        "text",
        ["delete clip 5", "احذف لقطة 7", "remove the clip at 12:34", "revert to version 2"],
    )
    def test_commands(self, text: str) -> None:
        assert classify(text) is InteractionType.COMMAND

    @pytest.mark.parametrize(
        "text",
        [
            "ركز على الـclutch والـkills",
            "focus on clutches",
            "make it cinematic",
            "لا تستخدم الكثير من الـeffects",
            "احذف التكرار واللحظات المملة",
        ],
    )
    def test_editing_instructions(self, text: str) -> None:
        assert classify(text) is InteractionType.EDITING_INSTRUCTION

    def test_delete_without_a_target_is_an_instruction_not_a_command(self) -> None:
        # "remove the boring parts" must not be read as a destructive command.
        assert classify("remove the boring parts") is InteractionType.EDITING_INSTRUCTION


class TestCommandParsing:
    def test_delete_clip_by_index(self) -> None:
        command = parse_command("delete clip 5")
        assert command is not None
        assert command.kind is CommandKind.DELETE_CLIP
        assert command.clip_index == 5

    def test_delete_clip_arabic(self) -> None:
        command = parse_command("احذف لقطة ٧")
        assert command is not None
        assert command.clip_index == 7

    def test_delete_at_timestamp(self) -> None:
        command = parse_command("delete the clip at 12:34")
        assert command is not None
        assert command.kind is CommandKind.DELETE_AT_TIMESTAMP
        assert command.timestamp_seconds == 754

    def test_revert_version(self) -> None:
        command = parse_command("revert to version 2")
        assert command is not None
        assert command.kind is CommandKind.REVERT_VERSION
        assert command.version == 2

    def test_vague_text_is_not_a_command(self) -> None:
        assert parse_command("make it better") is None

    def test_command_requires_its_argument(self) -> None:
        # A command with no target must not reach the applier.
        with pytest.raises(PydanticValidationError, match="requires one of"):
            EditCommand(kind=CommandKind.DELETE_CLIP)


class TestInstructionParsing:
    def test_focus_on_clutch(self) -> None:
        parsed = parse_instruction("ركز على الـclutch")
        assert parsed.delta.priority_moment_types is not None
        assert "clutch" in parsed.delta.priority_moment_types.add
        assert parsed.confidence > 0

    def test_remove_dead_time(self) -> None:
        parsed = parse_instruction("احذف الوقت الميت واللحظات المملة")
        assert parsed.delta.dead_time_policy is DeadTimePolicy.AGGRESSIVE

    def test_preserve_context(self) -> None:
        parsed = parse_instruction("حافظ على السياق قبل كل حدث مهم")
        assert parsed.delta.context_preservation is Level.HIGH

    def test_fewer_effects_is_relative(self) -> None:
        parsed = parse_instruction("لا تستخدم الكثير من الـeffects")
        assert parsed.effects_shift == -1
        assert parsed.delta.effects is None

    def test_no_effects_is_absolute(self) -> None:
        parsed = parse_instruction("no effects at all")
        assert parsed.delta.effects is EffectsLevel.NONE

    def test_explicit_duration(self) -> None:
        parsed = parse_instruction("أريد الفيديو حوالي 25 دقيقة")
        assert parsed.delta.target_duration_seconds == 1500

    def test_english_duration(self) -> None:
        assert parse_instruction("make it 30 minutes").delta.target_duration_seconds == 1800

    def test_shorter_is_relative(self) -> None:
        parsed = parse_instruction("أريد الفيديو أقصر")
        assert parsed.duration_multiplier is not None
        assert parsed.duration_multiplier < 1
        assert parsed.delta.target_duration_seconds is None

    def test_faster_is_relative(self) -> None:
        assert parse_instruction("اجعله أسرع").pacing_shift == 1

    def test_cinematic_sets_a_coherent_group(self) -> None:
        parsed = parse_instruction("أريد الفيديو cinematic")
        assert parsed.delta.style == "cinematic"
        assert parsed.delta.pacing is Pacing.SLOW
        assert parsed.delta.effects is EffectsLevel.MINIMAL

    def test_captions_important_only(self) -> None:
        parsed = parse_instruction("لا تستخدم captions إلا عندما تكون مهمة")
        assert parsed.delta.captions is not None
        assert parsed.delta.captions.value == "important_only"

    def test_negated_moment_type_is_avoided(self) -> None:
        parsed = parse_instruction("no fails please")
        assert parsed.delta.avoid_moment_types is not None
        assert "fail" in parsed.delta.avoid_moment_types.add

    def test_multi_kill_wins_over_kill(self) -> None:
        parsed = parse_instruction("focus on multi-kills")
        assert parsed.delta.priority_events is not None
        assert GameEventType.MULTI_KILL.value in parsed.delta.priority_events.add
        assert GameEventType.KILL.value not in parsed.delta.priority_events.add

    def test_unrecognised_text_reports_zero_confidence(self) -> None:
        parsed = parse_instruction("hello there")
        assert parsed.confidence == 0.0
        assert parsed.is_empty()


class TestScales:
    def test_pacing_shift_clamps(self) -> None:
        assert shift_pacing(Pacing.VERY_FAST, 1) is Pacing.VERY_FAST
        assert shift_pacing(Pacing.SLOW, -1) is Pacing.SLOW
        assert shift_pacing(Pacing.MEDIUM, 1) is Pacing.FAST

    def test_effects_shift_clamps(self) -> None:
        assert shift_effects(EffectsLevel.NONE, -1) is EffectsLevel.NONE
        assert shift_effects(EffectsLevel.HEAVY, 1) is EffectsLevel.HEAVY


# ---------------------------------------------------------------------------
# Intent resolution
# ---------------------------------------------------------------------------


class TestIntentResolution:
    def test_every_preset_resolves(self, config: AppConfig) -> None:
        resolver = IntentResolver(config)
        for name in config.presets:
            assert resolver.base_intent(name).preset == name

    def test_unknown_preset_is_rejected(self, config: AppConfig) -> None:
        with pytest.raises(ValidationError) as exc_info:
            IntentResolver(config).preset("does_not_exist")
        assert exc_info.value.code is ErrorCode.BUSINESS_VALIDATION_FAILED

    def test_works_with_no_instructions_at_all(self, config: AppConfig) -> None:
        # §1: the system must produce a video without the user writing anything.
        intent = IntentResolver(config).resolve(project_target_duration_seconds=1200)
        assert intent.target_duration_seconds == 1200
        assert intent.preset == config.interaction.default_preset

    def test_project_duration_beats_the_preset(self, config: AppConfig) -> None:
        intent = IntentResolver(config).resolve(
            preset_name="cinematic", project_target_duration_seconds=1500
        )
        assert intent.target_duration_seconds == 1500

    def test_instruction_beats_the_project_setting(self, config: AppConfig) -> None:
        from backend.interaction.models import IntentSource, IntentUpdate

        resolver = IntentResolver(config)
        intent = resolver.resolve(
            updates=[
                IntentUpdate(
                    sequence=0,
                    source=IntentSource.INSTRUCTION,
                    delta=IntentDelta(target_duration_seconds=1800),
                )
            ],
            project_target_duration_seconds=1200,
        )
        assert intent.target_duration_seconds == 1800

    def test_list_delta_adds_without_replacing(self, config: AppConfig) -> None:
        resolver = IntentResolver(config)
        base = resolver.base_intent("funny_gaming")
        before = set(base.priority_moment_types)
        after = resolver.apply(
            base, IntentDelta(priority_moment_types=ListDelta(add=["clutch"]))
        )
        assert before < set(after.priority_moment_types)
        assert MomentType.CLUTCH in after.priority_moment_types

    def test_list_delta_set_replaces(self, config: AppConfig) -> None:
        resolver = IntentResolver(config)
        base = resolver.base_intent("funny_gaming")
        after = resolver.apply(
            base, IntentDelta(priority_moment_types=ListDelta(set=["clutch"]))
        )
        assert [item.value for item in after.priority_moment_types] == ["clutch"]

    def test_newest_instruction_wins_a_priority_conflict(self, config: AppConfig) -> None:
        resolver = IntentResolver(config)
        base = resolver.apply(
            resolver.base_intent("best_moments"),
            IntentDelta(avoid_moment_types=ListDelta(add=["fail"])),
        )
        assert MomentType.FAIL in base.avoid_moment_types

        after = resolver.apply(
            base, IntentDelta(priority_moment_types=ListDelta(add=["fail"]))
        )
        assert MomentType.FAIL in after.priority_moment_types
        assert MomentType.FAIL not in after.avoid_moment_types

    def test_intent_rejects_a_contradiction(self) -> None:
        with pytest.raises(PydanticValidationError, match="prioritised and avoided"):
            EditingIntent(
                priority_moment_types=[MomentType.CLUTCH],
                avoid_moment_types=[MomentType.CLUTCH],
            )


# ---------------------------------------------------------------------------
# Conversation: accumulation, questions, commands
# ---------------------------------------------------------------------------


class TestConversationAccumulation:
    """§11: each instruction refines the brief instead of resetting it."""

    def test_follow_ups_compose(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        interaction.handle(project_id, "اعمل فيديو سريع")
        interaction.handle(project_id, "ركز أكثر على الـclutch")
        interaction.handle(project_id, "ولا تستخدم كثير effects")
        result = interaction.handle(project_id, "اجعله 25 دقيقة")

        intent = result.intent
        assert intent is not None
        assert intent.pacing is Pacing.FAST
        assert MomentType.CLUTCH in intent.priority_moment_types
        assert intent.effects is not EffectsLevel.HEAVY
        assert intent.target_duration_seconds == 1500

    def test_relative_follow_up_uses_the_current_value(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        interaction.handle(project_id, "make it 30 minutes")
        result = interaction.handle(project_id, "actually make it shorter")
        assert result.intent is not None
        assert result.intent.target_duration_seconds == 1440  # 1800 * 0.8

    def test_shorter_never_leaves_the_supported_band(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        interaction.handle(project_id, "make it 10 minutes")
        result = interaction.handle(project_id, "make it shorter")
        assert result.intent is not None
        assert result.intent.target_duration_seconds >= 600

    def test_history_records_both_sides(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        interaction.handle(project_id, "focus on clutches")
        history = interaction.history(project_id)
        assert [message.role.value for message in history] == ["user", "assistant"]

    def test_reset_returns_to_the_preset(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        interaction.handle(project_id, "make it cinematic")
        after_reset = interaction.reset_intent(project_id)
        assert after_reset.style != "cinematic"

    def test_changing_preset_keeps_instructions(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        interaction.handle(project_id, "ركز على الـclutch")
        intent = interaction.set_preset(project_id, "cinematic")
        assert intent.preset == "cinematic"
        assert MomentType.CLUTCH in intent.priority_moment_types

    def test_out_of_band_duration_is_refused(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            interaction.handle(project_id, "make it 5 minutes")
        assert exc_info.value.code is ErrorCode.INVALID_TARGET_DURATION


class TestQuestionsBeforeAnalysis:
    """§17: no guessing before the data exists."""

    def test_best_moment_is_refused(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        answer = interaction.ask(project_id, "ما أفضل لحظة؟")
        assert answer.requires_analysis is True
        assert answer.confidence == 0.0
        assert answer.source is AnswerSource.UNAVAILABLE
        assert "not complete" in answer.text.lower()

    def test_duration_still_answers(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        # An edit-level question needs no analysis at all.
        answer = interaction.ask(project_id, "how long is the video?")
        assert answer.requires_analysis is False


class TestQuestionsAfterAnalysis:
    @pytest.fixture(autouse=True)
    def _analysed(
        self,
        database: Database,
        job_manager: JobManager,
        project_id: str,
        media_id: str,
    ) -> None:
        complete_analysis(job_manager, project_id, media_id)
        insert_moment(
            database,
            project_id,
            media_id,
            moment_type="clutch",
            start=754.0,
            end=784.0,
            score=0.94,
            explanation=["Clutch event", "Low health", "Multi-kill", "Player reaction"],
            breakdown={"gameplay": 0.95, "reaction": 0.88, "audio": 0.7},
        )
        insert_moment(
            database, project_id, media_id, moment_type="funny", start=200.0, score=0.71
        )
        insert_moment(
            database,
            project_id,
            media_id,
            moment_type="fail",
            start=400.0,
            score=0.30,
            user_state="rejected",
            dead_time=0.6,
        )
        insert_event(database, project_id, media_id, event_type="death", start=300.0)
        insert_event(database, project_id, media_id, event_type="death", start=500.0)
        insert_event(database, project_id, media_id, event_type="kill", start=760.0)

    def test_best_moments(self, interaction: InteractionService, project_id: str) -> None:
        answer = interaction.ask(project_id, "ما أفضل اللحظات في الفيديو؟")
        assert answer.source is AnswerSource.DATABASE
        assert answer.evidence, "an answer must cite its records"
        assert "clutch" in answer.text

    def test_best_of_type(self, interaction: InteractionService, project_id: str) -> None:
        answer = interaction.ask(project_id, "ما أقوى clutch؟")
        assert "clutch" in answer.text
        assert answer.evidence[0].id is not None

    def test_explainability_uses_stored_reasons(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        # §12: reasons come from the data, never invented.
        answer = interaction.ask(project_id, "لماذا اخترت اللقطة عند 12:34؟")
        assert "Clutch event" in answer.text
        assert "Low health" in answer.text
        assert answer.evidence

    def test_what_happened_at_timestamp(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        answer = interaction.ask(project_id, "ماذا حدث عند 12:40؟")
        assert "12:40" in answer.text or "kill" in answer.text
        assert answer.evidence

    def test_quiet_timestamp_says_so(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        answer = interaction.ask(project_id, "what happened at 45:00?")
        assert "nothing was detected" in answer.text.lower()
        assert answer.confidence < 0.6

    def test_counting_events(self, interaction: InteractionService, project_id: str) -> None:
        answer = interaction.ask(project_id, "كم مرة مت فيها؟")
        assert "2" in answer.text

    def test_excluded_moments(self, interaction: InteractionService, project_id: str) -> None:
        answer = interaction.ask(project_id, "ما اللقطات التي استبعدتها؟")
        assert "fail" in answer.text
        assert answer.evidence

    def test_no_model_is_ever_invoked(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        # §6/§20: answering must not re-analyse, and must not need an LLM.
        for question in [
            "ما أفضل لحظة؟",
            "كم مرة مت فيها؟",
            "ماذا حدث عند 12:40؟",
            "how long is the video?",
        ]:
            assert interaction.ask(project_id, question).source in {
                AnswerSource.DATABASE,
                AnswerSource.UNAVAILABLE,
            }

    def test_question_kind_classification(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        qa = interaction._qa
        assert qa.classify("كم مرة مت فيها؟") is QuestionKind.COUNT_EVENTS
        assert qa.classify("ماذا حدث عند 12:34؟") is QuestionKind.WHAT_HAPPENED_AT
        assert qa.classify("لماذا اخترت هذه اللقطة؟") is QuestionKind.WHY_THIS_CLIP
        assert qa.classify("ما مدة الفيديو؟") is QuestionKind.EDIT_DURATION


class TestEditCommands:
    """§9, §10, §18, §19."""

    @pytest.fixture(autouse=True)
    def _edited(
        self,
        database: Database,
        job_manager: JobManager,
        project_id: str,
        media_id: str,
    ) -> None:
        complete_analysis(job_manager, project_id, media_id)
        for index in range(5):
            insert_clip(
                database,
                project_id,
                media_id,
                index=index,
                timeline_start=index * 120.0,
                duration=120.0,
            )

    def test_duration_comes_from_the_edl(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        answer = interaction.ask(project_id, "ما مدة الفيديو الحالية؟")
        assert "10:00" in answer.text

    def test_delete_clip_shortens_the_edit(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        result = interaction.handle(project_id, "delete clip 2")
        assert result.applied_command is not None
        assert result.requires_rerender is True

        answer = interaction.ask(project_id, "how long is the video?")
        assert "8:00" in answer.text

    def test_delete_at_timestamp(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        result = interaction.handle(project_id, "delete the clip at 4:10")
        assert result.applied_command is not None
        assert result.applied_command.kind is CommandKind.DELETE_AT_TIMESTAMP

    def test_deleting_a_missing_clip_is_refused(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            interaction.handle(project_id, "delete clip 99")
        assert exc_info.value.code is ErrorCode.INVALID_TIMELINE_OPERATION

    def test_editing_does_not_invalidate_analysis(
        self,
        interaction: InteractionService,
        job_manager: JobManager,
        project_id: str,
    ) -> None:
        # §10: modifying the EDL must never re-queue Whisper, vision or OCR.
        before = {
            job.stage: job.status for job in job_manager.list_jobs(project_id)
        }
        interaction.handle(project_id, "delete clip 1")
        after = {job.stage: job.status for job in job_manager.list_jobs(project_id)}

        assert after == before
        assert after[JobStage.TRANSCRIPT] is JobStatus.COMPLETED
        assert after[JobStage.VISION] is JobStatus.COMPLETED

    def test_versions_are_recorded(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        interaction.handle(project_id, "delete clip 1")
        versions = interaction.versions(project_id)
        assert len(versions) >= 2

    def test_revert_restores_the_previous_edit(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        # §19: going back costs no analysis.
        interaction.handle(project_id, "delete clip 1")
        shortened = interaction.ask(project_id, "how long is the video?").text
        assert "8:00" in shortened

        first_version = interaction.versions(project_id)[-1].version
        interaction.apply_command(
            project_id, EditCommand(kind=CommandKind.REVERT_VERSION, version=first_version)
        )
        assert "10:00" in interaction.ask(project_id, "how long is the video?").text

    def test_unparsable_command_does_not_change_anything(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        result = interaction.handle(project_id, "delete something please")
        assert result.applied_command is None
        assert result.requires_rerender is False


class TestNoEditNoCrash:
    def test_command_without_an_edit_is_refused(
        self, interaction: InteractionService, project_id: str, media_id: str
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            interaction.apply_command(
                project_id, EditCommand(kind=CommandKind.DELETE_CLIP, clip_index=0)
            )
        assert exc_info.value.code is ErrorCode.INVALID_TIMELINE_OPERATION

    def test_duration_question_without_an_edit(
        self, interaction: InteractionService, project_id: str
    ) -> None:
        answer = interaction.ask(project_id, "how long is the video?")
        assert "no edit" in answer.text.lower()


class TestKnowledgeBase:
    def test_readiness_reflects_completed_stages(
        self,
        knowledge: VideoKnowledgeBase,
        job_manager: JobManager,
        project_id: str,
        media_id: str,
    ) -> None:
        assert knowledge.readiness(project_id).is_complete is False
        complete_analysis(job_manager, project_id, media_id)
        readiness = knowledge.readiness(project_id)
        assert readiness.is_complete is True
        assert readiness.has_transcript is True

    def test_moment_type_counts(
        self, knowledge: VideoKnowledgeBase, database: Database, project_id: str, media_id: str
    ) -> None:
        insert_moment(database, project_id, media_id, moment_type="clutch")
        insert_moment(database, project_id, media_id, moment_type="clutch", start=300)
        insert_moment(database, project_id, media_id, moment_type="funny", start=500)
        assert knowledge.moment_type_counts(project_id) == {"clutch": 2, "funny": 1}

    def test_rejected_moments_are_excluded_from_top(
        self, knowledge: VideoKnowledgeBase, database: Database, project_id: str, media_id: str
    ) -> None:
        insert_moment(database, project_id, media_id, score=0.99, user_state="rejected")
        insert_moment(database, project_id, media_id, score=0.5, start=300)
        top = knowledge.top_moments(project_id)
        assert len(top) == 1
        assert top[0].score == 0.5


def _row_count(database: Database, table: str, project_id: str) -> Any:
    row = database.fetch_one(f"SELECT COUNT(*) AS total FROM {table} WHERE project_id = ?", (project_id,))
    return int(row["total"])
