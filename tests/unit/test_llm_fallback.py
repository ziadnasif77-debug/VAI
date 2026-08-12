"""Phase 13: reading the sentences the rules could not (SPEC §63, §85, §93–§95).

The model is the last thing added to this pipeline and the least trusted. These
tests are mostly about what happens when it is **wrong** — unavailable,
unconfident, answering with prose, citing records that do not exist, asking for
a clip that is not there — because those paths decide whether a bad answer
reaches someone's video.

The happy path matters too, and is one test: an instruction the rule parser
rejects becomes a validated `IntentDelta`. Everything else here is about the
guard rails around it.
"""

from __future__ import annotations

import pytest

from ai.llm.fake_provider import FakeLLMProvider
from backend.config.loader import load_config
from backend.core.models.enums import MomentType
from backend.interaction.llm_fallback import MIN_CONFIDENCE, LlmInterpreter
from backend.interaction.models import (
    Answer,
    AnswerSource,
    CommandKind,
    EditCommand,
    EditingIntent,
    Evidence,
    EvidenceKind,
    IntentDelta,
)
from backend.interaction.parser import parse_instruction

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def config():
    return load_config()


def _interpreter(config, **kwargs) -> tuple[LlmInterpreter, FakeLLMProvider]:
    provider = FakeLLMProvider(**kwargs)
    return LlmInterpreter(config, provider), provider


def _evidence() -> dict[str, Evidence]:
    return {
        "m1": Evidence(
            kind=EvidenceKind.MOMENT,
            id="mom-000000000001",
            start_seconds=642.0,
            end_seconds=667.0,
            detail="funny -- 3 detectors agreed",
            score=0.74,
        ),
        "e1": Evidence(
            kind=EvidenceKind.GAME_EVENT,
            id="evt-000000000001",
            start_seconds=650.0,
            end_seconds=652.0,
            detail="unexpected_event detected by audio, scene",
            score=0.6,
        ),
    }


class TestInstructions:
    """§63: an instruction the rules cannot read still changes the brief."""

    def test_the_rule_parser_really_does_reject_the_test_sentence(self) -> None:
        # Otherwise the rest of this class proves nothing: a sentence the rules
        # already understand would never reach the model.
        parsed = parse_instruction("give it the feel of a wildlife documentary")

        assert parsed.confidence == 0.0

    def test_an_unparsed_instruction_becomes_a_validated_delta(self, config) -> None:
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.instruction": {
                    "pacing": "slow",
                    "effects": "minimal",
                    "confidence": 0.8,
                }
            },
        )

        reading = interpreter.read_instruction(
            "give it the feel of a wildlife documentary", EditingIntent()
        )

        assert isinstance(reading.value, IntentDelta)
        assert reading.value.pacing is not None
        assert reading.value.effects is not None

    def test_the_model_is_told_the_current_preferences(self, config) -> None:
        # "A bit faster" means nothing without knowing the current pace.
        interpreter, provider = _interpreter(
            config, responses={"interaction.instruction": {"confidence": 0.1}}
        )

        interpreter.read_instruction("a bit faster", EditingIntent())

        _, prompt = provider.calls[0]
        assert "pacing:" in prompt
        assert "effects:" in prompt

    def test_the_model_is_told_which_moment_types_exist(self, config) -> None:
        interpreter, provider = _interpreter(
            config, responses={"interaction.instruction": {"confidence": 0.1}}
        )

        interpreter.read_instruction("favour the funny bits", EditingIntent())

        _, prompt = provider.calls[0]
        for moment_type in (MomentType.CLUTCH, MomentType.FUNNY):
            assert moment_type.value in prompt

    def test_an_unsupported_instruction_is_refused_rather_than_approximated(self, config) -> None:
        # Silently applying the nearest available preference would leave the
        # person believing their instruction was followed.
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.instruction": {
                    "confidence": 0.9,
                    "unsupported": "there is no setting for colour grading",
                }
            },
        )

        reading = interpreter.read_instruction("make it more teal and orange", EditingIntent())

        assert not reading.understood
        assert "colour grading" in reading.reason

    def test_a_low_confidence_reading_is_not_applied(self, config) -> None:
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.instruction": {
                    "pacing": "fast",
                    "confidence": MIN_CONFIDENCE - 0.01,
                }
            },
        )

        reading = interpreter.read_instruction("hmm", EditingIntent())

        assert not reading.understood
        assert "confident" in reading.reason

    def test_a_value_outside_the_model_is_rejected(self, config) -> None:
        # §94: the answer becomes the same validated object the rule parser
        # produces, or it becomes nothing.
        interpreter, _ = _interpreter(
            config,
            responses={"interaction.instruction": {"pacing": "supersonic", "confidence": 0.9}},
        )

        reading = interpreter.read_instruction("go supersonic", EditingIntent())

        assert not reading.understood
        assert "did not fit" in reading.reason

    def test_a_duration_outside_the_product_band_is_rejected(self, config) -> None:
        # The prompt no longer offers durations at all (see PHASE_13.md: the
        # real model turned "30 seconds" into 3000 and "25 minutes" into 2500).
        # This is the guard for a prompt that reintroduces one: §6 is a product
        # rule, not something a model may read past.
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.instruction": {
                    "target_duration_seconds": 30,
                    "confidence": 0.95,
                }
            },
        )

        reading = interpreter.read_instruction("make it 30 seconds", EditingIntent())

        assert not reading.understood
        assert "10-60 minute range" in reading.reason

    def test_the_prompt_does_not_offer_a_duration_at_all(self) -> None:
        from backend.core.prompts import load_prompt

        schema = load_prompt("interaction.instruction").output_schema
        assert "target_duration_seconds" not in schema["properties"]


class TestCommands:
    """§85: a sentence reaches an edit only through the validated command."""

    def test_an_unparsed_command_becomes_a_validated_command(self, config) -> None:
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.command": {
                    "kind": "delete_clip",
                    "clip_index": 3,
                    "confidence": 0.9,
                }
            },
        )

        reading = interpreter.read_command(
            "get rid of the third one", clip_count=5, duration="10:24"
        )

        assert isinstance(reading.value, EditCommand)
        assert reading.value.kind is CommandKind.DELETE_CLIP
        assert reading.value.clip_index == 3

    def test_the_original_text_is_kept_on_the_command(self, config) -> None:
        # §80: the edit history should say what was actually asked for.
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.command": {"kind": "delete_clip", "clip_index": 1, "confidence": 0.9}
            },
        )

        reading = interpreter.read_command("drop the opener", clip_count=5, duration="10:24")

        assert reading.value.raw_text == "drop the opener"

    def test_a_trim_keeps_its_amount(self, config) -> None:
        # The reading is only useful if the numbers arrive with it: a
        # trim_clip whose deltas were dropped on the way through is a
        # zero-second trim, which applies cleanly and does nothing.
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.command": {
                    "kind": "trim_clip",
                    "clip_index": 2,
                    "start_delta": 1.5,
                    "end_delta": -4.0,
                    "confidence": 0.9,
                }
            },
        )

        reading = interpreter.read_command(
            "tighten both ends of #2", clip_count=5, duration="10:24"
        )

        assert reading.value.kind is CommandKind.TRIM_CLIP
        assert reading.value.start_delta == 1.5
        assert reading.value.end_delta == -4.0

    def test_a_move_keeps_its_destination(self, config) -> None:
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.command": {
                    "kind": "move_clip",
                    "clip_index": 4,
                    "to_index": 1,
                    "confidence": 0.9,
                }
            },
        )

        reading = interpreter.read_command(
            "put the last one near the top", clip_count=5, duration="10:24"
        )

        assert reading.value.kind is CommandKind.MOVE_CLIP
        assert reading.value.to_index == 1

    def test_a_clip_that_does_not_exist_is_refused(self, config) -> None:
        # The prompt says so and the model may still do it, so it is a rule
        # here rather than an instruction there.
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.command": {"kind": "delete_clip", "clip_index": 99, "confidence": 0.95}
            },
        )

        reading = interpreter.read_command("delete the last one", clip_count=5, duration="10:24")

        assert not reading.understood
        assert "no clip 99" in reading.reason

    def test_none_is_a_real_answer(self, config) -> None:
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.command": {
                    "kind": "none",
                    "confidence": 0.9,
                    "reason": "that is a question, not an edit",
                }
            },
        )

        reading = interpreter.read_command("what is the best bit?", clip_count=5, duration="10:24")

        assert not reading.understood
        assert "question" in reading.reason

    def test_a_low_confidence_command_is_not_applied(self, config) -> None:
        # A wrong edit costs more than a clarifying question.
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.command": {
                    "kind": "delete_clip",
                    "clip_index": 2,
                    "confidence": 0.2,
                }
            },
        )

        reading = interpreter.read_command("maybe lose one", clip_count=5, duration="10:24")

        assert not reading.understood

    def test_the_model_is_told_how_many_clips_there_are(self, config) -> None:
        interpreter, provider = _interpreter(
            config, responses={"interaction.command": {"kind": "none", "confidence": 0.9}}
        )

        interpreter.read_command("remove the last clip", clip_count=7, duration="9:13")

        _, prompt = provider.calls[0]
        assert "7 clips" in prompt
        assert "9:13" in prompt


class TestQuestions:
    """§80: an answer that cites nothing real is not an answer."""

    def test_a_grounded_answer_carries_its_evidence(self, config) -> None:
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.question": {
                    "answer": "The funniest moment is at 10:42, where three detectors agreed.",
                    "citations": ["m1"],
                    "answered": True,
                    "confidence": 0.8,
                }
            },
        )

        reading = interpreter.answer_question("what was the funniest bit?", _evidence())

        assert isinstance(reading.value, Answer)
        assert reading.value.source is AnswerSource.LLM
        assert [item.id for item in reading.value.evidence] == ["mom-000000000001"]

    def test_an_invented_citation_does_not_resolve(self, config) -> None:
        # An answer standing entirely on invented citations arrives with no
        # evidence, and is refused by the same rule every other claim obeys.
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.question": {
                    "answer": "There is a triple kill at 4:20.",
                    "citations": ["m99", "nonsense"],
                    "answered": True,
                    "confidence": 0.9,
                }
            },
        )

        reading = interpreter.answer_question("was there a triple kill?", _evidence())

        assert not reading.understood
        assert "cite" in reading.reason

    def test_an_answer_citing_nothing_is_refused(self, config) -> None:
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.question": {
                    "answer": "Probably around the middle.",
                    "citations": [],
                    "answered": True,
                    "confidence": 0.7,
                }
            },
        )

        assert not interpreter.answer_question("when?", _evidence()).understood

    def test_the_model_declining_is_passed_through(self, config) -> None:
        interpreter, _ = _interpreter(
            config,
            responses={
                "interaction.question": {
                    "answer": "The analysis does not show any vehicle sections.",
                    "answered": False,
                    "confidence": 0.6,
                }
            },
        )

        reading = interpreter.answer_question("how many car chases?", _evidence())

        assert not reading.understood
        assert "does not show" in reading.reason

    def test_no_records_means_no_question_is_asked(self, config) -> None:
        interpreter, provider = _interpreter(
            config, responses={"interaction.question": {"answer": "x", "confidence": 0.9}}
        )

        reading = interpreter.answer_question("anything?", {})

        assert not reading.understood
        assert provider.calls == [], "the model should not be asked with nothing to cite"

    def test_the_records_reach_the_prompt_with_their_ids(self, config) -> None:
        interpreter, provider = _interpreter(
            config,
            responses={
                "interaction.question": {"answer": "x", "answered": False, "confidence": 0.5}
            },
        )

        interpreter.answer_question("what happened?", _evidence())

        _, prompt = provider.calls[0]
        assert "[m1]" in prompt
        assert "10:42" in prompt


class TestDegradation:
    """§95: no model is a smaller product, not a broken one."""

    def test_an_unavailable_model_is_reported_rather_than_raised(self, config) -> None:
        interpreter, _ = _interpreter(config, available=False, default={"confidence": 1.0})

        reading = interpreter.read_instruction("anything", EditingIntent())

        assert not reading.understood
        assert not reading.consulted
        assert "no language model" in reading.reason

    def test_a_disabled_fallback_never_asks(self, config) -> None:
        narrowed = config.model_copy(
            update={
                "interaction": config.interaction.model_copy(
                    update={
                        "llm_fallback": config.interaction.llm_fallback.model_copy(
                            update={"enabled": False}
                        )
                    }
                )
            }
        )
        provider = FakeLLMProvider(default={"confidence": 1.0})
        interpreter = LlmInterpreter(narrowed, provider)

        reading = interpreter.read_instruction("anything", EditingIntent())

        assert not reading.understood
        assert provider.calls == []
        assert "switched off" in reading.reason

    def test_a_model_that_fails_leaves_the_rule_path_standing(self, config) -> None:
        interpreter, _ = _interpreter(config, fail_times=99, default={"confidence": 1.0})

        reading = interpreter.read_instruction("anything", EditingIntent())

        assert not reading.understood
        assert reading.consulted

    def test_availability_is_only_checked_once(self, config) -> None:
        # An HTTP round trip per message would be the slowest part of an
        # interaction that is otherwise instant.
        interpreter, provider = _interpreter(
            config, responses={"interaction.instruction": {"confidence": 0.1}}
        )

        for _ in range(3):
            interpreter.read_instruction("something", EditingIntent())

        assert len(provider.calls) == 3
        assert interpreter.available() is True

    def test_a_failure_clears_the_cached_availability(self, config) -> None:
        # The model going away mid-session should be noticed, not assumed away.
        interpreter, _ = _interpreter(config, fail_times=99, default={"confidence": 1.0})
        interpreter.read_instruction("first", EditingIntent())

        assert interpreter._checked_availability is None
