"""The video's own words: title and thumbnail hook written by the model.

Owner instruction (2026-08-28), verbatim in spirit: *phrases picked for each
video, not one phrase for all videos*. The deterministic tables in
:mod:`backend.metadata.generation` and :mod:`backend.metadata.hooks` write a
correct sentence, and wrote the same correct sentence twice in one day. This
module hands the sentence to the 7B that already directs and critiques the
edit, with the video's own evidence in the prompt -- the game, what happened
and how often, the creature names OCR read off the screen, a line of what was
said -- and a temperature high enough that similar evidence still phrases
itself differently.

The tables stay, demoted to what they always should have been: the §95
fallback for a machine without a model, and the floor under an answer that
fails validation. Validation is shape, not taste: Ollama's grammar cannot
enforce lengths (measured in the maxLength incident), so the bounds live
here -- and an Arabic channel rejects a Latin-only answer outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from backend.core.errors import GamingEditorError
from backend.core.logging import LogChannel, get_logger
from backend.core.prompts import load_prompt

logger = get_logger("metadata.creative", LogChannel.PIPELINE)

_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["title", "hook_top", "hook_bottom"],
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "maxLength": 120},
        "hook_top": {"type": "string", "maxLength": 30},
        "hook_bottom": {"type": "string", "maxLength": 40},
    },
}

#: The writing runs hot on purpose: two videos with similar evidence must not
#: share a sentence, and determinism is the fallback tables' job.
_TEMPERATURE: Final[float] = 0.9

_TITLE_BOUNDS: Final[tuple[int, int]] = (15, 100)
_HOOK_BOUNDS: Final[tuple[int, int]] = (2, 22)


@dataclass(frozen=True, slots=True)
class CreativeText:
    """What the model wrote for this one video."""

    title: str
    hook_top: str
    hook_bottom: str

    @property
    def hook_lines(self) -> str:
        return f"{self.hook_top}|{self.hook_bottom}"


def write(
    provider: Any,
    *,
    game: str,
    duration: str,
    types: str,
    creatures: str,
    speech: str,
    arabic: bool,
) -> CreativeText | None:
    """One video's own words, or ``None`` and the tables carry on.

    Never raises: a model that is missing, silent, or off the rails is the
    fallback path working, not a failure of the suggestion.
    """
    if provider is None:
        return None
    try:
        if not provider.is_available():
            return None
        prompt = load_prompt("metadata.creative_text")
        rendered = prompt.render(
            game=game or "-",
            duration=duration or "-",
            types=types or "-",
            creatures=creatures or "-",
            speech=speech or "-",
        )
        payload = provider.complete_json(
            rendered, schema=_SCHEMA, prompt_id=prompt.id, temperature=_TEMPERATURE
        )
    except GamingEditorError as error:
        logger.warning("The creative writer did not answer", extra={"error": str(error)[:160]})
        return None
    except Exception:
        logger.exception("The creative writer failed unexpectedly")
        return None
    written = _validated(payload, arabic=arabic)
    if written is None:
        # One more ask. The 7B sometimes answers an all-Arabic prompt in
        # English (the Critic needed a prompt version for the same drift);
        # a second sample at this temperature usually lands. Two misses mean
        # the tables were the right author today.
        try:
            payload = provider.complete_json(
                rendered, schema=_SCHEMA, prompt_id=prompt.id, temperature=_TEMPERATURE
            )
        except GamingEditorError:
            return None
        written = _validated(payload, arabic=arabic)
    return written


def _validated(payload: Any, *, arabic: bool) -> CreativeText | None:
    """Shape checks only. Taste is the model's; bounds are ours."""
    if not isinstance(payload, dict):
        return None
    title = _line(payload.get("title"))
    top = _line(payload.get("hook_top"))
    bottom = _line(payload.get("hook_bottom"))
    if not title or not top or not bottom:
        return None
    if not (_TITLE_BOUNDS[0] <= len(title) <= _TITLE_BOUNDS[1]):
        logger.info("Creative title rejected on length", extra={"length": len(title)})
        return None
    for line in (top, bottom):
        if not (_HOOK_BOUNDS[0] <= len(line) <= _HOOK_BOUNDS[1]):
            logger.info("Creative hook rejected on length", extra={"length": len(line)})
            return None
    if arabic and not any("؀" <= ch <= "ۿ" for ch in title):
        logger.info("Creative title rejected: no Arabic on an Arabic channel")
        return None
    for line in (title, top, bottom):
        if not _scripts_allowed(line):
            # Measured on the second live sample: the 7B wrote Cyrillic into
            # an Arabic title. Foreign scripts are not creativity.
            logger.info("Creative text rejected: foreign script", extra={"line": line[:40]})
            return None
    return CreativeText(title=title, hook_top=top, hook_bottom=bottom)


def _scripts_allowed(text: str) -> bool:
    """Arabic, Latin, digits, punctuation and emoji -- nothing else."""
    import unicodedata

    for ch in text:
        if ch.isascii():
            continue
        if "؀" <= ch <= "ۿ" or "ﭐ" <= ch <= "﻿":
            continue
        category = unicodedata.category(ch)
        if category.startswith(("P", "S", "Z", "N", "M")):
            continue
        return False
    return True


def _line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("#", " ").split())


def gather_and_write(
    provider: Any,
    *,
    database: Any,
    project: Any,
    moments: Any,
    segments: Any,
    arabic: bool,
) -> CreativeText | None:
    """Assemble this video's evidence and ask for its words."""
    from collections import Counter

    from backend.core.duration import format_duration
    from backend.database.repositories.media import MediaRepository
    from backend.metadata import generation

    votes: Counter[str] = Counter()
    for moment in moments:
        kind = str(getattr(getattr(moment, "moment_type", ""), "value", "") or "")
        if kind:
            votes[kind] += 1
    types = "، ".join(
        f"{count} {generation._phrase(kind, arabic=arabic)}"
        for kind, count in votes.most_common(3)
    )

    # HUD nouns OCR reads on every second frame. A "creature" that is really
    # the STORAGE label walks straight into the title as a character -- the
    # first live sample wrote "STORAGE saves the day".
    interface_nouns = {
        "STORAGE", "NEW", "IDEAS", "NEW IDEAS", "LOCKED", "SAVE", "LOAD",
        "MENU", "INVENTORY", "EQUIP", "CRAFT", "OPTIONS", "SETTINGS", "MAP",
        "QUEST", "OBJECTIVES", "PAUSED", "LIVE", "REC", "HP", "MP", "XP",
    }
    creatures: Counter[str] = Counter()
    try:
        for item in MediaRepository(database).list_for_project(project.id):
            rows = database.fetch_all(
                "SELECT text FROM ocr_results WHERE media_id = ? LIMIT 400", (item.id,)
            )
            for row in rows:
                text = str(row["text"] or "").strip()
                if (
                    3 <= len(text) <= 24
                    and text.isupper()
                    and text.replace(" ", "").isalpha()
                    and text not in interface_nouns
                ):
                    creatures[text] += 1
    except Exception:
        logger.exception("Could not gather on-screen names; writing without them")
    names = "، ".join(name for name, _ in creatures.most_common(4))

    speech = ""
    for segment in segments:
        text = str(getattr(segment, "text", "") or "").strip()
        if len(text) > 12:
            speech = text[:140]
            break

    return write(
        provider,
        game=generation._game_name(project) or project.name,
        duration=format_duration(float(project.target_duration_seconds)),
        types=types,
        creatures=names,
        speech=speech,
        arabic=arabic,
    )


__all__ = ["CreativeText", "gather_and_write", "write"]
