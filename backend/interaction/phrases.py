"""What the editor says, in the language it was spoken to (SPEC §57, §63).

The parser has read Arabic since Phase 13 -- "احذف اللقطة ٣" and "delete clip 3"
have always produced the same command. The answers did not: they were English
f-strings built at the point of use, so an Arabic question got an English reply
and the conversation only worked in one direction.

**The language is the person's, not a setting.** Nothing here reads
configuration. Whichever language the message was written in is the language
the answer comes back in, decided per message, because someone who types one
Arabic question inside an English session wants that one answered in Arabic.

**A phrase is a key, and a key has both languages.** A test walks the whole
catalogue and fails if either is missing, which is what stops half-translated
replies -- an answer that begins in Arabic and ends in English reads as broken
software rather than as a partial feature.

**Vocabulary is translated too.** A reply that says "أقوى لحظة tension" is not
an Arabic reply. Moment types, event types and the ten score dimensions all
carry an Arabic name here, and an unknown one falls back to its raw value
rather than disappearing.

Numbers, timestamps and scores are left as they are. `9:59` and `0.54` read the
same in both languages, and localising digits would make a value that a person
might quote back to the timeline unrecognisable.
"""

from __future__ import annotations

import re
from typing import Final, Literal

Language = Literal["ar", "en"]

#: Arabic script. Enough to tell which language a chat message is in -- the
#: question is never "which of a hundred languages", only "did they write
#: Arabic or not".
_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")

DEFAULT_LANGUAGE: Final[Language] = "en"


def language_of(text: str | None) -> Language:
    """The language to answer in: the one the person wrote in.

    A message mixing scripts ("احذف clip 3") counts as Arabic, because the
    Arabic is the part they wrote rather than the part they copied.
    """
    return "ar" if text and _ARABIC.search(text) else DEFAULT_LANGUAGE


#: Every line the chat can say, in both languages. Keys are grouped by the
#: module that says them. `{name}` placeholders must match across languages --
#: :func:`say` raises on a missing one rather than emitting a broken sentence.
PHRASES: Final[dict[str, dict[Language, str]]] = {
    # -- questions about the edit (§18) ---------------------------------
    "edit_duration": {
        "en": "The current edit is {duration} across {clips} clips.",
        "ar": "المونتاج الحالي {duration} على {clips} لقطة.",
    },
    "edit_duration_none": {
        "en": "No edit has been generated yet. The source material is {duration} long.",
        "ar": "لم يُبنَ أي مونتاج بعد. مدة التسجيل الأصلي {duration}.",
    },
    "edit_duration_unknown": {
        "en": "No edit exists yet, and no source duration has been measured.",
        "ar": "لا يوجد مونتاج بعد، ولم تُقَس مدة التسجيل الأصلي.",
    },
    "clip_count": {
        "en": "The current edit has {clips} clips{suffix}.",
        "ar": "المونتاج الحالي فيه {clips} لقطة{suffix}.",
    },
    "clip_count_disabled": {
        "en": " ({disabled} disabled)",
        "ar": " (منها {disabled} معطّلة)",
    },
    # -- moments (§32, §80) ---------------------------------------------
    "best_moments_heading": {
        "en": "Top moments by score:",
        "ar": "أقوى اللحظات حسب التقييم:",
    },
    "best_moments_none": {
        "en": "No moments have been detected in this project yet.",
        "ar": "لم تُكتشف أي لحظات في هذا المشروع بعد.",
    },
    "best_of_type": {
        "en": "The strongest {moment_type} moment is {description}.",
        "ar": "أقوى لحظة {moment_type} هي {description}.",
    },
    "best_of_type_none": {
        "en": "No '{moment_type}' moments were detected in this project.",
        "ar": "لم تُكتشف أي لحظة من نوع «{moment_type}» في هذا المشروع.",
    },
    "moment_line": {
        "en": "{moment_type} at {start}-{end} (score {score})",
        "ar": "{moment_type} في {start}-{end} (تقييم {score})",
    },
    "excluded_heading": {
        "en": "Excluded moments:",
        "ar": "اللحظات المستبعدة:",
    },
    "excluded_none": {
        "en": "Nothing has been excluded from the edit so far.",
        "ar": "لم يُستبعد شيء من المونتاج حتى الآن.",
    },
    # -- why this clip (§12, §80) ---------------------------------------
    "why_which_moment": {
        "en": "Tell me which moment you mean -- a timestamp such as 12:34, or its id.",
        "ar": "أخبرني أي لحظة تقصد — وقت مثل 12:34، أو معرّفها.",
    },
    "why_no_breakdown": {
        "en": (
            "This {moment_type} moment scored {score}, but no per-dimension breakdown "
            "was stored for it, so I cannot explain why."
        ),
        "ar": (
            "لحظة {moment_type} هذه نالت {score}، لكن لم يُحفَظ لها تفصيل حسب الأبعاد، "
            "فلا أستطيع تفسير السبب."
        ),
    },
    "why_score_line": {
        "en": "Score {score} (confidence {confidence})",
        "ar": "التقييم {score} (الثقة {confidence})",
    },
    "why_dead_time": {
        "en": "- dead time {value}",
        "ar": "- وقت ميّت {value}",
    },
    "why_repetition": {
        "en": "- repetition {value}",
        "ar": "- تكرار {value}",
    },
    # -- what happened at (§8) ------------------------------------------
    "when_give_timestamp": {
        "en": "Give me a timestamp, for example 12:34.",
        "ar": "أعطني وقتاً، مثلاً 12:34.",
    },
    "when_heading": {
        "en": "Around {timestamp}:",
        "ar": "حوالي {timestamp}:",
    },
    "when_nothing": {
        "en": (
            "Nothing was detected around {timestamp}. That usually means the section is "
            "quiet gameplay with no notable event."
        ),
        "ar": ("لم يُكتشف شيء حول {timestamp}. غالباً يعني أن هذا المقطع لعب هادئ بلا حدث بارز."),
    },
    "when_event": {
        "en": "- {event_type} at {start} (confidence {confidence})",
        "ar": "- {event_type} في {start} (ثقة {confidence})",
    },
    "when_moment": {
        "en": "- part of a {moment_type} moment (score {score})",
        "ar": "- جزء من لحظة {moment_type} (تقييم {score})",
    },
    "when_speech": {
        "en": '- speech: "{text}"',
        "ar": "- كلام: «{text}»",
    },
    # -- counting events -------------------------------------------------
    "events_none": {
        "en": "No game events have been detected yet.",
        "ar": "لم تُكتشف أي أحداث في اللعبة بعد.",
    },
    "events_total": {
        "en": "{total} events detected in total ({listing}).",
        "ar": "اكتُشف {total} حدثاً في المجموع ({listing}).",
    },
    "events_of_type": {
        "en": "{count} {event_type} event(s) detected{detail}.",
        "ar": "اكتُشف {count} حدث {event_type}{detail}.",
    },
    "events_at_times": {
        "en": " at {times}",
        "ar": " في {times}",
    },
    # -- refusals and readiness ------------------------------------------
    "not_analysed": {
        "en": (
            "Video analysis is not complete yet, so I have no data to answer that from. "
            "Run the analysis first."
        ),
        "ar": "تحليل الفيديو لم يكتمل بعد، فليست لديّ بيانات أجيب منها. شغّل التحليل أولاً.",
    },
    "question_unreadable": {
        "en": (
            "I can answer questions about the detected moments, events, the current edit, "
            "and what happened at a given timestamp. I could not read this one.{suffix}"
        ),
        "ar": (
            "أستطيع الإجابة عن اللحظات المكتشفة والأحداث والمونتاج الحالي وما حدث عند وقت "
            "معيّن. لم أفهم هذا السؤال.{suffix}"
        ),
    },
    # -- why a moment scored what it did (§80) ---------------------------
    "reason_strongest": {
        "en": "Strongest on {dimensions}.",
        "ar": "الأقوى في {dimensions}.",
    },
    "reason_agreement": {
        "en": "{count} independent detectors agreed on this ({sources}).",
        "ar": "اتفق {count} كاشفات مستقلة على هذه اللحظة ({sources}).",
    },
    "reason_single_source": {
        "en": "Detected by {source} alone, so confidence is limited.",
        "ar": "اكتشفها {source} وحده، فالثقة محدودة.",
    },
    "reason_events": {
        "en": "Contains {count} event(s): {types}.",
        "ar": "تحتوي على {count} حدث: {types}.",
    },
    "reason_dead_time": {
        "en": "Penalised: {percent} of the clip is dead time.",
        "ar": "خُصم منها: {percent} من اللقطة وقت ميّت.",
    },
    "reason_repetition": {
        "en": "Penalised: similar moments appear elsewhere in the recording.",
        "ar": "خُصم منها: توجد لحظات مشابهة في مواضع أخرى من التسجيل.",
    },
    "reason_saturation": {
        "en": "Penalised for variety: {moment_type} moments already dominate the selection.",
        "ar": "خُصم منها للتنويع: لحظات {moment_type} تهيمن على الاختيار أصلاً.",
    },
    "reason_needs_review": {
        "en": (
            "Confidence {confidence} is below the review threshold, so this is flagged "
            "for a human to check."
        ),
        "ar": "الثقة {confidence} دون عتبة المراجعة، فهذه مُعلَّمة ليراجعها إنسان.",
    },
    "reason_boundaries": {
        "en": "Clip boundaries: {notes}.",
        "ar": "حدود اللقطة: {notes}.",
    },
    # -- commands (§42, §63) ---------------------------------------------
    "removed_clip": {
        "en": "Removed clip {clip} from the edit.",
        "ar": "أزلتُ اللقطة {clip} من المونتاج.",
    },
    "restored_clip": {
        "en": "Restored clip {clip}.",
        "ar": "أعدتُ اللقطة {clip}.",
    },
    "removed_at": {
        "en": "Removed {count} clip(s) covering that timestamp.",
        "ar": "أزلتُ {count} لقطة تغطي ذلك الوقت.",
    },
    "trimmed_clip": {
        "en": "Trimmed clip {clip}: {start}s at the start, {end}s at the end.",
        "ar": "قصصتُ اللقطة {clip}: {start} ثانية من البداية، {end} ثانية من النهاية.",
    },
    "split_clip": {
        "en": "Split clip {clip} at {at}.",
        "ar": "قسّمتُ اللقطة {clip} عند {at}.",
    },
    "moved_clip": {
        "en": "Moved clip {clip} to position {to}.",
        "ar": "نقلتُ اللقطة {clip} إلى الموضع {to}.",
    },
    "command_examples": {
        "en": (
            "Try 'delete clip 5', 'trim 2 seconds off the end of clip 3', "
            "'split clip 4 at 1:20', or 'move clip 2 to position 5'."
        ),
        "ar": (
            "جرّب «احذف اللقطة 5» أو «اقصص ثانيتين من نهاية اللقطة 3» أو "
            "«قسّم اللقطة 4 عند 1:20» أو «انقل اللقطة 2 إلى الموضع 5»."
        ),
    },
    "command_unreadable": {
        "en": "I could not read that as an edit command. {examples}",
        "ar": "لم أفهم ذلك كأمر تعديل. {examples}",
    },
    "command_unreadable_because": {
        "en": "I could not read that as an edit command, and {reason}. {examples}",
        "ar": "لم أفهم ذلك كأمر تعديل، و{reason}. {examples}",
    },
    "command_read_but": {
        "en": "I read that, but {reason}.",
        "ar": "فهمتُ ذلك، لكن {reason}.",
    },
    "brief_updated": {
        "en": "Updated the editing brief: {summary}.",
        "ar": "حدّثتُ خطة المونتاج: {summary}.",
    },
    # The one-line brief summary (§4), assembled from these pieces.
    "brief_target": {"en": "target {minutes} min", "ar": "المدة {minutes} دقيقة"},
    "brief_pacing": {"en": "pacing {value}", "ar": "الإيقاع {value}"},
    "brief_effects": {"en": "effects {value}", "ar": "المؤثرات {value}"},
    "brief_priority": {"en": "prioritising {types}", "ar": "بأولوية لـ{types}"},
    "brief_avoid": {"en": "avoiding {types}", "ar": "مع تجنّب {types}"},
    "brief_dead_time": {"en": "dead time {value}", "ar": "الوقت الميّت {value}"},
    "brief_chronological": {
        "en": "in time order (no hook)",
        "ar": "بالترتيب الزمني (بلا خطّاف)",
    },
    "instruction_unreadable": {
        "en": (
            "I did not recognise an editing preference in that. Try something like "
            "'focus on clutches', 'fewer effects', or 'make it 25 minutes'."
        ),
        "ar": (
            "لم أتعرّف على تفضيل تحريري في ذلك. جرّب شيئاً مثل «ركّز على اللحظات الحاسمة» "
            "أو «مؤثرات أقل» أو «اجعله 25 دقيقة»."
        ),
    },
}

#: Moment types (§32) in Arabic. A reply saying "أقوى لحظة tension" is not an
#: Arabic reply.
MOMENT_TYPES: Final[dict[str, str]] = {
    "epic": "ملحمية",
    "clutch": "حاسمة",
    "funny": "مضحكة",
    "fail": "فاشلة",
    "reaction": "ردّ فعل",
    "skill": "مهارة",
    "outplay": "تفوّق",
    "chaos": "فوضى",
    "tension": "توتّر",
    "surprise": "مفاجأة",
    "rage": "غضب",
    "comeback": "عودة",
    "boss": "زعيم",
    "victory": "انتصار",
    "defeat": "هزيمة",
    "discovery": "اكتشاف",
    "rare": "نادرة",
}

#: Game events (§21, §26).
EVENT_TYPES: Final[dict[str, str]] = {
    "death": "موت",
    "kill": "قتل",
    "multi_kill": "قتل متعدد",
    "low_health": "صحة منخفضة",
    "unexpected_event": "حدث مفاجئ",
    "escape": "نجاة",
    "funny_moment": "لحظة مضحكة",
    "victory": "انتصار",
    "defeat": "هزيمة",
    "level_up": "ترقية مستوى",
    "wanted_level": "مستوى المطلوبية",
    "crash": "اصطدام",
    "chase": "مطاردة",
}

#: Values that appear in the editing brief (§4): pacing, effects level and the
#: dead-time policy. Named rather than left as enum spellings, because "الإيقاع
#: very_fast" is not a sentence in either language.
BRIEF_VALUES: Final[dict[str, str]] = {
    "very_slow": "بطيء جداً",
    "slow": "بطيء",
    "medium": "متوسط",
    "fast": "سريع",
    "very_fast": "سريع جداً",
    "none": "بلا",
    "minimal": "قليلة",
    "moderate": "معتدلة",
    "heavy": "كثيفة",
    "remove": "حذف",
    "keep": "إبقاء",
    "trim": "تقصير",
}

#: The ten scoring dimensions (§32), as they appear in an explanation.
DIMENSIONS: Final[dict[str, str]] = {
    "gameplay": "اللعب",
    "visual": "الصورة",
    "audio": "الصوت",
    "reaction": "ردّ الفعل",
    "novelty": "الجِدّة",
    "skill": "المهارة",
    "emotion": "الانفعال",
    "narrative": "السرد",
    "context": "السياق",
    "entertainment": "المتعة",
}


class Phrasebook:
    """Says things in one language, chosen once per message.

    Carried rather than looked up globally: two people can hold a conversation
    with the same running application, and a process-wide "current language"
    would let one of them change the other's replies.
    """

    __slots__ = ("language",)

    def __init__(self, language: Language = DEFAULT_LANGUAGE) -> None:
        self.language = language

    @classmethod
    def for_message(cls, text: str | None) -> Phrasebook:
        """A phrasebook matching the language ``text`` was written in."""
        return cls(language_of(text))

    @property
    def is_arabic(self) -> bool:
        return self.language == "ar"

    def say(self, key: str, **params: object) -> str:
        """Render one phrase.

        Raises:
            KeyError: if the phrase is not in the catalogue. A missing phrase
                is a bug to fix rather than a sentence to guess at, and it is
                caught by the test that walks every key.
        """
        try:
            template = PHRASES[key][self.language]
        except KeyError as exc:
            raise KeyError(
                f"No {self.language!r} phrase for {key!r}. Add it to "
                "backend/interaction/phrases.py:PHRASES."
            ) from exc
        return template.format(**params)

    def moment_type(self, value: str) -> str:
        """A moment type in this language, or the raw value if it has no name yet."""
        if not self.is_arabic:
            return value
        return MOMENT_TYPES.get(value, value)

    def event_type(self, value: str) -> str:
        if not self.is_arabic:
            return value
        return EVENT_TYPES.get(value, value)

    def brief_value(self, value: str) -> str:
        """A pacing, effects or dead-time value as a word rather than an enum."""
        if not self.is_arabic:
            return value.replace("_", " ")
        return BRIEF_VALUES.get(value, value.replace("_", " "))

    def dimension(self, value: str) -> str:
        """A score dimension, spaced rather than underscored in either language."""
        if not self.is_arabic:
            return value.replace("_", " ")
        return DIMENSIONS.get(value, value.replace("_", " "))

    def join(self, items: list[str]) -> str:
        """Join a list the way the language does."""
        return "، ".join(items) if self.is_arabic else ", ".join(items)


__all__ = [
    "BRIEF_VALUES",
    "DEFAULT_LANGUAGE",
    "DIMENSIONS",
    "EVENT_TYPES",
    "MOMENT_TYPES",
    "PHRASES",
    "Language",
    "Phrasebook",
    "language_of",
]
