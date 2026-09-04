"""The interaction service -- one entry point for a user message.

SPEC §14, §15, §21, §22: the chat is a *control and query interface* over the
editor, not a chatbot that owns it. This service classifies a message, routes it
to intent resolution, question answering or the command applier, and records the
exchange. It never renders, never analyses, and never calls FFmpeg.

Dependency direction stays one-way: this module imports the pipeline's models
and repositories; nothing in the pipeline imports this.
"""

from __future__ import annotations

from typing import Any

from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, ValidationError
from backend.core.logging import LogChannel, get_logger, log_context
from backend.core.models.enums import JobStage
from backend.core.models.project import Project
from backend.database.connection import Database
from backend.database.repositories.projects import ProjectRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.interaction.intent import IntentResolver
from backend.interaction.knowledge import VideoKnowledgeBase
from backend.interaction.llm_fallback import LlmInterpreter, Reading
from backend.interaction.models import (
    Answer,
    AnswerSource,
    CommandKind,
    ConversationMessage,
    EditCommand,
    EditingIntent,
    EditVersion,
    IntentDelta,
    IntentSource,
    InteractionResult,
    InteractionType,
    MessageRole,
)
from backend.interaction.parser import (
    classify,
    parse_command,
    parse_instruction,
    shift_effects,
    shift_pacing,
)
from backend.interaction.phrases import Phrasebook
from backend.interaction.qa import QuestionAnswering
from backend.interaction.store import ConversationStore, EditVersionStore, IntentStore
from backend.preferences.learning import as_delta, learn
from backend.preferences.models import Preferences

logger = get_logger("interaction.service", LogChannel.APPLICATION)

#: Commands that edit the timeline through :mod:`backend.timeline.operations`
#: rather than by toggling a clip's enabled flag.
_TIMELINE_COMMANDS = frozenset(
    {CommandKind.TRIM_CLIP, CommandKind.SPLIT_CLIP, CommandKind.MOVE_CLIP}
)


class InteractionService:
    """Handles editing instructions, questions and commands for a project."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        *,
        interpreter: LlmInterpreter | None = None,
    ) -> None:
        """
        Args:
            interpreter: the §63 fallback, injectable for tests and for callers
                that already hold one. Built lazily otherwise.
        """
        self._db = database
        self._config = config
        self._projects = ProjectRepository(database)
        self._resolver = IntentResolver(config)
        self._knowledge = VideoKnowledgeBase(database)
        self._intents = IntentStore(database)
        self._conversation = ConversationStore(database)
        self._versions = EditVersionStore(database)
        # §63's fallback. Constructed here but silent until something is
        # unparsed: it does not reach for a model until one is needed. One
        # instance, shared with question answering, so the availability check
        # and the loaded model are shared too (§54).
        self._llm = interpreter if interpreter is not None else LlmInterpreter(config)
        self._qa = QuestionAnswering(config, self._knowledge, self._llm)

    # -- intent ---------------------------------------------------------

    def current_intent(self, project_id: str) -> EditingIntent:
        """Resolve the effective intent: preset + project settings + instructions.

        Recomputed from the update log rather than read from the cache, so the
        log stays the authority and a schema change to the intent cannot serve
        a stale shape.
        """
        project = self._projects.require(project_id)
        preset = self._intents.preset_for(project_id) or self._config.interaction.default_preset
        intent = self._resolver.resolve(
            preset_name=preset,
            updates=self._intents.updates(project_id),
            project_target_duration_seconds=project.target_duration_seconds,
            project_mode=project.mode,
            learned=self._learned(project_id),
        )
        self._intents.cache_resolved(project_id, preset, intent)
        return intent

    def preferences(self, project_id: str | None = None) -> Preferences:
        """What this editor has learned about the person using it (Phase F).

        Read fresh rather than cached. The log it reads is small -- a handful
        of rows per project -- and a cache would have to be invalidated by
        every instruction in every project, which is more machinery than the
        query costs.
        """
        if not self._config.interaction.learn_preferences:
            return Preferences()
        return learn(self._db, exclude_project=project_id)

    def _learned(self, project_id: str) -> IntentDelta | None:
        preferences = self.preferences(project_id)
        return as_delta(preferences) if not preferences.is_empty else None

    def set_preset(self, project_id: str, preset_name: str) -> EditingIntent:
        """Switch preset, keeping every instruction the user has given (§4)."""
        self._projects.require(project_id)
        self._resolver.preset(preset_name)  # validates the name
        base = self._resolver.base_intent(preset_name)
        self._intents.set_preset(project_id, preset_name, base)
        intent = self.current_intent(project_id)
        logger.info(
            "Editing preset selected",
            extra={"project_id": project_id, "preset": preset_name},
        )
        return intent

    def apply_instruction(
        self, project_id: str, text: str, *, source: IntentSource = IntentSource.INSTRUCTION
    ) -> tuple[EditingIntent, float]:
        """Parse an instruction and append it to the intent log.

        Returns the new intent and the parser's confidence. A confidence of 0
        means nothing was recognised -- the caller decides whether to escalate
        to an LLM.
        """
        project = self._projects.require(project_id)
        current = self.current_intent(project_id)
        parsed = parse_instruction(text)

        delta = parsed.delta
        # Relative requests need the current value, which the parser cannot see.
        resolved: dict[str, Any] = {}
        if parsed.pacing_shift:
            resolved["pacing"] = shift_pacing(current.pacing, parsed.pacing_shift)
        if parsed.effects_shift:
            resolved["effects"] = shift_effects(current.effects, parsed.effects_shift)
        if parsed.duration_multiplier is not None:
            base = current.target_duration_seconds or project.target_duration_seconds
            policy = self._config.duration_policy
            resolved["target_duration_seconds"] = policy.clamp(base * parsed.duration_multiplier)
        if resolved:
            delta = delta.model_copy(update=resolved)

        if delta.is_empty():
            return current, 0.0

        if "target_duration_seconds" in delta.model_dump(exclude_none=True):
            self._validate_duration(delta.target_duration_seconds)

        self._intents.append(
            project_id,
            delta,
            source=source,
            raw_text=text,
            confidence=parsed.confidence,
        )
        intent = self.current_intent(project_id)
        logger.info(
            "Editing intent updated",
            extra={
                "project_id": project_id,
                "matched_rules": parsed.matched,
                "confidence": parsed.confidence,
            },
        )
        return intent, parsed.confidence

    def reset_intent(self, project_id: str) -> EditingIntent:
        """Discard every instruction, keeping the preset."""
        self._projects.require(project_id)
        self._intents.clear(project_id)
        return self.current_intent(project_id)

    # -- questions ------------------------------------------------------

    def ask(self, project_id: str, question: str) -> Answer:
        """Answer a question from stored analysis data. Never re-analyses (§6)."""
        self._projects.require(project_id)
        return self._qa.answer(project_id, question)

    # -- commands -------------------------------------------------------

    def apply_command(self, project_id: str, command: EditCommand) -> InteractionResult:
        """Apply an edit command.

        Commands change the EDL and require a re-render. They never invalidate
        the analysis (§10), which is why nothing here touches a job.
        """
        if not self._config.interaction.commands.enabled:
            raise ValidationError(
                "Edit commands are disabled in this installation.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"command": command.kind.value},
                recoverable=False,
            )

        project = self._projects.require(project_id)
        with log_context(project_id=project_id):
            if command.kind is CommandKind.SET_DURATION:
                return self._set_duration(project, command)
            if command.kind is CommandKind.REVERT_VERSION:
                return self._revert(project, command)
            return self._apply_timeline_command(project, command)

    def _set_duration(self, project: Project, command: EditCommand) -> InteractionResult:
        seconds = self._validate_duration(command.target_duration_seconds)
        self._intents.append(
            project.id,
            IntentDelta(target_duration_seconds=seconds),
            source=IntentSource.COMMAND,
            raw_text=command.raw_text,
        )
        intent = self.current_intent(project.id)
        return InteractionResult(
            interaction_type=InteractionType.COMMAND,
            message=f"Target duration set to {seconds // 60} minutes.",
            intent=intent,
            applied_command=command,
            requires_rerender=True,
        )

    def _revert(self, project: Project, command: EditCommand) -> InteractionResult:
        """Restore a previous edit without re-analysing anything (§19)."""
        version = self._versions.get(project.id, command.version or 0)
        restored = self._restore_clips(project.id, version)
        return InteractionResult(
            interaction_type=InteractionType.COMMAND,
            message=f"Restored edit version {version.version} ({restored} clips).",
            intent=version.intent,
            applied_command=command,
            requires_rerender=True,
            edit_version=version.version,
        )

    def _apply_timeline_command(self, project: Project, command: EditCommand) -> InteractionResult:
        """Enable or disable clips on the current timeline."""
        clips = self._knowledge.clips(project.id, enabled_only=False)
        if not clips:
            raise ValidationError(
                "There is no edit to modify yet. Generate one first.",
                code=ErrorCode.INVALID_TIMELINE_OPERATION,
                details={"project_id": project.id, "command": command.kind.value},
                recoverable=False,
            )

        self._snapshot(project.id, reason=f"before {command.kind.value}")

        # §63: confirmed in the language the edit was asked for. The raw text
        # is what the person typed; a command built by the API rather than by a
        # sentence carries none, and falls back to English.
        phrases = Phrasebook.for_message(command.raw_text)

        if command.kind is CommandKind.DELETE_CLIP:
            target = _clip_by_index(clips, command.clip_index)
            self._set_clip_enabled(project.id, target.id, False)
            message = phrases.say("removed_clip", clip=target.clip_index)
        elif command.kind is CommandKind.RESTORE_CLIP:
            target = _clip_by_index(clips, command.clip_index)
            self._set_clip_enabled(project.id, target.id, True)
            message = phrases.say("restored_clip", clip=target.clip_index)
        elif command.kind is CommandKind.DELETE_AT_TIMESTAMP:
            timestamp = command.timestamp_seconds or 0.0
            matching = [
                clip
                for clip in clips
                if clip.timeline_start <= timestamp <= clip.timeline_end and clip.enabled
            ]
            if not matching:
                raise ValidationError(
                    f"No clip covers {timestamp:.0f}s in the current edit.",
                    code=ErrorCode.INVALID_TIMELINE_OPERATION,
                    details={"timestamp_seconds": timestamp},
                    recoverable=False,
                )
            for clip in matching:
                self._set_clip_enabled(project.id, clip.id, False)
            message = phrases.say("removed_at", count=len(matching))
        elif command.kind in _TIMELINE_COMMANDS:
            # trim / split / move: the timeline has done these since Phase 8
            # and the API has exposed them just as long. They route through the
            # same operations module the timeline screen uses, so a sentence
            # and a drag produce the identical edit and the identical refusal
            # (§42's bounds are checked in one place, not two).
            target = _clip_by_index(clips, command.clip_index)
            message = self._apply_timeline_operation(project.id, target, command, phrases)
        else:
            raise ValidationError(
                f"Command {command.kind.value!r} is not supported yet.",
                code=ErrorCode.INVALID_TIMELINE_OPERATION,
                details={"command": command.kind.value},
                recoverable=False,
            )

        # §80: the version list is what a person scrolls to understand how the
        # edit got here, so it records what they asked for. The kind alone --
        # "delete_clip" -- is the least informative true thing available, and
        # for a command the model read from a sentence it is the only record
        # outside the chat log.
        version = self._snapshot(project.id, reason=command.raw_text or command.kind.value)
        duration = self._knowledge.edit_duration_seconds(project.id)
        return InteractionResult(
            interaction_type=InteractionType.COMMAND,
            message=f"{message} The edit is now {duration / 60:.1f} minutes.",
            intent=self.current_intent(project.id),
            applied_command=command,
            requires_rerender=True,
            edit_version=version.version,
        )

    # -- conversation ---------------------------------------------------

    def handle(self, project_id: str, text: str) -> InteractionResult:
        """Route one user message and record the exchange (§11, §15)."""
        project = self._projects.require(project_id)
        message = text.strip()
        if not message:
            raise ValidationError(
                "Empty message.",
                code=ErrorCode.BUSINESS_VALIDATION_FAILED,
                details={"project_id": project_id},
                recoverable=False,
            )

        kind = classify(message)
        self._conversation.append(
            project_id, role=MessageRole.USER, text=message, interaction_type=kind
        )

        if kind is InteractionType.QUESTION:
            result = self._handle_question(project_id, message)
        elif kind is InteractionType.COMMAND:
            result = self._handle_command(project, message)
        else:
            result = self._handle_instruction(project_id, message)

        self._conversation.append(
            project_id,
            role=MessageRole.ASSISTANT,
            text=result.message,
            interaction_type=kind,
            payload={"requires_rerender": result.requires_rerender},
        )
        return result

    def history(self, project_id: str, *, limit: int | None = None) -> list[ConversationMessage]:
        self._projects.require(project_id)
        return self._conversation.history(
            project_id, limit=limit or self._config.interaction.conversation.max_messages
        )

    def versions(self, project_id: str) -> list[EditVersion]:
        self._projects.require(project_id)
        return self._versions.list(project_id)

    # -- internals ------------------------------------------------------

    def _handle_question(self, project_id: str, message: str) -> InteractionResult:
        answer = self.ask(project_id, message)
        return InteractionResult(
            interaction_type=InteractionType.QUESTION,
            message=answer.text,
            answer=answer,
        )

    def _handle_command(self, project: Project, message: str) -> InteractionResult:
        command = parse_command(message)
        if command is None:
            # §63: the rules did not recognise it, so ask the model to read it.
            # What comes back is an EditCommand like any other and goes through
            # apply_command, which validates it exactly as it validates a typed
            # one -- there is no shortcut from a sentence to an edit (§85).
            reading = self._llm.read_command(
                message,
                clip_count=self._knowledge.clip_count(project.id, enabled_only=False),
                duration=_timecode(self._knowledge.edit_duration_seconds(project.id)),
            )
            if isinstance(reading.value, EditCommand):
                logger.info(
                    "The model read an edit command",
                    extra={
                        "project_id": project.id,
                        "kind": reading.value.kind.value,
                        "confidence": reading.confidence,
                    },
                )
                return self.apply_command(project.id, reading.value)
            return InteractionResult(
                interaction_type=InteractionType.COMMAND,
                message=_command_help(reading, Phrasebook.for_message(message)),
                answer=Answer(
                    text=reading.reason or "Command not understood.",
                    confidence=0.0,
                    source=AnswerSource.UNAVAILABLE,
                ),
            )
        return self.apply_command(project.id, command)

    def _handle_instruction(self, project_id: str, message: str) -> InteractionResult:
        phrases = Phrasebook.for_message(message)
        intent, confidence = self.apply_instruction(project_id, message)
        if confidence == 0.0:
            # The parser reports 0.0 for text it could not read at all, which
            # is precisely the signal §63's fallback exists for.
            reading = self._llm.read_instruction(message, intent)
            # A delta that resolves to the intent already in force is not an
            # instruction that was followed -- it is the model restating the
            # status quo. The real qwen answers "delete the part right after
            # the opener" with `pacing: fast` when the pacing is already fast,
            # and reporting that as "updated the editing brief" would tell
            # someone their edit had been made when nothing happened at all.
            if isinstance(reading.value, IntentDelta) and (
                self._resolver.apply(intent, reading.value) != intent
            ):
                logger.info(
                    "The model read an editing instruction",
                    extra={"project_id": project_id, "confidence": reading.confidence},
                )
                self._intents.append(
                    project_id,
                    reading.value,
                    source=IntentSource.INSTRUCTION,
                    raw_text=message,
                )
                intent = self.current_intent(project_id)
                return InteractionResult(
                    interaction_type=InteractionType.EDITING_INSTRUCTION,
                    message=phrases.say(
                        "brief_updated", summary=self._resolver.describe(intent, phrases)
                    ),
                    intent=intent,
                    requires_rerender=True,
                )
            # Nothing changed the brief, so the last thing worth trying is that
            # it was an edit all along. The classifier routes a sentence here
            # unless it names a clip or a timestamp -- deliberately, because
            # before there was a model the alternative was rules deleting
            # footage on a vague phrase. "Delete the part right after the
            # opener" names neither and lands here; the model can read it, and
            # what it returns is validated exactly as a typed command is (§85).
            escalated = self._escalate_to_command(project_id, message)
            if escalated is not None:
                return escalated
            return InteractionResult(
                interaction_type=InteractionType.EDITING_INSTRUCTION,
                message=_instruction_help(reading, Phrasebook.for_message(message)),
                intent=intent,
            )
        return InteractionResult(
            interaction_type=InteractionType.EDITING_INSTRUCTION,
            message=phrases.say("brief_updated", summary=self._resolver.describe(intent, phrases)),
            intent=intent,
            # The intent feeds the STORY stage onward; the analysis is untouched.
            requires_rerender=True,
        )

    def _escalate_to_command(self, project_id: str, message: str) -> InteractionResult | None:
        """Read an unreadable instruction as an edit command, or give up.

        Returns ``None`` unless the model reads a command it is confident
        about, which is the common case: the command prompt answers ``none``
        for anything that is not an edit, and that is a real answer, not a
        failure. The cost is one extra local call on a message that has
        already failed everything else.
        """
        if not self._config.interaction.commands.enabled:
            return None
        clip_count = self._knowledge.clip_count(project_id, enabled_only=False)
        if clip_count == 0:
            # Nothing to edit yet, so every command would be refused anyway --
            # and `apply_command` refuses by *raising*, which is right for a
            # command someone deliberately sent and wrong for a guess made
            # about an instruction.
            return None
        reading = self._llm.read_command(
            message,
            clip_count=clip_count,
            duration=_timecode(self._knowledge.edit_duration_seconds(project_id)),
        )
        if not isinstance(reading.value, EditCommand):
            return None
        logger.info(
            "The model read an edit command from an instruction",
            extra={
                "project_id": project_id,
                "kind": reading.value.kind.value,
                "confidence": reading.confidence,
            },
        )
        return self.apply_command(project_id, reading.value)

    def _apply_timeline_operation(
        self, project_id: str, target: Any, command: EditCommand, phrases: Phrasebook
    ) -> str:
        """Run trim, split or move through the shared operations module."""
        from backend.timeline import operations

        repository = TimelineRepository(self._db)
        timeline = repository.load(project_id)

        if command.kind is CommandKind.TRIM_CLIP:
            from backend.config.paths import build_paths
            from backend.gaming.exclusions import exclusions_for_media
            from backend.timeline.authorization import Granter

            edited = operations.trim(
                timeline,
                target.id,
                start_delta=command.start_delta or 0.0,
                end_delta=command.end_delta or 0.0,
                # P0.3: the person asking is the granter; their grant is cut
                # back at the recording's exclusions like anyone else's.
                granted_by=Granter.HUMAN,
                reason=(
                    f"trimmed by hand (chat): start {command.start_delta or 0.0:+.2f} s, "
                    f"end {command.end_delta or 0.0:+.2f} s"
                ),
                exclusions=exclusions_for_media(
                    self._db, target.media_id, build_paths(self._config).profiles_dir
                ),
            )
            message = phrases.say(
                "trimmed_clip",
                clip=target.clip_index,
                start=f"{command.start_delta or 0.0:+.1f}",
                end=f"{command.end_delta or 0.0:+.1f}",
            )
        elif command.kind is CommandKind.SPLIT_CLIP:
            at = command.timestamp_seconds or 0.0
            edited = operations.split(timeline, target.id, at)
            message = phrases.say("split_clip", clip=target.clip_index, at=_timecode(at))
        else:
            edited = operations.move(timeline, target.id, command.to_index or 1)
            message = phrases.say("moved_clip", clip=target.clip_index, to=command.to_index)

        repository.save_edit(project_id, operations.reflow(edited))
        return message

    def _validate_duration(self, seconds: int | None) -> int:
        policy = self._config.duration_policy
        if seconds is None:
            raise ValidationError(
                "No target duration given.",
                code=ErrorCode.INVALID_TARGET_DURATION,
                recoverable=False,
            )
        if not policy.contains(seconds):
            raise ValidationError(
                f"A {seconds // 60}-minute target is outside the supported "
                f"{policy.min_seconds // 60}-{policy.max_seconds // 60} minute range.",
                code=ErrorCode.INVALID_TARGET_DURATION,
                details={
                    "requested_seconds": seconds,
                    "min_seconds": policy.min_seconds,
                    "max_seconds": policy.max_seconds,
                },
                recoverable=False,
            )
        return seconds

    def snapshot(self, project_id: str, *, reason: str) -> EditVersion:
        """Record the edit as it stands, so the next change is undoable (§78).

        Public because two doors lead to the same timeline: the chat commands
        (which always snapshotted) and the timeline screen's buttons (which
        did not, so "undo" silently could not cover exactly the edits people
        make most). One snapshot path, whoever asks.
        """
        return self._snapshot(project_id, reason=reason)

    def _snapshot(self, project_id: str, *, reason: str) -> EditVersion:
        clips = [
            {
                "id": clip.id,
                "clip_index": clip.clip_index,
                "media_id": clip.media_id,
                "moment_id": clip.moment_id,
                "source_in": clip.source_in,
                "source_out": clip.source_out,
                "timeline_start": clip.timeline_start,
                "timeline_end": clip.timeline_end,
                "enabled": clip.enabled,
            }
            for clip in self._knowledge.clips(project_id, enabled_only=False)
        ]
        version = self._versions.snapshot(
            project_id, self.current_intent(project_id), clips, reason=reason
        )
        self._versions.prune(project_id, self._config.interaction.commands.keep_versions)
        return version

    def _set_clip_enabled(self, project_id: str, clip_id: str, enabled: bool) -> None:
        """Enable or disable a clip through the timeline repository.

        Not a raw ``UPDATE``. Removing a clip has to re-flow the ones after it,
        or the finished video keeps a hole the length of what was removed --
        the validator calls that a gap and the renderer would draw it as black
        frames. The repository owns that rule so every path obeys it.
        """
        TimelineRepository(self._db).set_enabled(project_id, clip_id, enabled=enabled)

    def _restore_clips(self, project_id: str, version: EditVersion) -> int:
        """Re-apply a snapshot's enabled/disabled state, re-flowing once (§78)."""
        states = {
            str(clip["id"]): bool(clip.get("enabled", True))
            for clip in version.clips
            if clip.get("id")
        }
        return TimelineRepository(self._db).apply_enabled_states(project_id, states)


def _clip_by_index(clips: list, index: int | None) -> Any:
    for clip in clips:
        if clip.clip_index == index:
            return clip
    raise ValidationError(
        f"There is no clip {index} in the current edit.",
        code=ErrorCode.INVALID_TIMELINE_OPERATION,
        details={"clip_index": index, "available": [clip.clip_index for clip in clips]},
        recoverable=False,
    )


#: The pipeline stage the intent first influences. Everything before it is
#: analysis and is never re-run because an instruction changed (§10).
FIRST_INTENT_DEPENDENT_STAGE = JobStage.STORY


def _timecode(seconds: float) -> str:
    """Seconds as m:ss, for telling the model how long the edit is."""
    minutes, rest = divmod(int(max(0.0, seconds)), 60)
    return f"{minutes}:{rest:02d}"


def _clause(reason: str) -> str:
    """A reason fit to follow "I read that, but ...".

    The reasons this code writes are already clauses. The ones a model writes
    are sentences, and dropping one in unedited produces "I read that, but The
    instruction does not correspond to any preference.." -- which reads like a
    bug even when the answer is right.
    """
    text = reason.strip().rstrip(".")
    if text[:2].isupper():
        # An acronym or a quoted term; leave the capitalisation alone.
        return text
    return text[:1].lower() + text[1:] if text else text


def _instruction_help(reading: Reading, phrases: Phrasebook) -> str:
    """What to say when an instruction could not be applied.

    The distinction matters more than it looks. "No model is installed" and
    "that preference does not exist" send a person to completely different next
    steps, and a single polite refusal for both would strand whoever hit the
    first one.

    The model's own ``reason`` is passed through in whatever language it wrote
    it, and it writes English. Translating it here would mean paraphrasing a
    sentence this code did not produce and cannot check -- so the frame is
    Arabic and the quoted reason is the model's, which is at least honest about
    where each half came from.
    """
    unrecognised = phrases.say("instruction_unreadable")
    if reading.consulted and reading.reason:
        return f"{phrases.say('command_read_but', reason=_clause(reading.reason))} {unrecognised}"
    if reading.reason:
        return f"{unrecognised[:-1]} — {_clause(reading.reason)}."
    return unrecognised


def _command_help(reading: Reading, phrases: Phrasebook) -> str:
    """What to say when a command could not be applied."""
    examples = phrases.say("command_examples")
    if reading.consulted and reading.reason:
        return phrases.say("command_read_but", reason=_clause(reading.reason))
    if reading.reason:
        return phrases.say(
            "command_unreadable_because",
            reason=_clause(reading.reason),
            examples=examples,
        )
    return phrases.say("command_unreadable", examples=examples)


__all__ = ["FIRST_INTENT_DEPENDENT_STAGE", "InteractionService"]
