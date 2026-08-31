"""The Style Bible: taste, resolved once and recorded (V2-P8).

A style has selected an effects profile since V1's Phase 8 -- the presets set
``intent.style``, the library scales its decoration by it. What no style could
do was change a cut length, an audio decision, what the judge values, or what
counts as a defect. Those lived as module constants, identical for every video
this machine will ever make, which meant the channel's identity was one dial on
the effects library and nothing else.

This module resolves the body behind that name, and records which body made a
given edit. The recording is the part that matters later: P9 asks which videos
were cut which way, and an answer that depends on remembering what the file
said last month is not an answer.

Nothing here learns. ``resolve`` reads a document a person wrote, checks it
against bounds the same document declares, and returns it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.core.logging import LogChannel, get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.config.schema import (
        AppConfig,
        StyleAudioConfig,
        StyleCritiqueConfig,
        StyleEntry,
        StyleJudgementConfig,
        StylePacingConfig,
        StyleShotsConfig,
    )

logger = get_logger("style.bible", LogChannel.PIPELINE)

#: What ``intent.style`` says when nobody chose: the string it is born with.
#: Treated as "no preference" rather than as a style named "default", so a
#: project that never asked gets the bible's default body.
UNSET: frozenset[str] = frozenset({"", "default"})


@dataclass(frozen=True, slots=True)
class Style:
    """One resolved style: the name asked for, and the body that answered."""

    #: What the project asked for, verbatim -- including a name with no body.
    asked: str
    #: The entry actually used. Differs from :attr:`asked` when the name has
    #: no body, and the difference is recorded rather than smoothed over.
    name: str
    version: int
    #: What this style asks of the shots themselves (V2-P0): how much run-up
    #: each keeps, whether cuts snap to seams the footage already has, and
    #: whether a stretch that earns nothing is priced.
    shots: StyleShotsConfig
    pacing: StylePacingConfig
    audio: StyleAudioConfig
    judgement: StyleJudgementConfig
    critique: StyleCritiqueConfig
    #: Content hash of the resolved values. Two edits with the same digest were
    #: cut by the same taste, whatever the file was called at the time.
    digest: str
    #: Keys P10's controlled tuning moved away from what the file says. Empty
    #: is the normal state and the only state so far.
    tuned: tuple[str, ...] = ()

    def shelf_for(self, level: str, fallback: str) -> str:
        """The music bed this style puts under a level."""
        return self.audio.shelves.get(level, fallback)

    def as_dict(self) -> dict[str, Any]:
        return {
            "asked": self.asked,
            "name": self.name,
            "version": self.version,
            "digest": self.digest,
            "tuned": list(self.tuned),
            "shots": self.shots.model_dump(mode="json"),
            "pacing": self.pacing.model_dump(mode="json"),
            "audio": self.audio.model_dump(mode="json"),
            "judgement": self.judgement.model_dump(mode="json"),
            "critique": self.critique.model_dump(mode="json"),
        }


def resolve(
    config: AppConfig, asked: str | None = None, *, database: Any = None
) -> Style:
    """The style a name resolves to, and never an exception for a typo.

    A name with no entry falls back to the bible's default and says so in the
    log and in the returned :attr:`Style.name`. Refusing to build the video
    over a misspelled style would be a worse failure than cutting it in the
    house style and recording that this is what happened.

    ``database`` brings P10's controlled tuning into the answer: any adjustment
    in force is added to the file's value and clamped to the same bounds the
    file declares. Without it the file is the whole truth, which is what every
    caller got before that phase and what every caller still gets today -- the
    ledger is empty and the switch is off.
    """
    bible = config.style.bible
    wanted = (asked or "").strip()
    name = config.style.default if wanted.lower() in UNSET else wanted
    entry = bible.get(name)
    if entry is None:
        if wanted:
            logger.warning(
                "No body for this style; the default was used and recorded",
                extra={"asked": wanted, "used": config.style.default},
            )
        name = config.style.default
        entry = bible.get(name)
    if entry is None:
        raise KeyError(
            f"The style bible has no entry for {name!r} and no default; "
            f"config/style.yaml lists {sorted(bible)}."
        )
    entry, applied = _tuned(config, name, entry, database)
    return _style(asked=wanted or name, name=name, entry=entry, tuned=applied)


def _style(
    *, asked: str, name: str, entry: StyleEntry, tuned: tuple[str, ...] = ()
) -> Style:
    return Style(
        asked=asked,
        name=name,
        version=entry.version,
        shots=entry.shots,
        pacing=entry.pacing,
        audio=entry.audio,
        judgement=entry.judgement,
        critique=entry.critique,
        digest=digest_of(name, entry),
        tuned=tuned,
    )


def _tuned(
    config: AppConfig, name: str, entry: StyleEntry, database: Any
) -> tuple[StyleEntry, tuple[str, ...]]:
    """The entry with P10's adjustments folded in, and which keys moved.

    Folded into the entry rather than applied at each use, so the digest
    already reflects them: two videos cut with different tuning must not carry
    the same fingerprint, or the record P9 keeps would say they were the same
    edit made twice.

    A failure to read the ledger returns the file untouched. Tuning is an
    improvement to a system that works without it, and a database that will not
    answer is not a reason to stop making videos.
    """
    if database is None:
        return entry, ()
    try:
        from backend.tuning.deltas import TuningLedger

        offsets = TuningLedger(database, config).offsets(name)
    except Exception:
        logger.exception("The tuning ledger could not be read; the file stands")
        return entry, ()
    if not offsets:
        return entry, ()

    sections: dict[str, dict[str, float]] = {}
    moved: list[str] = []
    for key, delta in offsets.items():
        section, _, field_name = key.partition(".")
        current = getattr(entry, section, None)
        if current is None or not hasattr(current, field_name):
            logger.warning(
                "A tuning delta names a value this style does not have",
                extra={"style": name, "key": key},
            )
            continue
        limit = config.style.limits.get(key)
        value = float(getattr(current, field_name)) + float(delta)
        if limit is not None:
            # The same fence the ledger checked at write time, checked again at
            # read time: the file may have been edited since, and a delta that
            # was legal against the old base need not be legal against the new.
            value = min(float(limit.max), max(float(limit.min), value))
        sections.setdefault(section, {})[field_name] = value
        moved.append(key)

    if not sections:
        return entry, ()
    update = {
        section: getattr(entry, section).model_copy(update=fields)
        for section, fields in sections.items()
    }
    return entry.model_copy(update=update), tuple(sorted(moved))


def digest_of(name: str, entry: StyleEntry) -> str:
    """A stable hash of what this style actually decides.

    Over the values, not the file: a reordered YAML key or a reworded
    description must not read as a change of taste, and a changed number must.
    """
    payload = {
        "name": name,
        "version": entry.version,
        "pacing": entry.pacing.model_dump(mode="json"),
        "audio": entry.audio.model_dump(mode="json"),
        "judgement": entry.judgement.model_dump(mode="json"),
        "critique": entry.critique.model_dump(mode="json"),
    }
    return hashlib.blake2b(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        digest_size=12,
    ).hexdigest()


# -- what made this edit ----------------------------------------------------


def stamp(database: Any, project_id: str, style: Style) -> None:
    """Record the style that produced the current edit.

    One row per project, rewritten whenever the edit is rebuilt: the question
    this answers is "what made the video that exists", not "what has this
    project ever been cut with".
    """
    database.execute(
        "INSERT INTO edit_styles "
        "(project_id, asked, style, version, digest, resolved, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id) DO UPDATE SET "
        "asked = excluded.asked, style = excluded.style, "
        "version = excluded.version, digest = excluded.digest, "
        "resolved = excluded.resolved, created_at = excluded.created_at",
        (
            project_id,
            style.asked,
            style.name,
            style.version,
            style.digest,
            json.dumps(style.as_dict(), sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    logger.info(
        "The edit was stamped with the style that made it",
        extra={"style": style.name, "version": style.version, "digest": style.digest},
    )


def for_project(database: Any, config: AppConfig, project_id: str) -> Style:
    """The style that made this project's edit, or the one that would.

    Stages after the edit -- the renderer, QA, the post-render critic -- must
    judge the video by the taste that produced it, not by whatever the intent
    says now. A person who changes the preset after a render has not changed
    the video sitting on disk.
    """
    row = None
    try:
        row = database.fetch_one(
            "SELECT style, asked FROM edit_styles WHERE project_id = ?", (project_id,)
        )
    except Exception:
        logger.exception("The style stamp could not be read; the intent decides")
    if row is not None and row["style"] in config.style.bible:
        return _style(
            asked=row["asked"] or row["style"],
            name=row["style"],
            entry=config.style.bible[row["style"]],
        )
    return resolve(
        config, _asked_by(database, config, project_id), database=database
    )


def _asked_by(database: Any, config: AppConfig, project_id: str) -> str | None:
    """What this project's editing brief calls for, if it can be read."""
    try:
        from backend.interaction.service import InteractionService

        intent = InteractionService(database, config).current_intent(project_id)
    except Exception:
        logger.info("No editing brief for this project; the default style applies")
        return None
    return getattr(intent, "style", None)


__all__ = ["UNSET", "Style", "digest_of", "for_project", "resolve", "stamp"]
