"""Game profiles (SPEC sections 22, 23, 24, 25, 111).

§22 lets a profile declare where a game puts its HUD, what its kill feed looks
like, and which text means victory. §23 is the constraint that shapes the whole
module:

    **The application must not require a game profile.**

So a profile is *additive*. Every field is optional, the generic profile
declares nothing at all, and asking for a game nobody has written a profile for
returns the generic one rather than failing. What a profile buys is accuracy
and cost: region-restricted OCR against three declared boxes is both cheaper
and far more reliable than scanning a whole frame of stylised game UI (§25).

Regions are stored as **fractions of the frame**, not pixels. A profile written
against 1080p gameplay has to keep working on the 720p proxy the analysis
actually reads, and on an ultrawide capture — and a pixel rectangle does none
of that.

§111 governs how many of these exist: one real game validated before more are
written. A directory of ten speculative profiles is ten things to maintain and
zero evidence the design works.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.core.errors import ConfigurationError, ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import GameEventType

logger = get_logger("gaming.profiles", LogChannel.PIPELINE)

#: The profile used when the game is unknown or unsupported (§23).
GENERIC_PROFILE_ID: Final[str] = "generic"

#: Values that mean "no specific game".
UNSPECIFIED_GAMES: Final[frozenset[str]] = frozenset({"", "auto", "unknown", "generic"})

#: Kept for readers inside this module; the public name is the exported one.
_UNSPECIFIED: Final[frozenset[str]] = UNSPECIFIED_GAMES

PROFILE_FILENAME: Final[str] = "profile.json"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Region(_Model):
    """A rectangle on the frame, in fractions of width and height.

    Fractions rather than pixels so one profile serves the source, the 720p
    proxy and any capture resolution. ``(0, 0)`` is the top-left corner.
    """

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _within_frame(self) -> Region:
        if self.x + self.width > 1.0 + 1e-6 or self.y + self.height > 1.0 + 1e-6:
            raise ValueError(
                "A region must fit inside the frame: x + width and y + height "
                "cannot exceed 1.0."
            )
        return self

    def to_pixels(self, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
        """Return ``(left, top, right, bottom)`` for a frame of this size."""
        left = round(self.x * frame_width)
        top = round(self.y * frame_height)
        right = min(round((self.x + self.width) * frame_width), frame_width)
        bottom = min(round((self.y + self.height) * frame_height), frame_height)
        return left, top, max(right, left + 1), max(bottom, top + 1)


class EventRule(_Model):
    """Text that identifies a game event, when a profile knows the wording.

    A rule is evidence, not a verdict: it raises a detector's confidence that
    something specific happened, and §27 still decides by agreement across
    sources. ``confidence`` is what this rule alone is worth.
    """

    event_type: GameEventType
    #: Case-insensitive regular expressions matched against OCR text.
    patterns: tuple[str, ...] = ()
    #: Restrict matching to text read from these named regions. Empty matches
    #: text from anywhere, which is what an unknown layout has to do.
    regions: tuple[str, ...] = ()
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    #: Seconds of gameplay this event covers, before §29's context expansion.
    duration_seconds: float = Field(default=2.0, gt=0.0)

    @field_validator("patterns")
    @classmethod
    def _compilable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid pattern {pattern!r}: {exc}") from exc
        return value

    def matches(self, text: str, *, region: str | None = None) -> bool:
        """Whether ``text`` (optionally from ``region``) satisfies this rule."""
        if self.regions and (region is None or region not in self.regions):
            return False
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in self.patterns)


class ContentRule(_Model):
    """Text that identifies what the screen is showing when it is not the game.

    The JSON form of :class:`backend.gaming.content.ContentRule`. Deliberately
    separate from :class:`EventRule`: that one names things that *happen in
    the game* and everything downstream treats its output as material worth
    selecting. A menu is not material, and giving it the same vocabulary is
    how seventeen seconds of them reached a finished video (V2-P0.1).

    ``lead_seconds`` and ``hold_seconds`` are the point of this rule existing
    at all. OCR samples roughly a frame every seven seconds, so a menu on
    screen for twenty is read at one instant; a rule that produced a point
    would be describing the sample rather than the screen.
    """

    state: str = Field(min_length=1)
    name: str = Field(min_length=1)
    patterns: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    lead_seconds: float = Field(default=4.0, ge=0.0)
    hold_seconds: float = Field(default=8.0, ge=0.0)
    vision_may_raise: bool = True

    @field_validator("patterns")
    @classmethod
    def _compilable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid pattern {pattern!r}: {exc}") from exc
        return value

    @field_validator("state")
    @classmethod
    def _known_state(cls, value: str) -> str:
        from backend.gaming.content import ContentState

        try:
            ContentState(value)
        except ValueError as exc:
            allowed = ", ".join(sorted(item.value for item in ContentState))
            raise ValueError(
                f"Unknown content state {value!r}. Allowed: {allowed}."
            ) from exc
        return value

    @model_validator(mode="after")
    def _asks_for_something(self) -> ContentRule:
        if not self.patterns:
            raise ValueError(
                f"Content rule {self.name!r} declares no patterns, so it would "
                "match every frame in the recording."
            )
        return self


class HudKind(str, Enum):
    """The shapes of HUD indicator this reader knows (§24)."""

    #: A row of identical glyphs, some filled: wanted stars, lives, pips.
    GLYPH_ROW = "glyph_row"
    #: A horizontal fill bar: health, armour, boss health, a charge meter.
    BAR = "bar"


class HudChangeRule(_Model):
    """What a transition in an indicator means (§24, §26).

    Separate from :class:`EventRule` because that reads *text* and this reads a
    *number that moved*. "Rose to at least 3" and "fell to 0" are different
    events in every game that has a threat level, and neither is a string.
    """

    event_type: GameEventType
    #: Only fire when the value moved in this direction.
    direction: str = Field(default="any", pattern="^(rise|fall|any)$")
    #: Only fire when the new value is at least / at most this.
    at_least: float | None = None
    at_most: float | None = None
    #: Only fire when the value moved by at least this much, which is what
    #: separates "the police noticed" from "the police are now a helicopter".
    min_change: float = Field(default=0.0, ge=0.0)
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    duration_seconds: float = Field(default=4.0, gt=0.0)

    def matches(self, change: Any) -> bool:
        """Whether ``change`` (a :class:`backend.gaming.hud.HudChange`) fires this."""
        if self.direction == "rise" and not change.rising:
            return False
        if self.direction == "fall" and change.rising:
            return False
        if change.magnitude < self.min_change:
            return False
        current = change.current if change.current is not None else 0.0
        if self.at_least is not None and current < self.at_least:
            return False
        return not (self.at_most is not None and current > self.at_most)


class HudIndicator(_Model):
    """One thing a game draws that has a value (§24).

    ``region`` is a *search window*, not the exact rectangle: the reader locates
    the indicator inside it. GTA V's wanted row is right-anchored and grows
    leftwards, so a fixed rectangle misreads it by a whole star as the level
    changes -- silently, and in the direction that matters most.
    """

    name: str = Field(min_length=1)
    kind: HudKind
    region: Region
    #: For ``glyph_row``: how many glyph positions the row has.
    count: int = Field(default=1, ge=1, le=32)
    #: What one clean reading of this indicator is worth. Deliberately below
    #: 1.0: a pixel heuristic that claims certainty is lying, and §27 exists to
    #: combine it with detectors that saw the same instant.
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)

    #: What it means when the indicator is not drawn at all. GTA V hides the
    #: star row at wanted level zero, so absence *is* the reading -- but in a
    #: game that hides its health bar in cutscenes, absence means nothing and
    #: this stays ``None``.
    absent_value: float | None = None
    absent_confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    change_rules: tuple[HudChangeRule, ...] = ()

    def rule_for(self, change: Any) -> HudChangeRule | None:
        """The first rule this change satisfies, if any."""
        for rule in self.change_rules:
            if rule.matches(change):
                return rule
        return None


class ProfileFusionRule(_Model):
    """A profile's own rule for naming an instant from combined evidence.

    The JSON form of :class:`backend.gaming.fusion.FusionRule`. Profiles are
    configuration and validate through pydantic; the fusion module stays a
    plain domain dataclass that knows nothing about files.
    """

    event_type: GameEventType
    name: str = Field(min_length=1)
    labels: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    types: tuple[GameEventType, ...] = ()
    min_label_confidence: float = Field(default=0.45, ge=0.0, le=1.0)
    min_label_count: int = Field(default=1, ge=1)
    description_pattern: str = ""
    confidence: float = Field(default=0.65, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _asks_for_something(self) -> ProfileFusionRule:
        if not (self.labels or self.sources or self.types or self.description_pattern):
            raise ValueError(
                f"Fusion rule {self.name!r} requires no evidence at all, so it "
                "would name every unnamed instant in the recording."
            )
        if self.description_pattern:
            try:
                re.compile(self.description_pattern)
            except re.error as error:
                raise ValueError(
                    f"Fusion rule {self.name!r} has a description pattern that is "
                    f"not a regular expression: {error}."
                ) from error
        return self


class GameProfile(_Model):
    """What is known about one game's interface (§22).

    Every field is optional. An empty profile is valid and is exactly what
    :data:`GENERIC_PROFILE_ID` is.
    """

    id: str = Field(min_length=1)
    name: str = ""
    description: str = ""

    #: On-screen text that identifies this game, for the ``auto`` path. Item
    #: names, system labels, place names -- anything another game would not
    #: write. Matched against OCR, and a match is only ever a vote (§23): the
    #: detector needs a clear margin over every other profile before it claims
    #: to have recognised anything.
    signature_patterns: tuple[str, ...] = ()

    #: Rules that name an instant from evidence no single detector could name,
    #: consulted ahead of the generic table (Phase 0.2, 0.3).
    fusion_rules: tuple[ProfileFusionRule, ...] = ()

    #: This game's own wording for menus, loading screens and failure
    #: screens (V2-P0.1). Appended to the generic table rather than replacing
    #: it, because most games say "LOADING" the same way.
    content_rules: tuple[ContentRule, ...] = ()

    #: Names of generic *content* rules this game contradicts, by the same
    #: escape hatch and for the same reason as the fusion one below.
    suppressed_content_rules: tuple[str, ...] = ()

    #: Names of generic fusion rules this game contradicts. The generic table
    #: is written for the common case, and Grounded is the measured
    #: counter-case: its vision labels call a player *holding a bow*
    #: "combat" and buggy-riding "driving", so `combat_seen_and_heard` and
    #: `driving_impact` named eight fights and four crashes on a stretch a
    #: person marked boring. A profile that knows better says so by name.
    suppressed_generic_rules: tuple[str, ...] = ()

    #: Named HUD regions: ``kill_feed``, ``health``, ``score``, ``timer``...
    #: The names are free-form so a profile can declare what its game has.
    regions: dict[str, Region] = Field(default_factory=dict)

    #: Regions OCR should read. A subset of ``regions``; empty means "read what
    #: the fallback finds", which is the unknown-game path (§23, §25).
    ocr_regions: tuple[str, ...] = ()

    #: Text that identifies events in this game.
    event_rules: tuple[EventRule, ...] = ()

    #: Interface text that is never an event: menu labels, watermarks, the
    #: player's own name. Filtering these out is most of what makes OCR-driven
    #: detection usable.
    ignore_patterns: tuple[str, ...] = ()

    #: Indicators read as state rather than text (§24).
    hud: tuple[HudIndicator, ...] = ()

    @model_validator(mode="after")
    def _hud_names_unique(self) -> GameProfile:
        names = [indicator.name for indicator in self.hud]
        duplicated = {name for name in names if names.count(name) > 1}
        if duplicated:
            raise ValueError(
                "Two HUD indicators share a name, so their readings would be "
                f"tracked as one: {sorted(duplicated)}."
            )
        return self

    @model_validator(mode="after")
    def _regions_exist(self) -> GameProfile:
        unknown = [name for name in self.ocr_regions if name not in self.regions]
        if unknown:
            raise ValueError(
                f"ocr_regions names regions this profile does not define: {unknown}. "
                "OCR would read nothing from them."
            )
        for rule in self.event_rules:
            missing = [name for name in rule.regions if name not in self.regions]
            if missing:
                raise ValueError(
                    f"Event rule {rule.event_type.value!r} restricts to regions "
                    f"this profile does not define: {missing}."
                )
        return self

    @property
    def is_generic(self) -> bool:
        """Whether this profile declares nothing game-specific."""
        return not self.regions and not self.event_rules and not self.hud

    def signature_hits(self, texts: Iterable[str]) -> int:
        """How many of this profile's signatures appear in ``texts``.

        Counted once per pattern rather than once per reading: a quest tracker
        holding one recognisable word on screen for four minutes is one piece
        of evidence about which game this is, not two hundred.
        """
        if not self.signature_patterns:
            return 0
        joined = "\n".join(texts)
        return sum(
            1
            for pattern in self.signature_patterns
            if re.search(pattern, joined, re.IGNORECASE)
        )

    def fusion(self) -> tuple[Any, ...]:
        """This profile's fusion rules, in the form the fusion module uses."""
        from backend.gaming.fusion import FusionRule

        return tuple(
            FusionRule(
                event_type=rule.event_type,
                name=f"{self.id}:{rule.name}",
                labels=rule.labels,
                sources=rule.sources,
                types=rule.types,
                min_label_confidence=rule.min_label_confidence,
                min_label_count=rule.min_label_count,
                description_pattern=rule.description_pattern,
                confidence=rule.confidence,
            )
            for rule in self.fusion_rules
        )

    def rules_with(self, generic) -> tuple[Any, ...]:
        """This profile's rules ahead of the generic table, minus what it vetoes."""
        suppressed = set(self.suppressed_generic_rules)
        return (
            *self.fusion(),
            *(rule for rule in generic if rule.name not in suppressed),
        )

    @property
    def has_ocr_regions(self) -> bool:
        return bool(self.ocr_regions)

    def region(self, name: str) -> Region | None:
        return self.regions.get(name)

    def reading_regions(self) -> dict[str, Region]:
        """The regions OCR should read, resolved (§25)."""
        return {name: self.regions[name] for name in self.ocr_regions}

    def should_ignore(self, text: str) -> bool:
        """Whether this text is interface furniture rather than an event."""
        stripped = text.strip()
        if not stripped:
            return True
        return any(
            re.search(pattern, stripped, re.IGNORECASE) for pattern in self.ignore_patterns
        )

    def rules_for(self, text: str, *, region: str | None = None) -> tuple[EventRule, ...]:
        """Every rule this text satisfies."""
        if self.should_ignore(text):
            return ()
        return tuple(rule for rule in self.event_rules if rule.matches(text, region=region))

    def summary(self) -> dict[str, Any]:
        return {
            "profile": self.id,
            "generic": self.is_generic,
            "regions": len(self.regions),
            "ocr_regions": len(self.ocr_regions),
            "event_rules": len(self.event_rules),
            "hud_indicators": len(self.hud),
        }


#: The profile every unknown game gets. Declares nothing, which is the point:
#: detection falls back to vision, OCR, audio, speech and temporal analysis
#: over the whole frame (§23).
GENERIC_PROFILE: Final[GameProfile] = GameProfile(
    id=GENERIC_PROFILE_ID,
    name="Generic",
    description=(
        "Used when the game is unknown or unsupported. Declares no regions and "
        "no rules, so nothing about a specific game's interface is assumed."
    ),
)


@dataclass(frozen=True, slots=True)
class ProfileResolution:
    """Which profile was used, and whether it is the one that was asked for.

    Recorded on results because "detected with the generic profile" and
    "detected with the Valorant profile" are different claims about the same
    event, and §49 wants that distinction preserved.
    """

    profile: GameProfile
    requested: str
    #: False when the requested game has no profile and generic was substituted.
    exact: bool

    @property
    def id(self) -> str:
        return self.profile.id


def load_profile(game: str, profiles_dir: Path) -> ProfileResolution:
    """Resolve ``game`` to a profile, falling back to generic (§23).

    Never raises for an unknown game — that is the entire point of §23. It does
    raise for a profile that exists but is malformed, because a broken profile
    that silently becomes generic would look like the feature working.
    """
    requested = (game or "").strip()
    if requested.lower() in _UNSPECIFIED:
        return ProfileResolution(profile=GENERIC_PROFILE, requested=requested, exact=True)

    base = Path(profiles_dir)
    if not base.is_dir():
        # Two absences that read the same from the outside and are not the
        # same thing (V2-P0.3). "This game has no profile" is the ordinary
        # case §23 promises never to fail on. "The profiles directory is not
        # there" means the install is broken -- every game would silently
        # become generic and the feature would look like it was working.
        # That was swallowed by a catch-all until a log line gave it away.
        raise ConfigurationError(
            f"The game profiles directory is missing: {base}",
            code=ErrorCode.CONFIG_INVALID,
            details={"game": requested, "profiles_dir": str(base)},
            recoverable=False,
        )

    path = base / requested / PROFILE_FILENAME
    if not path.is_file():
        logger.info(
            "No profile for this game; using the generic one",
            extra={"game": requested, "expected": str(path)},
        )
        return ProfileResolution(profile=GENERIC_PROFILE, requested=requested, exact=False)

    return ProfileResolution(
        profile=_read_profile(path, requested), requested=requested, exact=True
    )


@lru_cache(maxsize=32)
def _read_profile(path: Path, game: str) -> GameProfile:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"Game profile {game!r} could not be read: {exc}",
            code=ErrorCode.GAME_PROFILE_INVALID,
            details={"game": game, "path": str(path)},
            cause=exc,
            recoverable=False,
        ) from exc

    payload.setdefault("id", game)
    try:
        return GameProfile.model_validate(payload)
    except Exception as exc:
        raise ConfigurationError(
            f"Game profile {game!r} is invalid: {exc}",
            code=ErrorCode.GAME_PROFILE_INVALID,
            details={"game": game, "path": str(path)},
            cause=exc,
            recoverable=False,
        ) from exc


def available_profiles(profiles_dir: Path) -> tuple[str, ...]:
    """Game ids that have a profile on disk, plus the generic one."""
    base = Path(profiles_dir)
    found = {GENERIC_PROFILE_ID}
    if base.is_dir():
        found |= {
            entry.name for entry in base.iterdir() if (entry / PROFILE_FILENAME).is_file()
        }
    return tuple(sorted(found))


def clear_profile_cache() -> None:
    """Drop the cache. Used by tests that write profiles to a temporary root."""
    _read_profile.cache_clear()


__all__ = [
    "GENERIC_PROFILE",
    "GENERIC_PROFILE_ID",
    "PROFILE_FILENAME",
    "UNSPECIFIED_GAMES",
    "ContentRule",
    "EventRule",
    "GameProfile",
    "HudChangeRule",
    "HudIndicator",
    "HudKind",
    "ProfileFusionRule",
    "ProfileResolution",
    "Region",
    "available_profiles",
    "clear_profile_cache",
    "load_profile",
]
