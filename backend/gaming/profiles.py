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
from dataclasses import dataclass
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
_UNSPECIFIED: Final[frozenset[str]] = frozenset({"", "auto", "unknown", "generic"})

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


class GameProfile(_Model):
    """What is known about one game's interface (§22).

    Every field is optional. An empty profile is valid and is exactly what
    :data:`GENERIC_PROFILE_ID` is.
    """

    id: str = Field(min_length=1)
    name: str = ""
    description: str = ""

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
        return not self.regions and not self.event_rules

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

    path = Path(profiles_dir) / requested / PROFILE_FILENAME
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
    "EventRule",
    "GameProfile",
    "ProfileResolution",
    "Region",
    "available_profiles",
    "clear_profile_cache",
    "load_profile",
]
