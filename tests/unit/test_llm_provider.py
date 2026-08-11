"""The language-model provider (SPEC §92–§95).

Two things are worth testing here and the model is neither of them.

**Reject, retry, then fail (§94).** A local model asked for JSON will sometimes
answer with an apology, a code fence, or a JSON array. Everything above this
layer assumes it received an object with the required keys, so the tests below
feed the parser what a real model actually returns when it goes wrong.

**The prompts are versioned and loadable (§92).** A prompt that fails to load,
or a schema that drifts from the fields the code reads, breaks the natural-
language path at the moment someone types a sentence -- so it is checked here
rather than discovered there.

The transport is stubbed. This is a unit test, and an assertion about Ollama
being installed belongs in the health check, not in the suite.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from ai.llm import LLM_PROVIDERS, create_llm_provider
from ai.llm.fake_provider import FakeLLMProvider
from ai.llm.ollama_provider import MAX_ATTEMPTS, OllamaLLMProvider, _parse
from backend.config.loader import load_config
from backend.core.errors import ErrorCode, ModelError, ValidationError
from backend.core.models.enums import MomentType, VideoMode
from backend.core.prompts import load_prompt
from backend.core.versions import PROMPT_VERSIONS
from backend.interaction.models import (
    CaptionPolicy,
    DeadTimePolicy,
    EffectsLevel,
    Level,
    MusicPolicy,
    Pacing,
)

pytestmark = pytest.mark.unit

INTERACTION_PROMPTS = (
    "interaction.instruction",
    "interaction.command",
    "interaction.question",
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["confidence"],
}


@pytest.fixture(scope="module")
def config():
    return load_config()


class TestParsing:
    """§94: what a caller receives is an object, or an error. Never prose."""

    def test_a_conforming_object_passes(self) -> None:
        assert _parse('{"confidence": 0.8}', SCHEMA) == {"confidence": 0.8}

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert _parse('\n  {"confidence": 0.8}  \n', SCHEMA) == {"confidence": 0.8}

    @pytest.mark.parametrize(
        "response",
        [
            "",
            "   ",
            "I'm sorry, I can't help with that.",
            '```json\n{"confidence": 0.8}\n```',
            '{"confidence": 0.8',
        ],
    )
    def test_anything_that_is_not_json_is_rejected(self, response: str) -> None:
        with pytest.raises(ValidationError) as error:
            _parse(response, SCHEMA)
        assert error.value.code is ErrorCode.LLM_INVALID_JSON

    @pytest.mark.parametrize("response", ['["a", "b"]', '"just a string"', "42", "null"])
    def test_json_that_is_not_an_object_is_rejected(self, response: str) -> None:
        # A model asked for one answer sometimes returns a list of them.
        with pytest.raises(ValidationError):
            _parse(response, SCHEMA)

    def test_a_missing_required_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as error:
            _parse('{"answer": "yes"}', SCHEMA)
        assert error.value.code is ErrorCode.SCHEMA_VALIDATION_FAILED
        assert error.value.details["missing"] == ["confidence"]

    def test_extra_fields_are_left_alone(self) -> None:
        # Shallow on purpose: the caller's Pydantic model knows what the values
        # mean, and a second full validation here would be a second place for
        # the rules to drift.
        parsed = _parse('{"confidence": 0.8, "surprise": 1}', SCHEMA)
        assert parsed["surprise"] == 1


class TestRetries:
    """§94: two more tries, then the caller's §95 fallback takes over."""

    def _provider(self, config, responses: list[str]) -> tuple[OllamaLLMProvider, list[str]]:
        provider = OllamaLLMProvider(config.models.llm, gpu=config.gpu)
        seen: list[str] = []

        def fake_post(path: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:
            seen.append(path)
            return {"response": responses[min(len(seen) - 1, len(responses) - 1)]}

        provider._post = fake_post  # type: ignore[assignment]
        return provider, seen

    def test_a_first_good_answer_is_not_retried(self, config) -> None:
        provider, seen = self._provider(config, ['{"confidence": 0.9}'])

        result = provider.complete_json("hello", schema=SCHEMA, prompt_id="test")

        assert result == {"confidence": 0.9}
        assert len(seen) == 1

    def test_prose_then_json_succeeds(self, config) -> None:
        provider, seen = self._provider(config, ["Sure! Here you go.", '{"confidence": 0.7}'])

        assert provider.complete_json("hello", schema=SCHEMA, prompt_id="test") == {
            "confidence": 0.7
        }
        assert len(seen) == 2

    def test_persistent_prose_fails_after_the_attempts(self, config) -> None:
        provider, seen = self._provider(config, ["nope"])

        with pytest.raises(ModelError) as error:
            provider.complete_json("hello", schema=SCHEMA, prompt_id="test")

        assert error.value.code is ErrorCode.LLM_REQUEST_FAILED
        assert len(seen) == MAX_ATTEMPTS
        # The original parse failure is kept, so the log says *why* it failed.
        assert isinstance(error.value.__cause__, ValidationError)

    def test_the_schema_is_sent_to_the_runtime(self, config) -> None:
        # §93: the schema shipped beside the prompt is both the constraint given
        # to Ollama and the contract checked on return -- one definition.
        provider = OllamaLLMProvider(config.models.llm, gpu=config.gpu)
        bodies: list[dict[str, Any]] = []

        def fake_post(path: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:
            bodies.append(body)
            return {"response": '{"confidence": 1.0}'}

        provider._post = fake_post  # type: ignore[assignment]
        provider.complete_json("hello", schema=SCHEMA, prompt_id="test", temperature=0.1)

        assert bodies[0]["format"] == SCHEMA
        assert bodies[0]["stream"] is False
        assert bodies[0]["options"]["temperature"] == 0.1

    def test_unloading_asks_ollama_to_free_the_memory(self, config) -> None:
        # §54: Ollama holds a model for minutes after the last request, which on
        # an 8 GB card is the VLM's memory.
        provider = OllamaLLMProvider(config.models.llm, gpu=config.gpu)
        bodies: list[dict[str, Any]] = []
        provider._post = lambda path, body, *, timeout: bodies.append(body) or {}  # type: ignore

        provider.load()
        provider.unload()

        assert bodies[-1]["keep_alive"] == 0


class TestAvailability:
    """§95: 'is there a model' is answered before anything depends on it."""

    def _tagged(self, config, tags: dict[str, Any] | None) -> OllamaLLMProvider:
        provider = OllamaLLMProvider(config.models.llm, gpu=config.gpu)
        provider._get = lambda path, *, timeout: tags  # type: ignore[assignment]
        return provider

    def test_a_dead_endpoint_is_unavailable(self, config) -> None:
        assert self._tagged(config, None).is_available() is False

    def test_a_running_ollama_without_the_model_is_unavailable(self, config) -> None:
        # The failure this prevents is the worst-timed one: Ollama answers, so
        # the check passes, and the first real request fails minutes into an
        # interaction someone is waiting on.
        assert self._tagged(config, {"models": [{"name": "llama3:8b"}]}).is_available() is False

    def test_the_model_being_present_is_enough(self, config) -> None:
        name = config.models.llm.model
        assert self._tagged(config, {"models": [{"name": name}]}).is_available() is True

    def test_the_tag_does_not_have_to_match(self, config) -> None:
        # `qwen2.5:7b-instruct` and `qwen2.5:latest` are the same model pulled
        # differently, and refusing the second would be pedantry.
        base = config.models.llm.model.split(":")[0]
        provider = self._tagged(config, {"models": [{"name": f"{base}:latest"}]})
        assert provider.is_available() is True


class TestTheFactory:
    def test_the_configured_provider_is_built(self, config) -> None:
        assert isinstance(create_llm_provider(config), OllamaLLMProvider)

    def test_the_fake_is_selectable(self, config) -> None:
        narrowed = config.model_copy(
            update={
                "models": config.models.model_copy(
                    update={"llm": config.models.llm.model_copy(update={"provider": "fake"})}
                )
            }
        )
        assert isinstance(create_llm_provider(narrowed), FakeLLMProvider)

    def test_an_unknown_provider_is_refused_rather_than_substituted(self, config) -> None:
        narrowed = config.model_copy(
            update={
                "models": config.models.model_copy(
                    update={"llm": config.models.llm.model_copy(update={"provider": "gpt5"})}
                )
            }
        )
        with pytest.raises(ModelError) as error:
            create_llm_provider(narrowed)
        assert error.value.code is ErrorCode.PROVIDER_NOT_REGISTERED
        assert error.value.details["supported"] == list(LLM_PROVIDERS)


class TestThePrompts:
    """§92: what was asked is reproducible, and the schema matches the code."""

    @pytest.mark.parametrize("prompt_id", INTERACTION_PROMPTS)
    def test_each_prompt_loads_with_a_recorded_version(self, prompt_id: str) -> None:
        prompt = load_prompt(prompt_id)
        assert prompt.version == PROMPT_VERSIONS[prompt_id]
        assert prompt.output_schema.get("type") == "object"

    @pytest.mark.parametrize("prompt_id", INTERACTION_PROMPTS)
    def test_every_placeholder_is_filled_by_the_caller(self, prompt_id: str) -> None:
        # A template variable nobody supplies raises at the moment a person
        # types a sentence, which is the worst place to find out.
        prompt = load_prompt(prompt_id)
        supplied = {
            "interaction.instruction": {"text": "x", "intent": "y", "moment_types": "z"},
            "interaction.command": {"text": "x", "clip_count": 3, "duration": "1:00"},
            "interaction.question": {"question": "x", "records": "y"},
        }[prompt_id]
        # Rendering raises on a variable nobody supplied; this catches the
        # quieter case of one that survived into the text the model reads.
        rendered = prompt.render(**supplied)

        assert not re.findall(r"\{[a-z_][a-z0-9_]*\}", rendered)

    def test_the_instruction_schema_covers_the_fields_the_code_reads(self) -> None:
        from backend.interaction.models import IntentDelta

        properties = set(load_prompt("interaction.instruction").output_schema["properties"])
        assert properties - {"confidence", "unsupported"} <= set(IntentDelta.model_fields)

    @pytest.mark.parametrize(
        ("field", "enum"),
        [
            ("mode", VideoMode),
            ("pacing", Pacing),
            ("dead_time_policy", DeadTimePolicy),
            ("context_preservation", Level),
            ("effects", EffectsLevel),
            ("captions", CaptionPolicy),
            ("music", MusicPolicy),
            ("variety", Level),
        ],
    )
    def test_every_choice_offered_is_a_choice_that_exists(self, field: str, enum) -> None:
        """The drift this catches cost a working feature (see PHASE_13.md).

        The schema is a grammar to Ollama, so a wrong value is not a typo the
        model can route around -- it is *forced* to emit it, and the answer is
        then rejected by the Pydantic model on the way back. Every reading of
        that dimension fails, every time, and the chat says only "the model's
        answer did not fit". This file had four such drifts, including a
        `captions: animated` that has never existed.
        """
        offered = load_prompt("interaction.instruction").output_schema["properties"][field]["enum"]

        assert offered == [member.value for member in enum]

    def test_the_moment_types_offered_are_the_moment_types_there_are(self) -> None:
        schema = load_prompt("interaction.instruction").output_schema
        expected = [member.value for member in MomentType]
        for field in ("priority_moment_types", "avoid_moment_types"):
            for side in ("add", "remove"):
                assert schema["properties"][field]["properties"][side]["items"]["enum"] == expected

    def test_the_prompt_text_names_the_same_choices_as_the_schema(self) -> None:
        # The schema constrains the model; the prose is what it reads. A value
        # in one and not the other is the same drift by a slower route.
        prompt = load_prompt("interaction.instruction")
        text = prompt.render(text="x", intent="y", moment_types="z")
        for field, spec in prompt.output_schema["properties"].items():
            for value in spec.get("enum", []):
                assert f"`{value}`" in text, f"{field}: {value} is offered but never explained"

    def test_the_command_schema_offers_every_supported_kind(self) -> None:
        from backend.interaction.models import CommandKind

        schema = load_prompt("interaction.command").output_schema
        offered = set(schema["properties"]["kind"]["enum"])
        assert offered - {"none"} <= {kind.value for kind in CommandKind}

    @pytest.mark.parametrize("prompt_id", INTERACTION_PROMPTS)
    def test_confidence_is_always_required(self, prompt_id: str) -> None:
        # Every caller refuses a low-confidence reading, so a schema that lets
        # the model omit it would silently default the guard to zero.
        assert "confidence" in load_prompt(prompt_id).output_schema.get("required", [])

    @pytest.mark.parametrize("prompt_id", INTERACTION_PROMPTS)
    def test_the_schema_is_valid_json(self, prompt_id: str) -> None:
        json.dumps(load_prompt(prompt_id).output_schema)
