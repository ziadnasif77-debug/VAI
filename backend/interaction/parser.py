"""Rule-based parsing of user messages (§2, §9, §15, §20).

Two design rules shape this module.

**Deterministic first.** §20 asks for maximum intelligence at minimum expensive
inference. Most editing instructions and commands are formulaic -- "focus on
clutches", "delete clip 5", "make it 25 minutes" -- and a rule can read them
exactly, instantly, offline, and identically every time. An LLM is the fallback
for what the rules cannot read, not the default path.

**Relative changes stay relative.** "Make it faster" and "shorter" have no
meaning without the current intent, so the parser reports the *shift* and the
service resolves it. The parser never reads state.

Both Arabic and English are handled, because the product is used in both.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from backend.core.models.enums import GameEventType, MomentType
from backend.interaction.models import (
    CaptionPolicy,
    CommandKind,
    DeadTimePolicy,
    EditCommand,
    EffectsLevel,
    IntentDelta,
    InteractionType,
    Level,
    ListDelta,
    MusicPolicy,
    Pacing,
)

#: Ordered scale used to resolve relative pacing shifts ("faster", "أسرع").
PACING_SCALE: tuple[Pacing, ...] = (Pacing.SLOW, Pacing.MEDIUM, Pacing.FAST, Pacing.VERY_FAST)

#: Ordered scale for effects, so "fewer effects" is one step, not a jump to none.
EFFECTS_SCALE: tuple[EffectsLevel, ...] = (
    EffectsLevel.NONE,
    EffectsLevel.MINIMAL,
    EffectsLevel.MODERATE,
    EffectsLevel.HEAVY,
)

#: Proportion applied by a relative duration request ("shorter", "أقصر").
RELATIVE_DURATION_STEP = 0.8

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

_QUESTION_MARKERS = (
    "?", "؟",
    "what", "which", "why", "when", "who", "where", "list",
    "how", "how many", "how much", "how long",
    "show me", "tell me", "give me",
    "ما ", "ماذا", "لماذا", "متى", "كم ", "أي ", "اي ", "هل ", "كيف", "أين", "اين",
    "أعطني", "اعطني", "اذكر",
)

_COMMAND_MARKERS = (
    "delete", "remove", "restore", "undo", "revert", "add clip", "add moment",
    "احذف", "امسح", "ازل", "أزل", "استعد", "ارجع", "أرجع", "تراجع", "اضف", "أضف",
)

_NEGATIONS = (
    "no ", "not ", "don't", "do not", "without", "less", "fewer", "reduce", "minimal",
    "لا ", "بدون", "قلل", "خفف", "أقل", "اقل", "ما تستخدم",
)

#: Moment-type vocabulary in both languages.
_MOMENT_KEYWORDS: dict[MomentType, tuple[str, ...]] = {
    MomentType.CLUTCH: ("clutch", "كلتش", "كلاتش"),
    MomentType.EPIC: ("epic", "ملحمي", "أسطوري", "اسطوري"),
    MomentType.FUNNY: ("funny", "comedy", "hilarious", "مضحك", "مضحكة", "طريف", "كوميدي"),
    MomentType.FAIL: ("fail", "فشل", "إخفاق", "اخفاق"),
    MomentType.REACTION: ("reaction", "ردة فعل", "ردود الفعل", "رد فعل"),
    MomentType.SKILL: ("skill", "skillful", "مهارة", "مهارات"),
    MomentType.OUTPLAY: ("outplay", "تفوق", "خداع"),
    MomentType.CHAOS: ("chaos", "فوضى"),
    MomentType.TENSION: ("tension", "tense", "توتر", "إثارة", "اثارة"),
    MomentType.SURPRISE: ("surprise", "مفاجأة", "مفاجئ"),
    MomentType.RAGE: ("rage", "غضب", "عصبية"),
    MomentType.COMEBACK: ("comeback", "عودة", "انتفاضة"),
    MomentType.BOSS: ("boss", "زعيم", "بوس"),
    MomentType.VICTORY: ("victory", "win", "فوز", "انتصار"),
    MomentType.DEFEAT: ("defeat", "loss", "خسارة", "هزيمة"),
    MomentType.DISCOVERY: ("discovery", "اكتشاف"),
    MomentType.RARE: ("rare", "نادر"),
}

_EVENT_KEYWORDS: dict[GameEventType, tuple[str, ...]] = {
    GameEventType.MULTI_KILL: ("multi kill", "multi-kill", "multikill", "قتل متعدد", "ملتي كيل"),
    GameEventType.KILL: ("kill", "kills", "قتل", "قتلات", "كيل"),
    GameEventType.DEATH: ("death", "deaths", "موت", "مت ", "وفاة"),
    GameEventType.CLUTCH: ("clutch", "كلتش"),
    GameEventType.VICTORY: ("victory", "فوز", "انتصار"),
    GameEventType.DEFEAT: ("defeat", "خسارة", "هزيمة"),
    GameEventType.COMEBACK: ("comeback", "عودة"),
    GameEventType.BOSS_DEFEAT: ("boss", "زعيم"),
    GameEventType.RARE_LOOT: ("rare loot", "loot", "غنيمة", "نادر"),
}


@dataclass(slots=True)
class ParsedInstruction:
    """An editing instruction read from natural language.

    ``pacing_shift``, ``effects_shift`` and ``duration_multiplier`` express
    *relative* requests. The service resolves them against the current intent,
    because the parser deliberately holds no state.
    """

    delta: IntentDelta
    pacing_shift: int = 0
    effects_shift: int = 0
    duration_multiplier: float | None = None
    confidence: float = 0.0
    matched: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return (
            self.delta.is_empty()
            and not self.pacing_shift
            and not self.effects_shift
            and self.duration_multiplier is None
        )


def normalise(text: str) -> str:
    """Lowercase, convert Arabic-Indic digits, strip diacritics and tatweel."""
    converted = text.translate(_ARABIC_DIGITS)
    decomposed = unicodedata.normalize("NFKD", converted)
    stripped = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character) and character != "ـ"
    )
    return unicodedata.normalize("NFKC", stripped).lower().strip()


def classify(text: str) -> InteractionType:
    """Decide what kind of message this is (§15).

    Commands are checked first: "delete clip 5" contains no interrogative and
    would otherwise fall through to an instruction. Questions next. Anything
    left is an editing instruction, which is the safe default -- it changes
    intent, which is reversible, rather than the EDL.
    """
    lowered = normalise(text)
    if not lowered:
        return InteractionType.QUESTION

    if parse_command(text) is not None:
        return InteractionType.COMMAND
    if any(marker in lowered for marker in _COMMAND_MARKERS) and _references_edit(lowered):
        return InteractionType.COMMAND
    if lowered.endswith(("?", "؟")) or any(
        lowered.startswith(marker.strip()) or f" {marker.strip()} " in f" {lowered} "
        for marker in _QUESTION_MARKERS
        if marker not in {"?", "؟"}
    ):
        return InteractionType.QUESTION
    return InteractionType.EDITING_INSTRUCTION


def _references_edit(lowered: str) -> bool:
    """Whether a delete/restore verb is aimed at something in the edit."""
    return bool(
        re.search(r"\b(clip|clips|لقطة|مقطع|كليب)\b", lowered)
        or _find_timestamp(lowered) is not None
    )


def parse_command(text: str) -> EditCommand | None:
    """Read an unambiguous edit command, or return ``None``.

    Only exact, safe forms are recognised. Anything vaguer is left to the
    instruction path, where it changes intent instead of deleting footage.
    """
    lowered = normalise(text)
    if not lowered:
        return None

    if re.search(r"(revert|undo|rollback|ارجع|أرجع|تراجع|استعد)", lowered):
        version = re.search(r"(?:version|v|إصدار|اصدار)\s*(\d+)", lowered)
        if version:
            return EditCommand(
                kind=CommandKind.REVERT_VERSION, version=int(version.group(1)), raw_text=text
            )

    delete = re.search(r"(delete|remove|احذف|امسح|ازل|أزل)", lowered)
    restore = re.search(r"(restore|keep back|استعد|ارجع)", lowered)

    clip_index = re.search(r"(?:clip|لقطة|مقطع|كليب)\s*#?\s*(\d+)", lowered)
    timestamp = _find_timestamp(lowered)

    if delete and clip_index:
        return EditCommand(
            kind=CommandKind.DELETE_CLIP, clip_index=int(clip_index.group(1)), raw_text=text
        )
    if restore and clip_index:
        return EditCommand(
            kind=CommandKind.RESTORE_CLIP, clip_index=int(clip_index.group(1)), raw_text=text
        )
    if delete and timestamp is not None:
        return EditCommand(
            kind=CommandKind.DELETE_AT_TIMESTAMP, timestamp_seconds=timestamp, raw_text=text
        )
    if re.search(r"(add|أضف|اضف)", lowered) and timestamp is not None:
        return EditCommand(
            kind=CommandKind.ADD_MOMENT, timestamp_seconds=timestamp, raw_text=text
        )
    return None


def parse_instruction(text: str) -> ParsedInstruction:
    """Read an editing instruction into a structured delta.

    Returns an empty result when nothing is recognised; the caller then decides
    whether to fall back to an LLM.
    """
    lowered = normalise(text)
    delta = IntentDelta()
    matched: list[str] = []
    pacing_shift = 0
    effects_shift = 0
    duration_multiplier: float | None = None

    changes: dict[str, object] = {}

    minutes = _find_minutes(lowered)
    if minutes is not None:
        changes["target_duration_seconds"] = round(minutes * 60)
        matched.append("duration")
    elif re.search(r"(shorter|shorten|أقصر|اقصر|قصر)", lowered):
        duration_multiplier = RELATIVE_DURATION_STEP
        matched.append("duration_shorter")
    elif re.search(r"(longer|أطول|اطول)", lowered):
        duration_multiplier = 1 / RELATIVE_DURATION_STEP
        matched.append("duration_longer")

    if re.search(r"(faster|أسرع|اسرع|سريع|fast|energetic|حماسي)", lowered):
        if re.search(r"(faster|أسرع|اسرع)", lowered):
            pacing_shift = 1
        else:
            changes["pacing"] = Pacing.FAST
        matched.append("pacing_fast")
    elif re.search(r"(slower|أبطأ|ابطأ|بطيء|slow|calm|هادئ)", lowered):
        if re.search(r"(slower|أبطأ|ابطأ)", lowered):
            pacing_shift = -1
        else:
            changes["pacing"] = Pacing.SLOW
        matched.append("pacing_slow")

    if re.search(r"(cinematic|سينمائي|سينمائية)", lowered):
        changes.setdefault("pacing", Pacing.SLOW)
        changes["style"] = "cinematic"
        changes.setdefault("effects", EffectsLevel.MINIMAL)
        matched.append("style_cinematic")

    negated = any(marker in lowered for marker in _NEGATIONS)

    if re.search(r"(effect|effects|مؤثر|مؤثرات|تأثير)", lowered):
        if re.search(r"\b(no|none|zero|بدون|بلا)\b", lowered):
            changes["effects"] = EffectsLevel.NONE
        elif negated:
            effects_shift = -1
        else:
            changes["effects"] = EffectsLevel.HEAVY
        matched.append("effects")

    if re.search(r"(caption|captions|subtitle|subtitles|ترجمة|نصوص|كتابة)", lowered):
        if re.search(r"(important|only|مهم|مهمة|فقط)", lowered):
            changes["captions"] = CaptionPolicy.IMPORTANT_ONLY
        elif negated:
            changes["captions"] = CaptionPolicy.NONE
        else:
            changes["captions"] = CaptionPolicy.FULL
        matched.append("captions")

    if re.search(r"(music|موسيقى|موسيقا)", lowered):
        changes["music"] = MusicPolicy.NONE if negated else MusicPolicy.PROMINENT
        matched.append("music")

    if re.search(r"(dead time|boring|dull|ممل|الممل|فراغ|وقت ميت|مملة)", lowered):
        changes["dead_time_policy"] = DeadTimePolicy.AGGRESSIVE
        matched.append("dead_time")

    if re.search(r"(repetition|repeated|duplicate|تكرار|المكرر|مكرر)", lowered):
        delta = delta.model_copy(update={"custom": {**delta.custom, "repetition": "aggressive"}})
        matched.append("repetition")

    if re.search(r"(context|سياق|السياق)", lowered):
        # "keep the context" and "don't cut the context" both mean: protect it.
        changes["context_preservation"] = Level.HIGH
        matched.append("context")

    priorities, avoided = _moment_preferences(lowered)
    if priorities:
        delta = delta.model_copy(
            update={"priority_moment_types": ListDelta(add=sorted(priorities))}
        )
        matched.append("priority_moments")
    if avoided:
        delta = delta.model_copy(
            update={"avoid_moment_types": ListDelta(add=sorted(avoided))}
        )
        matched.append("avoid_moments")

    events = _event_preferences(lowered)
    if events:
        delta = delta.model_copy(update={"priority_events": ListDelta(add=sorted(events))})
        matched.append("priority_events")

    if changes:
        delta = delta.model_copy(update=changes)

    confidence = 0.0 if not matched else min(0.55 + 0.1 * len(matched), 0.95)
    return ParsedInstruction(
        delta=delta,
        pacing_shift=pacing_shift,
        effects_shift=effects_shift,
        duration_multiplier=duration_multiplier,
        confidence=confidence,
        matched=matched,
    )


def _moment_preferences(lowered: str) -> tuple[set[str], set[str]]:
    """Split mentioned moment types into wanted and unwanted."""
    priorities: set[str] = set()
    avoided: set[str] = set()
    for moment_type, keywords in _MOMENT_KEYWORDS.items():
        for keyword in keywords:
            index = lowered.find(keyword)
            if index < 0:
                continue
            window = lowered[max(0, index - 30) : index]
            if any(marker in window for marker in _NEGATIONS):
                avoided.add(moment_type.value)
            else:
                priorities.add(moment_type.value)
            break
    return priorities - avoided, avoided - priorities


def _event_preferences(lowered: str) -> set[str]:
    """Game events the user asked to prioritise."""
    found: set[str] = set()
    for event_type, keywords in _EVENT_KEYWORDS.items():
        for keyword in keywords:
            index = lowered.find(keyword)
            if index < 0:
                continue
            window = lowered[max(0, index - 30) : index]
            if not any(marker in window for marker in _NEGATIONS):
                found.add(event_type.value)
            break
    # "multi kill" also matches "kill"; the more specific one is what was meant.
    if GameEventType.MULTI_KILL.value in found:
        found.discard(GameEventType.KILL.value)
    return found


def _find_minutes(lowered: str) -> float | None:
    """Read an explicit duration such as "25 minutes" or "٢٥ دقيقة"."""
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(minutes|minute|mins|min|دقيقة|دقائق|د)\b", lowered)
    if match:
        return float(match.group(1).replace(",", "."))
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(hours|hour|ساعة|ساعات)\b", lowered)
    if match:
        return float(match.group(1).replace(",", ".")) * 60
    return None


def _find_timestamp(lowered: str) -> float | None:
    """Read a timestamp such as ``12:34`` or ``1:02:03``."""
    match = re.search(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b", lowered)
    if not match:
        return None
    first, second, third = match.group(1), match.group(2), match.group(3)
    if third is not None:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


def shift_pacing(current: Pacing, steps: int) -> Pacing:
    """Move ``current`` along the pacing scale, clamped at both ends."""
    index = PACING_SCALE.index(current)
    return PACING_SCALE[max(0, min(index + steps, len(PACING_SCALE) - 1))]


def shift_effects(current: EffectsLevel, steps: int) -> EffectsLevel:
    """Move ``current`` along the effects scale, clamped at both ends."""
    index = EFFECTS_SCALE.index(current)
    return EFFECTS_SCALE[max(0, min(index + steps, len(EFFECTS_SCALE) - 1))]


__all__ = [
    "EFFECTS_SCALE",
    "PACING_SCALE",
    "RELATIVE_DURATION_STEP",
    "ParsedInstruction",
    "classify",
    "normalise",
    "parse_command",
    "parse_instruction",
    "shift_effects",
    "shift_pacing",
]
