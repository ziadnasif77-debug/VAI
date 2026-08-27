"""Mine a project's stored OCR and vision for game-profile signature candidates.

    python scripts/profile_report.py proj-445ae3666902

The measured path to a new game profile (docs/PROFILES.md): record real
footage, run the analysis, then run this against the project. It reads what
the pipeline already stored -- nothing is re-analysed and nothing is written --
and prints the game's own vocabulary ranked by how often the screen showed it,
plus a ready-to-paste ``signature_patterns`` snippet.

Mining beats invention because detection sees OCR readings, not the game's
manual. ``profiles/gta_v/profile.json`` shipped with seven measured HUD regions
and zero signature patterns, so game detection -- which matches on signatures
(SPEC section 23) -- could never identify GTA, and a real 96-minute recording
fell back to the generic profile with 62 driving events left unnamed. And the
readings themselves are imperfect: ``DIRECTOR MODE`` came back as
``DIFIECTOR``, ``DINECTOH`` and ``DIPECTOR`` across 161 readings of one
recording, so a pattern written from the correct spelling alone would have
matched a minority of its own evidence.

Candidates are only candidates. A signature must be text no *other* game
writes -- place names, item names, mode banners -- and ``detect_game`` claims
nothing below 3 pattern hits and a lead of 2 over the runner-up
(``backend/gaming/detection.py``). The printed list belongs in
``profiles/<id>/profile.json`` only after the checks in docs/PROFILES.md.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.loader import load_config
from backend.config.paths import build_paths
from backend.database.connection import Database
from backend.database.repositories.gaming import OcrRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.projects import ProjectRepository
from backend.database.repositories.vision import VisionRepository

#: Readings shorter than this are fragments ("OK", a stray glyph pair) and
#: longer ones are sentences -- tutorial text and subtitles, which change every
#: time and can never recur often enough to matter.
MIN_LENGTH: Final[int] = 3
MAX_LENGTH: Final[int] = 40

#: How often a string must recur before it is proposed. A signature is matched
#: against a whole recording at once, so one appearance would technically do --
#: but a string OCR produced once is as likely a misread as a fact, and five
#: appearances across 40+ minutes is the floor at which it is reliably the
#: game's own vocabulary.
MIN_COUNT: Final[int] = 5

#: How many candidates the ready-to-paste snippet offers. The larger shipped
#: profile (grounded) carries 12 signatures and separates cleanly at 12 hits to
#: the runner-up's 0; more patterns is maintenance, not accuracy (SPEC
#: section 111).
SIGNATURE_LIMIT: Final[int] = 12

#: Whole readings that cannot identify a game because most games write them.
#: Two traceable sources: the single-word menu chrome the two shipped profiles
#: already ignore (Close/Analyze/Sort/... in grounded, SCENE/CATEGORY/TYPE/...
#: in gta_v), and pan-genre interface staples -- ``detect_game``'s own comment
#: is the rule: one shared word ("Craft", "Analyze") is vocabulary half the
#: genre uses. ``victory``, ``defeat`` and ``game over`` are here for a subtler
#: reason: they identify an *event*, which the generic patterns in
#: ``backend/gaming/events.py`` already read, not a *game*. Entries are stored
#: normalised (casefolded, whitespace collapsed) so membership is one lookup.
GENERIC_STRINGS: Final[frozenset[str]] = frozenset(
    {
        # Menu chrome the shipped profiles measured and ignore.
        "actions",
        "analyze",
        "available",
        "back",
        "brief",
        "category",
        "chop",
        "close",
        "explore",
        "load",
        "lower",
        "move",
        "place",
        "quit",
        "retrieve",
        "rotate",
        "scene",
        "sort",
        "stats",
        "take all",
        "type",
        # Pan-genre interface staples.
        "accept",
        "apply",
        "audio",
        "autosave",
        "buy",
        "cancel",
        "checkpoint",
        "confirm",
        "continue",
        "controls",
        "cook",
        "craft",
        "credits",
        "delete",
        "display",
        "done",
        "drop",
        "equip",
        "exit",
        "graphics",
        "hold",
        "inventory",
        "language",
        "loading",
        "main menu",
        "map",
        "menu",
        "new game",
        "next",
        "objectives",
        "open",
        "options",
        "pause",
        "paused",
        "play",
        "press",
        "quests",
        "restart",
        "resume",
        "retry",
        "save",
        "saving",
        "select",
        "sell",
        "settings",
        "skip",
        "start",
        "video",
        "volume",
        # HUD nouns every genre draws.
        "ammo",
        "armor",
        "armour",
        "damage",
        "energy",
        "health",
        "level",
        "score",
        "shield",
        "stamina",
        "time",
        "timer",
        # Event words: they name what happened, never which game (SPEC
        # section 23's generic path already reads them).
        "defeat",
        "game over",
        "victory",
        "you died",
        "you win",
    }
)


# ---------------------------------------------------------------------------
# Pure parts: mining, ranking, escaping. No database, no filesystem.
# ---------------------------------------------------------------------------


def normalise(text: str) -> str:
    """One reading in canonical form: whitespace collapsed and stripped.

    OCR of the same banner varies in spacing frame to frame; the words are the
    identity, the gaps are noise.
    """
    return " ".join(text.split())


def mine_strings(
    texts: Iterable[str],
    *,
    min_length: int = MIN_LENGTH,
    max_length: int = MAX_LENGTH,
) -> list[tuple[str, int]]:
    """Count the distinct readings, most frequent first.

    Counting is case-insensitive -- "Lean-To" and "LEAN-TO" are one string the
    engine read in two moods -- and each group is reported under its most
    common spelling, because that spelling is what a pattern will be written
    against. Ties break deterministically: by count, then alphabetically.
    """
    counts: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = {}
    for raw in texts:
        text = normalise(raw)
        if not (min_length <= len(text) <= max_length):
            continue
        key = text.casefold()
        counts[key] += 1
        spellings.setdefault(key, Counter())[text] += 1
    ranked = [(spellings[key].most_common(1)[0][0], total) for key, total in counts.items()]
    ranked.sort(key=lambda item: (-item[1], item[0].casefold()))
    return ranked


def signature_candidates(
    counted: Sequence[tuple[str, int]],
    *,
    min_count: int = MIN_COUNT,
    ignore: frozenset[str] = GENERIC_STRINGS,
) -> list[tuple[str, int]]:
    """The mined strings worth proposing as signatures, rank order preserved.

    Three filters, each dropping a class of string that cannot identify a game:
    seen fewer than ``min_count`` times (a misread as likely as a fact), no
    letter at all ("12/25" is a counter every menu draws), or generic interface
    vocabulary (the ``ignore`` set) that most games write verbatim.
    """
    kept: list[tuple[str, int]] = []
    for text, count in counted:
        if count < min_count:
            continue
        if not any(character.isalpha() for character in text):
            continue
        if normalise(text).casefold() in ignore:
            continue
        kept.append((text, count))
    return kept


def _is_word_character(character: str) -> bool:
    return re.match(r"\w", character) is not None


def escape_signature(text: str) -> str:
    """Turn one mined string into a signature pattern in the shipped style.

    Every literal is escaped, runs of whitespace become ``\\s+`` (the gap OCR
    is least consistent about), and the ends get ``\\b`` anchors -- but only
    where the edge is a word character, because a boundary beside punctuation
    ("O.R.C." ends in a dot) would quietly invert its meaning. The result
    compiles under ``re.IGNORECASE``, which is how every profile pattern is
    matched (``backend/gaming/profiles.py``).
    """
    words = normalise(text).split()
    if not words:
        return ""
    body = r"\s+".join(re.escape(word) for word in words)
    prefix = r"\b" if _is_word_character(words[0][0]) else ""
    suffix = r"\b" if _is_word_character(words[-1][-1]) else ""
    return f"{prefix}{body}{suffix}"


def snippet(texts: Sequence[str]) -> str:
    """A ready-to-paste ``signature_patterns`` block for ``profile.json``.

    Serialised with :func:`json.dumps` so the backslashes arrive doubled
    exactly as the profile file needs them: ``\\bMilk\\s+Molar\\b`` on disk.
    """
    return json.dumps(
        {"signature_patterns": [escape_signature(text) for text in texts]},
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# The thin shell: read the stored analysis, print the report.
# ---------------------------------------------------------------------------


def _print_ranked(rows: Sequence[tuple[str, int]], *, limit: int) -> None:
    for text, count in rows[:limit]:
        print(f"  {count:>5}  {text}")
    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("project_id", help="the project whose stored analysis to mine")
    args = parser.parse_args()

    # OCR text can be anything the screen showed; a Windows console defaults
    # to a codepage that cannot print half of it.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()
    paths = build_paths(config)
    database = Database(paths.database_path, config.application.database)
    try:
        projects = ProjectRepository(database)
        project = projects.get(args.project_id)
        if project is None:
            print(f"FAILED: no project {args.project_id!r} in {paths.database_path}.")
            known = projects.list(limit=20)
            if known:
                print("Projects that do exist (most recently updated first):")
                for item in known:
                    print(f"  {item.id}  {item.name}")
            return 1

        ocr = OcrRepository(database)
        vision = VisionRepository(database)
        texts: list[str] = []
        labels: Counter[str] = Counter()
        observations = 0
        media_items = MediaRepository(database).list_for_project(project.id)
        for media in media_items:
            texts.extend(detection.text for detection in ocr.list_for_media(media.id))
            labels.update(vision.label_counts(media.id))
            observations += vision.count_for_media(media.id)

        print(f"project  {project.id}  ({project.name})")
        print(f"game     {project.game or 'auto'}  detected: {project.detected_game or '-'}")
        print(
            f"stored   {len(media_items)} media file(s), {len(texts)} OCR readings, "
            f"{observations} vision observations"
        )
        if not texts:
            print("FAILED: this project has no stored OCR text. Has the OCR stage run?")
            return 1

        ranked = mine_strings(texts)
        print(f"\n--- top on-screen strings ({MIN_LENGTH}-{MAX_LENGTH} chars) ---")
        _print_ranked(ranked, limit=25)

        if labels:
            print("\n--- top vision labels ---")
            _print_ranked(sorted(labels.items(), key=lambda i: (-i[1], i[0])), limit=15)

        candidates = signature_candidates(ranked)
        print(
            f"\n--- signature candidates (>= {MIN_COUNT} readings, has a letter, "
            "not generic interface vocabulary) ---"
        )
        if not candidates:
            print("  none: everything frequent enough was generic interface text")
            return 0
        _print_ranked(candidates, limit=20)

        print("\n--- ready to paste into profiles/<game_id>/profile.json ---")
        print(snippet([text for text, _ in candidates[:SIGNATURE_LIMIT]]))
        print(
            "\nKeep only what no OTHER game would write: place names, item names, mode\n"
            "banners. detect_game (backend/gaming/detection.py) claims nothing below\n"
            "3 pattern hits and a lead of 2 over the runner-up, so validate the way\n"
            "docs/PROFILES.md and tests/unit/test_gaming.py (TestTheShipped*) do:\n"
            "your game detected with 0 runner-up hits, and every other profile still\n"
            "detected with 0."
        )
        return 0
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
