"""Question answering over the video knowledge base (§5, §6, §12, §13, §20).

Every answer here is computed from stored analysis results. Nothing in this
module loads a model or touches a video file.

Three rules, all of them about not lying:

* **Refuse before guessing.** If the analysis that would answer a question has
  not run, say so (§17).
* **Cite the data.** Every claim carries the records it came from; the "why did
  you pick this clip?" answer is assembled from the stored score breakdown, not
  invented (§12).
* **Report confidence.** An uncertain answer says it is uncertain (§13).
"""

from __future__ import annotations

import re
from enum import Enum

from backend.config.schema import AppConfig
from backend.core.duration import format_duration
from backend.interaction.knowledge import (
    MomentRecord,
    VideoKnowledgeBase,
)
from backend.interaction.llm_fallback import LlmInterpreter
from backend.interaction.models import Answer, AnswerSource, Evidence, EvidenceKind
from backend.interaction.parser import normalise
from backend.interaction.phrases import Phrasebook
from backend.moments.explanation import ReasonFacts, build_reasons


class QuestionKind(str, Enum):
    """Question shapes answerable straight from the database (§20)."""

    EDIT_DURATION = "edit_duration"
    CLIP_COUNT = "clip_count"
    BEST_MOMENTS = "best_moments"
    BEST_OF_TYPE = "best_of_type"
    WHY_THIS_CLIP = "why_this_clip"
    WHAT_HAPPENED_AT = "what_happened_at"
    COUNT_EVENTS = "count_events"
    EXCLUDED_MOMENTS = "excluded_moments"
    UNKNOWN = "unknown"


_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "clutch": ("clutch", "كلتش", "كلاتش"),
    "funny": ("funny", "مضحك", "أطرف", "اطرف", "طريف"),
    "epic": ("epic", "ملحمي", "أسطوري"),
    "fail": ("fail", "فشل"),
    "comeback": ("comeback", "عودة", "انتفاضة"),
    "victory": ("victory", "فوز", "انتصار"),
    "skill": ("skill", "مهارة"),
    "boss": ("boss", "زعيم"),
    "rage": ("rage", "غضب"),
}

_EVENT_WORDS: dict[str, tuple[str, ...]] = {
    "death": ("death", "deaths", "died", "مت", "موت", "وفاة"),
    "kill": ("kill", "kills", "قتل", "قتلات"),
    "multi_kill": ("multi kill", "multi-kill", "multikill", "قتل متعدد"),
    "victory": ("victory", "wins", "فوز"),
    "defeat": ("defeat", "خسارة"),
}


class QuestionAnswering:
    """Answers questions about a project from its analysis data."""

    def __init__(
        self,
        config: AppConfig,
        knowledge: VideoKnowledgeBase,
        interpreter: LlmInterpreter | None = None,
    ) -> None:
        """
        Args:
            interpreter: the §63 fallback for questions the resolvers below do
                not recognise. Built lazily, so a machine without a model pays
                nothing until a question actually falls through.
        """
        self._config = config
        self._knowledge = knowledge
        self._llm = interpreter if interpreter is not None else LlmInterpreter(config)

    # -- entry point ----------------------------------------------------

    def answer(self, project_id: str, question: str) -> Answer:
        """Answer ``question`` or explain why it cannot be answered yet."""
        kind = self.classify(question)
        # §63: the language is the person's, decided per message rather than
        # from configuration -- one Arabic question inside an English session
        # is still an Arabic question.
        phrases = Phrasebook.for_message(question)
        readiness = self._knowledge.readiness(project_id)

        # Edit-level questions read the timeline, not the analysis, so they work
        # as soon as an edit exists.
        if kind is QuestionKind.EDIT_DURATION:
            return self._edit_duration(project_id, phrases)
        if kind is QuestionKind.CLIP_COUNT:
            return self._clip_count(project_id, phrases)

        if self._config.interaction.qa.require_analysis_complete and not readiness.is_complete:
            return _not_ready(kind, phrases)

        if kind is QuestionKind.BEST_MOMENTS:
            return self._best_moments(project_id, phrases)
        if kind is QuestionKind.BEST_OF_TYPE:
            return self._best_of_type(project_id, question, phrases)
        if kind is QuestionKind.WHY_THIS_CLIP:
            return self._why_this_clip(project_id, question, phrases)
        if kind is QuestionKind.WHAT_HAPPENED_AT:
            return self._what_happened_at(project_id, question, phrases)
        if kind is QuestionKind.COUNT_EVENTS:
            return self._count_events(project_id, question, phrases)
        if kind is QuestionKind.EXCLUDED_MOMENTS:
            return self._excluded_moments(project_id, phrases)

        # Nothing deterministic recognised it, so try the model -- grounded in
        # the same records the resolvers above would have used, and refused if
        # it cannot cite them (§80).
        reading = self._llm.answer_question(question, self._evidence_pool(project_id))
        if isinstance(reading.value, Answer):
            return reading.value

        suffix = f" {reading.reason.capitalize()}." if reading.consulted and reading.reason else ""
        return Answer(
            text=phrases.say("question_unreadable", suffix=suffix),
            confidence=0.0,
            source=AnswerSource.UNAVAILABLE,
        )

    def _evidence_pool(self, project_id: str, *, limit: int | None = None) -> dict[str, Evidence]:
        """The records a free-form question may be answered from (§80).

        Bounded by ``interaction.llm_fallback.max_input_records``: a
        two-hour session has hundreds of moments, and a prompt carrying all of
        them would cost more context than the answer is worth and bury the
        relevant ones. The strongest moments and events come first, because a
        question about a video is usually a question about its highlights.
        """
        cap = limit or self._config.interaction.llm_fallback.max_input_records
        pool: dict[str, Evidence] = {}

        for index, moment in enumerate(self._knowledge.top_moments(project_id, limit=cap)):
            pool[f"m{index + 1}"] = Evidence(
                kind=EvidenceKind.MOMENT,
                id=moment.id,
                start_seconds=moment.start_seconds,
                end_seconds=moment.end_seconds,
                detail=(
                    f"{moment.moment_type} -- "
                    f"{'; '.join(moment.explanation) or 'no explanation recorded'}"
                ),
                score=moment.score,
            )

        remaining = max(0, cap - len(pool))
        for index, event in enumerate(self._knowledge.events(project_id, limit=remaining)):
            pool[f"e{index + 1}"] = Evidence(
                kind=EvidenceKind.GAME_EVENT,
                id=event.id,
                start_seconds=event.start_seconds,
                end_seconds=event.end_seconds,
                detail=f"{event.event_type} detected by {event.sources or 'one detector'}",
                score=event.confidence,
            )
        return pool

    def classify(self, question: str) -> QuestionKind:
        """Recognise the question shape. Order matters: specific before general."""
        lowered = normalise(question)

        # "why" is checked first: "why did you pick the clip at 12:34?" carries a
        # timestamp but is an explanation request, and Arabic "لماذا" contains
        # "ماذا" as a substring, so the reverse order would misread it.
        if re.search(r"(why|لماذا|ليش)", lowered):
            return QuestionKind.WHY_THIS_CLIP
        if _timestamp_in(lowered) is not None and re.search(
            r"(what|happen|حدث|يحدث|ماذا)", lowered
        ):
            return QuestionKind.WHAT_HAPPENED_AT
        if re.search(r"(how long|duration|length|مدة|طول|كم دقيقة)", lowered):
            return QuestionKind.EDIT_DURATION
        if re.search(r"(how many clips|clip count|كم لقطة|كم مقطع|عدد اللقطات)", lowered):
            return QuestionKind.CLIP_COUNT
        if re.search(r"(excluded|removed|left out|rejected|استبعدت|حذفت|استبعاد)", lowered):
            return QuestionKind.EXCLUDED_MOMENTS
        if re.search(r"(how many|how often|كم مرة|كم عدد|عدد)", lowered):
            return QuestionKind.COUNT_EVENTS
        if _type_in(lowered) is not None and re.search(
            r"(best|strongest|top|أفضل|افضل|أقوى|اقوى|أطرف|اطرف)", lowered
        ):
            return QuestionKind.BEST_OF_TYPE
        if re.search(r"(best|top|highlights|أفضل|افضل|أقوى|اقوى|أهم|اهم)\b", lowered) or re.search(
            r"(best|top)\s+\d+", lowered
        ):
            return QuestionKind.BEST_MOMENTS
        return QuestionKind.UNKNOWN

    # -- answers --------------------------------------------------------

    def _edit_duration(self, project_id: str, phrases: Phrasebook) -> Answer:
        """§18: the answer comes from the current EDL, not the source file."""
        seconds = self._knowledge.edit_duration_seconds(project_id)
        clips = self._knowledge.clip_count(project_id)
        if clips == 0:
            source = self._knowledge.source_duration_seconds(project_id)
            if source <= 0:
                return Answer(
                    text=phrases.say("edit_duration_unknown"),
                    confidence=0.9,
                    evidence=[Evidence(kind=EvidenceKind.PROJECT, detail="no clips, no probe")],
                )
            return Answer(
                text=phrases.say("edit_duration_none", duration=format_duration(source)),
                confidence=0.9,
                evidence=[
                    Evidence(
                        kind=EvidenceKind.PROJECT,
                        detail=f"source duration {format_duration(source)}",
                    )
                ],
            )
        return Answer(
            text=phrases.say("edit_duration", duration=format_duration(seconds), clips=clips),
            confidence=1.0,
            evidence=[
                Evidence(
                    kind=EvidenceKind.TIMELINE_CLIP,
                    start_seconds=0.0,
                    end_seconds=seconds,
                    detail=f"{clips} enabled clips on the video track",
                )
            ],
        )

    def _clip_count(self, project_id: str, phrases: Phrasebook) -> Answer:
        enabled = self._knowledge.clip_count(project_id)
        total = self._knowledge.clip_count(project_id, enabled_only=False)
        disabled = total - enabled
        suffix = phrases.say("clip_count_disabled", disabled=disabled) if disabled else ""
        return Answer(
            text=phrases.say("clip_count", clips=enabled, suffix=suffix),
            confidence=1.0,
            evidence=[
                Evidence(kind=EvidenceKind.TIMELINE_CLIP, detail=f"{total} clips on the timeline")
            ],
        )

    def _best_moments(self, project_id: str, phrases: Phrasebook) -> Answer:
        limit = self._requested_count() or self._config.interaction.qa.max_results
        moments = self._knowledge.top_moments(project_id, limit=limit)
        if not moments:
            return _no_data(phrases.say("best_moments_none"))
        lines = [
            f"{index}. {_describe(moment, phrases)}"
            for index, moment in enumerate(moments, start=1)
        ]
        return Answer(
            text=phrases.say("best_moments_heading") + "\n" + "\n".join(lines),
            confidence=_aggregate_confidence(moments),
            evidence=[_evidence(moment) for moment in moments],
        )

    def _best_of_type(self, project_id: str, question: str, phrases: Phrasebook) -> Answer:
        moment_type = _type_in(normalise(question))
        moments = self._knowledge.top_moments(project_id, limit=3, moment_type=moment_type)
        if not moments:
            return _no_data(
                phrases.say("best_of_type_none", moment_type=phrases.moment_type(str(moment_type)))
            )
        best = moments[0]
        return Answer(
            text=phrases.say(
                "best_of_type",
                moment_type=phrases.moment_type(str(moment_type)),
                description=_describe(best, phrases),
            ),
            confidence=_aggregate_confidence([best]),
            evidence=[_evidence(moment) for moment in moments],
        )

    def _why_this_clip(self, project_id: str, question: str, phrases: Phrasebook) -> Answer:
        """§12: reasons come from the stored breakdown, never invented."""
        moment = self._referenced_moment(project_id, question)
        if moment is None:
            return Answer(
                text=phrases.say("why_which_moment"),
                confidence=0.0,
                source=AnswerSource.UNAVAILABLE,
            )

        # §80's explanation is stored in English, because that is the record --
        # what the API serves and what the moments screen shows. For a reader in
        # another language it is *rebuilt* from the same stored facts by the
        # same rules the scorer used (backend/moments/explanation.py), rather
        # than translated: paraphrasing an explanation is how an explanation
        # stops being evidence.
        #
        # One line does not survive the rebuild. "Clip boundaries: ..." comes
        # from moment metadata the moments table has no column for, so an
        # Arabic answer is missing it where an English one has it.
        reasons = self._rebuild_reasons(moment, phrases) if phrases.is_arabic else []
        reasons = reasons or list(moment.explanation)
        if not reasons and moment.score_breakdown:
            reasons = [
                f"{phrases.dimension(name)} {value:.2f}"
                for name, value in sorted(
                    moment.score_breakdown.items(), key=lambda item: item[1], reverse=True
                )[:5]
                if value > 0
            ]
        if not reasons:
            return Answer(
                text=phrases.say(
                    "why_no_breakdown",
                    moment_type=phrases.moment_type(moment.moment_type),
                    score=f"{moment.score:.2f}",
                ),
                confidence=0.3,
                evidence=[_evidence(moment)],
            )

        penalties = []
        if moment.dead_time_score > 0:
            penalties.append(phrases.say("why_dead_time", value=f"{moment.dead_time_score:.2f}"))
        if moment.repetition_score > 0:
            penalties.append(phrases.say("why_repetition", value=f"{moment.repetition_score:.2f}"))

        body = "\n".join(f"+ {reason}" for reason in reasons)
        headline = phrases.say(
            "why_score_line",
            score=f"{moment.score:.2f}",
            confidence=f"{moment.confidence:.2f}",
        )
        text = f"{_describe(moment, phrases)}\n\n{headline}\n{body}"
        if penalties:
            text += "\n" + "\n".join(penalties)
        return Answer(
            text=text, confidence=max(moment.confidence, 0.5), evidence=[_evidence(moment)]
        )

    def _what_happened_at(self, project_id: str, question: str, phrases: Phrasebook) -> Answer:
        """§8: search a window around the timestamp across every data source."""
        timestamp = _timestamp_in(normalise(question))
        if timestamp is None:
            return Answer(
                text=phrases.say("when_give_timestamp"),
                confidence=0.0,
                source=AnswerSource.UNAVAILABLE,
            )

        window = self._config.interaction.qa.timestamp_context_seconds
        events = self._knowledge.events_at(project_id, timestamp, window_seconds=window)
        moments = self._knowledge.moments_at(project_id, timestamp, window_seconds=window)
        speech = self._knowledge.transcript_at(project_id, timestamp, window_seconds=window)

        if not (events or moments or speech):
            return Answer(
                text=phrases.say("when_nothing", timestamp=format_duration(timestamp)),
                confidence=0.4,
                evidence=[
                    Evidence(
                        kind=EvidenceKind.PROJECT,
                        start_seconds=max(timestamp - window, 0),
                        end_seconds=timestamp + window,
                        detail="no records in this window",
                    )
                ],
            )

        parts: list[str] = [phrases.say("when_heading", timestamp=format_duration(timestamp))]
        evidence: list[Evidence] = []
        for event in events:
            parts.append(
                phrases.say(
                    "when_event",
                    event_type=phrases.event_type(event.event_type),
                    start=format_duration(event.start_seconds),
                    confidence=f"{event.confidence:.2f}",
                )
            )
            evidence.append(
                Evidence(
                    kind=EvidenceKind.GAME_EVENT,
                    id=event.id,
                    start_seconds=event.start_seconds,
                    end_seconds=event.end_seconds,
                    detail=event.event_type,
                    score=event.confidence,
                )
            )
        for moment in moments:
            parts.append(
                phrases.say(
                    "when_moment",
                    moment_type=phrases.moment_type(moment.moment_type),
                    score=f"{moment.score:.2f}",
                )
            )
            evidence.append(_evidence(moment))
        for segment in speech:
            parts.append(phrases.say("when_speech", text=segment.text.strip()))
            evidence.append(
                Evidence(
                    kind=EvidenceKind.TRANSCRIPT,
                    id=segment.id,
                    start_seconds=segment.start_seconds,
                    end_seconds=segment.end_seconds,
                    detail=segment.text.strip()[:120],
                )
            )

        confidence = 0.85 if events or moments else 0.6
        return Answer(text="\n".join(parts), confidence=confidence, evidence=evidence)

    def _count_events(self, project_id: str, question: str, phrases: Phrasebook) -> Answer:
        lowered = normalise(question)
        event_type = _event_in(lowered)
        counts = self._knowledge.event_counts(project_id)
        if not counts:
            return _no_data(phrases.say("events_none"))

        if event_type is None:
            total = sum(counts.values())
            listing = phrases.join(
                [f"{phrases.event_type(name)}: {count}" for name, count in counts.items()]
            )
            return Answer(
                text=phrases.say("events_total", total=total, listing=listing),
                confidence=0.9,
                evidence=[
                    Evidence(kind=EvidenceKind.GAME_EVENT, detail=f"{name} x{count}")
                    for name, count in counts.items()
                ],
            )

        count = counts.get(event_type, 0)
        events = self._knowledge.events(project_id, event_type=event_type, limit=10)
        times = phrases.join([format_duration(event.start_seconds) for event in events[:5]])
        detail = phrases.say("events_at_times", times=times) if times else ""
        return Answer(
            text=phrases.say(
                "events_of_type",
                count=count,
                event_type=phrases.event_type(event_type),
                detail=detail,
            ),
            confidence=0.9 if count else 0.7,
            evidence=[
                Evidence(
                    kind=EvidenceKind.GAME_EVENT,
                    id=event.id,
                    start_seconds=event.start_seconds,
                    end_seconds=event.end_seconds,
                    detail=event.event_type,
                    score=event.confidence,
                )
                for event in events
            ]
            or [Evidence(kind=EvidenceKind.GAME_EVENT, detail=f"{event_type} x0")],
        )

    def _excluded_moments(self, project_id: str, phrases: Phrasebook) -> Answer:
        moments = self._knowledge.rejected_moments(
            project_id, limit=self._config.interaction.qa.max_results
        )
        if not moments:
            return Answer(
                text=phrases.say("excluded_none"),
                confidence=0.9,
                evidence=[Evidence(kind=EvidenceKind.PROJECT, detail="no rejected moments")],
            )
        lines = [
            f"- {_describe(moment, phrases)}"
            + (
                " "
                + phrases.say("why_dead_time", value=f"{moment.dead_time_score:.2f}").lstrip("- ")
                if moment.dead_time_score > 0
                else ""
            )
            + (
                " "
                + phrases.say("why_repetition", value=f"{moment.repetition_score:.2f}").lstrip("- ")
                if moment.repetition_score > 0
                else ""
            )
            for moment in moments
        ]
        return Answer(
            text=phrases.say("excluded_heading") + "\n" + "\n".join(lines),
            confidence=0.85,
            evidence=[_evidence(moment) for moment in moments],
        )

    # -- helpers --------------------------------------------------------

    def _rebuild_reasons(self, moment: MomentRecord, phrases: Phrasebook) -> list[str]:
        """Re-run the §80 rules over the stored facts, in ``phrases``'s language.

        Returns nothing when the breakdown was never stored, which leaves the
        caller on the stored English prose -- a real answer in the wrong
        language beats no answer.
        """
        dimensions = {
            name: value
            for name, value in moment.score_breakdown.items()
            if not name.startswith("_")
        }
        if not dimensions:
            return []
        # `sources` -- which detectors agreed (§27) -- is not stored on the
        # moment at all, only written into the English prose at scoring time.
        # So the Arabic answer is one line shorter than the English one, and
        # that is an honest gap rather than an invented sentence.
        facts = ReasonFacts(
            moment_type=moment.moment_type,
            dimensions=dimensions,
            confidence=moment.confidence,
            event_types=tuple(sorted(set(moment.event_types))),
            event_count=len(moment.event_types),
            dead_time=moment.score_breakdown.get("_penalty_dead_time", moment.dead_time_score),
            repetition=moment.score_breakdown.get(
                "_penalty_repetition", moment.repetition_score
            ),
            saturation=moment.score_breakdown.get("_penalty_saturation", 0.0),
            review_threshold=self._config.moments.scoring.needs_review_confidence,
        )
        return build_reasons(facts, phrases)

    def _referenced_moment(self, project_id: str, question: str) -> MomentRecord | None:
        """Find the moment a "why" question is about."""
        lowered = normalise(question)
        timestamp = _timestamp_in(lowered)
        if timestamp is not None:
            window = self._config.interaction.qa.timestamp_context_seconds
            candidates = self._knowledge.moments_at(project_id, timestamp, window_seconds=window)
            return max(candidates, key=lambda item: item.score) if candidates else None

        identifier = re.search(r"\bmom-[0-9a-f]{12}\b", lowered)
        if identifier:
            return self._knowledge.moment(identifier.group(0))

        # "why this one?" with no reference: the top moment is the sensible
        # assumption, and the evidence makes it clear which one was explained.
        top = self._knowledge.top_moments(project_id, limit=1)
        return top[0] if top else None

    @staticmethod
    def _requested_count() -> int | None:
        return None


def _describe(moment: MomentRecord, phrases: Phrasebook) -> str:
    return phrases.say(
        "moment_line",
        moment_type=phrases.moment_type(moment.moment_type),
        start=format_duration(moment.start_seconds),
        end=format_duration(moment.end_seconds),
        score=f"{moment.score:.2f}",
    )


def _evidence(moment: MomentRecord) -> Evidence:
    return Evidence(
        kind=EvidenceKind.MOMENT,
        id=moment.id,
        start_seconds=moment.start_seconds,
        end_seconds=moment.end_seconds,
        detail=moment.moment_type,
        score=moment.score,
    )


def _aggregate_confidence(moments: list[MomentRecord]) -> float:
    if not moments:
        return 0.0
    return min(sum(moment.confidence for moment in moments) / len(moments), 1.0)


def _not_ready(kind: QuestionKind, phrases: Phrasebook) -> Answer:
    """§17: refuse rather than guess before the analysis exists."""
    return Answer(
        text=phrases.say("not_analysed"),
        confidence=0.0,
        source=AnswerSource.UNAVAILABLE,
        requires_analysis=True,
        evidence=[Evidence(kind=EvidenceKind.PROJECT, detail=f"question kind: {kind.value}")],
    )


def _no_data(message: str) -> Answer:
    return Answer(
        text=message,
        confidence=0.5,
        evidence=[Evidence(kind=EvidenceKind.PROJECT, detail="query returned no rows")],
    )


def _timestamp_in(lowered: str) -> float | None:
    match = re.search(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b", lowered)
    if not match:
        return None
    first, second, third = match.group(1), match.group(2), match.group(3)
    if third is not None:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


def _type_in(lowered: str) -> str | None:
    for name, keywords in _TYPE_WORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return name
    return None


def _event_in(lowered: str) -> str | None:
    for name, keywords in _EVENT_WORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return name
    return None


__all__ = ["QuestionAnswering", "QuestionKind"]
