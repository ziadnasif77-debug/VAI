"""Reading the sentences the rules could not (SPEC §63, §85, §93–§95).

The rule-based parser handles the phrasings people actually use most:
"focus on clutches", "delete clip 5", "make it 25 minutes". It reports
confidence 0.0 on everything else, and this is what that signal escalates to.

The division of labour is the point. **The model reads; it does not decide.**
What it returns is mapped onto the same `IntentDelta` and `EditCommand` the
rule parser produces, validated by the same Pydantic models, and applied by the
same service methods. There is no path from a sentence to an effect that skips
the validation a typed command already goes through (§85).

Three consequences follow, and each is enforced here rather than trusted:

**It never touches a file.** §63 is explicit: natural language modifies project
state, not files. Nothing in this module opens, writes or deletes anything —
it returns data structures.

**A refusal is a real answer.** A model that cannot express an instruction says
so, and the person is told. Silently applying the nearest available preference
would leave them believing their instruction was followed.

**No model is not a failure (§95).** Without Ollama the interaction layer keeps
the rule path it always had. What is lost is the unusual phrasing, not the
feature — which is why the fallback is *this* way round, rules first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai.providers.base import LLMProvider
from backend.config.schema import AppConfig
from backend.core.errors import GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import MomentType
from backend.core.prompts import load_prompt
from backend.interaction.models import (
    Answer,
    AnswerSource,
    CommandKind,
    EditCommand,
    EditingIntent,
    Evidence,
    IntentDelta,
)

logger = get_logger("interaction.llm", LogChannel.AI)

#: Below this, the reading is reported rather than applied. A model that is
#: unsure has guessed, and a guessed edit costs more than a question.
MIN_CONFIDENCE: float = 0.5


@dataclass(frozen=True, slots=True)
class Reading:
    """What the model made of a sentence, and whether to act on it.

    ``value`` is ``None`` whenever the text could not be read, the model was
    unavailable, or its answer failed validation. ``reason`` always says which,
    because "no model installed" and "that instruction is not supported" lead a
    person to different next steps.
    """

    value: IntentDelta | EditCommand | Answer | None
    reason: str = ""
    confidence: float = 0.0
    #: True when a model was actually consulted. False means the rule path
    #: stands unaided and nothing about the model can be concluded.
    consulted: bool = False

    @property
    def understood(self) -> bool:
        return self.value is not None


class LlmInterpreter:
    """Turns unparsed text into validated instructions, commands and answers."""

    def __init__(self, config: AppConfig, provider: LLMProvider | None = None) -> None:
        """
        Args:
            provider: injected for tests and for callers that already hold one.
                Built lazily otherwise, so a machine without Ollama pays
                nothing until something is actually unparsed.
        """
        self._config = config
        self._provider = provider
        self._checked_availability: bool | None = None

    # -- availability ---------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._config.interaction.llm_fallback.enabled

    def available(self) -> bool:
        """Whether a model can be reached, cached for this instance.

        Cached because the answer is asked once per unparsed message and an
        HTTP round trip per keystroke-length message would be the slowest part
        of an interaction that is otherwise instant.
        """
        if not self.enabled:
            return False
        if self._checked_availability is None:
            provider = self._get_provider()
            self._checked_availability = bool(provider and provider.is_available())
        return self._checked_availability

    def _get_provider(self) -> LLMProvider | None:
        if self._provider is None:
            from ai.llm import create_llm_provider

            try:
                self._provider = create_llm_provider(self._config)
            except GamingEditorError as error:
                logger.warning("No language model is configured", extra={"error_code": error.code})
                return None
        return self._provider

    # -- instructions (§63) ---------------------------------------------

    def read_instruction(self, text: str, intent: EditingIntent) -> Reading:
        """Read an editing preference the parser did not recognise."""
        if not self.available():
            return Reading(None, reason=self._unavailable_reason())

        prompt = load_prompt("interaction.instruction")
        rendered = prompt.render(
            text=text,
            intent=_describe_intent(intent),
            moment_types=", ".join(item.value for item in MomentType),
        )
        payload = self._complete(prompt, rendered)
        if payload is None:
            return Reading(None, reason="the model could not be reached", consulted=True)

        confidence = float(payload.get("confidence", 0.0))
        unsupported = str(payload.get("unsupported", "")).strip()
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"confidence", "unsupported"} and value is not None
        }

        if not fields:
            return Reading(
                None,
                reason=unsupported or "that instruction does not map to any editing preference",
                confidence=confidence,
                consulted=True,
            )
        if confidence < MIN_CONFIDENCE:
            return Reading(
                None,
                reason="I was not confident enough about that reading to apply it",
                confidence=confidence,
                consulted=True,
            )

        try:
            # §94: the model's answer becomes the same validated object the
            # rule parser produces, or it becomes nothing.
            delta = IntentDelta.model_validate(fields)
        except Exception as error:  # pydantic ValidationError and friends
            logger.warning(
                "The model's instruction did not validate",
                extra={"error": str(error)[:200], "fields": sorted(fields)},
            )
            return Reading(
                None,
                reason="the model's answer did not fit the editing preferences",
                confidence=confidence,
                consulted=True,
            )

        if delta.is_empty():
            return Reading(
                None,
                reason=unsupported or "that instruction would change nothing",
                confidence=confidence,
                consulted=True,
            )

        policy = self._config.duration_policy
        target = delta.target_duration_seconds
        if target is not None and not policy.contains(target):
            # §6 is a product rule, not a preference the model may read past.
            # The prompt states the band; this is what enforces it.
            return Reading(
                None,
                reason=(
                    f"a {target // 60}-minute video is outside the supported "
                    f"{policy.min_seconds // 60}-{policy.max_seconds // 60} minute range"
                ),
                confidence=confidence,
                consulted=True,
            )
        return Reading(delta, confidence=confidence, consulted=True)

    # -- commands (§63, §85) --------------------------------------------

    def read_command(self, text: str, *, clip_count: int, duration: str) -> Reading:
        """Read an edit command the parser did not recognise.

        The clip count is given to the model so it can refuse an out-of-range
        clip rather than inventing one — and refused again here, because a
        prompt is guidance and this is a rule.
        """
        if not self.available():
            return Reading(None, reason=self._unavailable_reason())

        prompt = load_prompt("interaction.command")
        rendered = prompt.render(text=text, clip_count=clip_count, duration=duration)
        payload = self._complete(prompt, rendered)
        if payload is None:
            return Reading(None, reason="the model could not be reached", consulted=True)

        kind = str(payload.get("kind", "none"))
        confidence = float(payload.get("confidence", 0.0))
        if kind == "none":
            return Reading(
                None,
                reason=str(payload.get("reason", "") or "that is not an edit command"),
                confidence=confidence,
                consulted=True,
            )
        if confidence < MIN_CONFIDENCE:
            return Reading(
                None,
                reason="I was not confident enough about that reading to change the edit",
                confidence=confidence,
                consulted=True,
            )

        index = payload.get("clip_index")
        if index is not None and not 1 <= int(index) <= max(clip_count, 0):
            # The prompt says so and the model may still do it. A command that
            # names a clip which does not exist is refused here.
            return Reading(
                None,
                reason=f"there is no clip {int(index)} in this edit",
                confidence=confidence,
                consulted=True,
            )

        try:
            command = EditCommand.model_validate(
                {
                    "kind": CommandKind(kind),
                    # The interface numbers clips from 1; the model was told so.
                    "clip_index": int(index) if index is not None else None,
                    "timestamp_seconds": payload.get("timestamp_seconds"),
                    "target_duration_seconds": payload.get("target_duration_seconds"),
                    "version": payload.get("version"),
                    "start_delta": payload.get("start_delta"),
                    "end_delta": payload.get("end_delta"),
                    "to_index": payload.get("to_index"),
                    "raw_text": text,
                }
            )
        except Exception as error:
            logger.warning(
                "The model's command did not validate", extra={"error": str(error)[:200]}
            )
            return Reading(
                None,
                reason="the model's answer did not fit any edit command",
                confidence=confidence,
                consulted=True,
            )
        return Reading(command, confidence=confidence, consulted=True)

    # -- questions (§59, §80) -------------------------------------------

    def answer_question(self, question: str, evidence: dict[str, Evidence]) -> Reading:
        """Answer from retrieved records, or decline.

        Grounding is the whole design, and it is enforced rather than asked
        for: the model is given records with ids and cites the ids it used,
        and each citation is resolved back to the record it names. An id the
        model invented does not resolve, so an answer standing entirely on
        invented citations arrives with no evidence and is refused -- the same
        rule §80 applies to every other claim this system makes.
        """
        if not self.available():
            return Reading(None, reason=self._unavailable_reason())
        if not evidence:
            return Reading(
                None,
                reason="there is nothing analysed yet to answer from",
            )

        prompt = load_prompt("interaction.question")
        rendered = prompt.render(question=question, records=_describe_records(evidence))
        payload = self._complete(prompt, rendered)
        if payload is None:
            return Reading(None, reason="the model could not be reached", consulted=True)

        confidence = float(payload.get("confidence", 0.0))
        text = str(payload.get("answer", "")).strip()
        answered = bool(payload.get("answered", True))
        if not text or not answered:
            return Reading(
                None,
                reason=text or "the analysis does not cover that",
                confidence=confidence,
                consulted=True,
            )

        cited = [
            evidence[str(item)] for item in payload.get("citations", []) if str(item) in evidence
        ]
        if not cited:
            # Either it cited nothing, or everything it cited was invented.
            # Both mean the answer is not traceable to the analysis.
            return Reading(
                None,
                reason="the model's answer did not cite anything from the analysis",
                confidence=confidence,
                consulted=True,
            )

        answer = Answer(
            text=text,
            confidence=confidence,
            source=AnswerSource.LLM,
            evidence=cited,
        )
        return Reading(answer, confidence=confidence, consulted=True)

    # -- internals ------------------------------------------------------

    def _complete(self, prompt: Any, rendered: str) -> dict[str, Any] | None:
        """One structured call, with failures turned into ``None`` (§95)."""
        provider = self._get_provider()
        if provider is None:
            return None
        try:
            return provider.complete_json(
                rendered,
                schema=prompt.output_schema,
                prompt_id=prompt.id,
                temperature=self._config.interaction.llm_fallback.temperature,
            )
        except GamingEditorError as error:
            # The retries already happened inside the provider (§94). This is
            # the point where the interaction gives up on the model and the
            # rule-based message stands.
            logger.warning(
                "The language model could not read the message",
                extra={"prompt_id": prompt.id, "error_code": error.code},
            )
            # A failure here may be the model going away mid-session, so the
            # cached availability is cleared rather than trusted.
            self._checked_availability = None
            return None

    def _unavailable_reason(self) -> str:
        if not self.enabled:
            return "the language-model fallback is switched off"
        return "no language model is available on this machine"


def _describe_records(evidence: dict[str, Evidence]) -> str:
    """The retrieved records, one per line with the id the model must cite."""
    lines: list[str] = []
    for record_id, item in evidence.items():
        when = ""
        if item.start_seconds is not None:
            minutes, seconds = divmod(int(item.start_seconds), 60)
            when = f" at {minutes}:{seconds:02d}"
        score = f" (score {item.score:.2f})" if item.score is not None else ""
        lines.append(f"[{record_id}] {item.kind.value}{when}{score}: {item.detail}")
    return "\n".join(lines)


def _describe_intent(intent: EditingIntent) -> str:
    """The current preferences, one per line, for the prompt.

    Given to the model because an instruction is often relative -- "a bit
    faster" means nothing without knowing the current pace.
    """
    lines = [
        f"- pacing: {intent.pacing.value}",
        f"- dead time: {intent.dead_time_policy.value}",
        f"- context preservation: {intent.context_preservation.value}",
        f"- effects: {intent.effects.value}",
        f"- captions: {intent.captions.value}",
        f"- music: {intent.music.value}",
        f"- variety: {intent.variety.value}",
    ]
    if intent.mode:
        lines.append(f"- mode: {intent.mode.value}")
    if intent.target_duration_seconds:
        lines.append(f"- target length: {intent.target_duration_seconds // 60} minutes")
    if intent.priority_moment_types:
        lines.append(
            "- favouring: " + ", ".join(item.value for item in intent.priority_moment_types)
        )
    if intent.avoid_moment_types:
        lines.append("- avoiding: " + ", ".join(item.value for item in intent.avoid_moment_types))
    return "\n".join(lines)


__all__ = ["MIN_CONFIDENCE", "LlmInterpreter", "Reading"]
