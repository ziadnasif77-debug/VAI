"""Recognising the game from what is written on the screen (Phase 0.3).

``projects.detected_game`` has been a column since the schema was written and
nothing ever filled it. Every real project carries ``game: auto``, so
``load_profile`` returned the generic profile every time — and the profile
sitting on disk, with its death-screen wording and its HUD indicator, has
never once been loaded. A profile nobody selects is a profile that does not
exist.

The recogniser is deterministic and cheap: no model, no network, one pass over
text OCR has already read. A profile declares ``signature_patterns`` — item
names, system labels, place names, anything another game would not write — and
the profile whose signatures appear wins, provided it wins clearly.

Two rules keep it honest, and both come from §23:

* **Silence beats a guess.** Returning the wrong profile is worse than
  returning none: a wrong profile reads another game's kill feed out of this
  game's inventory screen and calls the results events. Without a clear
  margin, this returns ``None`` and the generic path continues exactly as it
  did before.
* **Recognition is evidence, not identity.** What is stored is
  ``detected_game``, beside the user's own ``game`` field, so a person who
  says which game they are playing is never overruled by a pattern match.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from backend.core.errors import ConfigurationError
from backend.core.logging import LogChannel, get_logger
from backend.gaming.profiles import (
    GENERIC_PROFILE_ID,
    PROFILE_FILENAME,
    GameProfile,
    load_profile,
)

logger = get_logger("gaming.detection", LogChannel.PIPELINE)

#: How many signatures a profile needs before it may be claimed at all. One
#: shared word ("Craft", "Analyze") is vocabulary half the genre uses.
MIN_HITS: int = 3

#: How far ahead of the runner-up the winner must be. Two profiles for games
#: that share wording is exactly when a guess does damage.
MIN_MARGIN: int = 2


@dataclass(frozen=True, slots=True)
class GameGuess:
    """What the recogniser concluded, and what it read to get there."""

    game: str | None
    hits: int = 0
    runner_up: str | None = None
    runner_up_hits: int = 0

    @property
    def recognised(self) -> bool:
        return self.game is not None

    def summary(self) -> dict[str, object]:
        return {
            "detected_game": self.game,
            "hits": self.hits,
            "runner_up": self.runner_up,
            "runner_up_hits": self.runner_up_hits,
        }


def detect_game(
    texts: Iterable[str],
    profiles_dir: Path,
    *,
    min_hits: int = MIN_HITS,
    min_margin: int = MIN_MARGIN,
) -> GameGuess:
    """Recognise the game from on-screen text, or admit that nothing matched.

    Args:
        texts: everything OCR read from this recording.
        profiles_dir: where the profiles live.

    Returns:
        A :class:`GameGuess`. ``game`` is ``None`` whenever no profile earned
        both ``min_hits`` signatures and a ``min_margin`` lead — which is the
        common case, and the correct one for a game nobody has written a
        profile for (§23).
    """
    readings = [text for text in texts if text and text.strip()]
    if not readings:
        return GameGuess(game=None)

    scored = sorted(
        (
            (profile.signature_hits(readings), profile.id)
            for profile in _candidates(profiles_dir)
        ),
        reverse=True,
    )
    if not scored:
        return GameGuess(game=None)

    hits, game = scored[0]
    runner_hits, runner = scored[1] if len(scored) > 1 else (0, None)
    guess = GameGuess(
        game=game if hits >= min_hits and hits - runner_hits >= min_margin else None,
        hits=hits,
        runner_up=runner,
        runner_up_hits=runner_hits,
    )
    logger.info(
        "Looked for the game in what the screen said",
        extra={"readings": len(readings), "considered": len(scored), **guess.summary()},
    )
    return guess


def _candidates(profiles_dir: Path) -> Sequence[GameProfile]:
    """Every profile on disk that declares signatures to match against.

    A profile that will not parse is skipped with a warning rather than
    raising. Loudness about a broken profile belongs to the profile somebody
    *asked for* -- ``load_profile`` still raises there, and should. This is an
    optional scan across every file on disk, and letting a third profile
    nobody mentioned stop the analysis stage would be the tail wagging the dog.
    """
    base = Path(profiles_dir)
    if not base.is_dir():
        return ()
    found: list[GameProfile] = []
    for entry in sorted(base.iterdir()):
        if entry.name == GENERIC_PROFILE_ID or not (entry / PROFILE_FILENAME).is_file():
            continue
        try:
            profile = load_profile(entry.name, base).profile
        except ConfigurationError as error:
            logger.warning(
                "Skipped an unreadable profile while looking for the game",
                extra={"profile": entry.name, "error": str(error)[:160]},
            )
            continue
        if profile.signature_patterns:
            found.append(profile)
    return found


__all__ = ["MIN_HITS", "MIN_MARGIN", "GameGuess", "detect_game"]
