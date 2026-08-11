"""Game profile endpoints (SPEC §22, §23, §111).

§111's instruction is to build the Game Profile API *after* one real game
works, and not to write ten speculative profiles before the architecture is
validated. What that architecture has to be worth is this: **a second game is a
data change, not a code change.** These endpoints are what makes that true from
outside the process.

The validate endpoint is the one that earns its place. A profile is JSON that
declares regions, enum values and regular expressions, and every one of those
can be wrong in a way that produces silence rather than an error — a region
that reads empty sky, a pattern that never matches, an event type that does not
exist. Writing GTA V's profile hit that: two event types were plausible names
for values the enum does not have, and the difference between finding out here
and finding out after a two-hour analysis is the whole point.

Nothing here writes a profile. Profiles ship with the code and are edited on
disk; an endpoint that installed one would be an upload path into a directory
the pipeline executes rules from.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.dependencies import AppState, get_state
from backend.core.errors import GamingEditorError
from backend.gaming.profiles import (
    GENERIC_PROFILE_ID,
    GameProfile,
    available_profiles,
    load_profile,
)

router = APIRouter(prefix="/profiles", tags=["profiles"])


class ProfileSummary(BaseModel):
    """One profile, as the import screen needs to list it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str = ""
    generic: bool
    regions: int
    ocr_regions: int
    event_rules: int
    hud_indicators: int


class ProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The profile used when the game is unknown (§23). Always present.
    generic: str = GENERIC_PROFILE_ID
    items: list[ProfileSummary]


class ProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What the caller asked for, which is not always what they got (§23).
    requested: str
    #: False when the requested game has no profile and generic was returned.
    exact: bool
    profile: GameProfile


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: dict[str, Any] = Field(description="A candidate profile document.")


class ValidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    #: What is wrong, in the words the model used. Empty when valid.
    errors: list[str] = Field(default_factory=list)
    #: What the profile would do, when it is valid: how many regions OCR would
    #: read, how many rules could fire. A profile that validates and declares
    #: nothing is a common and silent mistake.
    summary: dict[str, Any] | None = None


def _summary(profile: GameProfile) -> ProfileSummary:
    counts = profile.summary()
    return ProfileSummary(
        id=profile.id,
        name=profile.name or profile.id,
        description=profile.description,
        generic=bool(counts["generic"]),
        regions=int(counts["regions"]),
        ocr_regions=int(counts["ocr_regions"]),
        event_rules=int(counts["event_rules"]),
        hud_indicators=int(counts["hud_indicators"]),
    )


@router.get("", response_model=ProfileListResponse)
def list_profiles(state: AppState = Depends(get_state)) -> ProfileListResponse:
    """Every profile on disk, plus the generic one (§22)."""
    items: list[ProfileSummary] = []
    for game in available_profiles(state.paths.profiles_dir):
        try:
            items.append(_summary(load_profile(game, state.paths.profiles_dir).profile))
        except GamingEditorError as error:
            # One broken profile must not hide the rest, and it must not be
            # silently omitted either -- a game missing from this list is the
            # symptom someone would chase for an hour.
            items.append(
                ProfileSummary(
                    id=game,
                    name=game,
                    description=f"This profile could not be read: {error}",
                    generic=False,
                    regions=0,
                    ocr_regions=0,
                    event_rules=0,
                    hud_indicators=0,
                )
            )
    return ProfileListResponse(items=items)


@router.get("/{game}", response_model=ProfileResponse)
def get_profile(game: str, state: AppState = Depends(get_state)) -> ProfileResponse:
    """One profile in full.

    An unknown game returns the generic profile with ``exact: false`` rather
    than 404 — §23 says the application must work without a profile, and a
    caller asking about a game nobody has written one for is that case, not an
    error.
    """
    try:
        resolution = load_profile(game, state.paths.profiles_dir)
    except GamingEditorError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(error), "code": error.code.value, "game": game},
        ) from error
    return ProfileResponse(
        requested=resolution.requested or GENERIC_PROFILE_ID,
        exact=resolution.exact,
        profile=resolution.profile,
    )


@router.post("/validate", response_model=ValidateResponse)
def validate_profile(request: ValidateRequest) -> ValidateResponse:
    """Check a candidate profile without installing it.

    Returns 200 with ``valid: false`` rather than 422: the caller asked whether
    this document is valid, and "no, here is why" is a successful answer to
    that question.
    """
    try:
        profile = GameProfile.model_validate(request.profile)
    except Exception as error:
        return ValidateResponse(valid=False, errors=_readable(error))
    return ValidateResponse(valid=True, summary=profile.summary())


def _readable(error: Exception) -> list[str]:
    """Pydantic's errors as lines a person can act on."""
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return [str(error)]
    lines: list[str] = []
    for item in errors():
        where = ".".join(str(part) for part in item.get("loc", ())) or "profile"
        lines.append(f"{where}: {item.get('msg', 'invalid')}")
    return lines or [str(error)]


__all__ = ["router"]
