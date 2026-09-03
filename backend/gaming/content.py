"""What is on the screen when it is not the game (V2-P0.1).

Every stage after this one has always assumed that a second of recording is a
second of gameplay. It is not. A recording contains menus, loading screens,
pause screens, the game's own intro, and the screen a player sees when they
die -- and this system has been treating all of it as material to be scored,
selected and cut.

The evidence that it is not gameplay has been on disk the whole time. On the
88-minute session this module was written from, ``ocr_results`` holds 18,827
rows and reads, at 3:51.50 of the source::

    LOAD   REPLAY   REPLANMISSION   EXIT TO MENU   AGENT DOWN:   MISSIONFAILED

Nothing read it. The moment formed over that stretch anyway, the optimiser
selected it, and seventeen seconds of menus and a loading screen reached the
finished video at 1:47. This module is the consumer that was missing.

**Spans, never points.** OCR samples a frame every 7.1 seconds on that
recording, so a menu that is on screen for twenty seconds is *read* at one
instant. A design that treats these signals as points fails exactly the way
``_inside`` and ``_cuts`` failed earlier in this project: a point filter over
data that has extent. Every rule therefore declares how far its state reaches
either side of the instant that proves it.

**OCR decides, vision supports.** At the very instant the OCR read
``MISSIONFAILED``, the vision model described the same frame as "a combat
situation, as indicated by the 'COMBAT' label". Had the two been weighed
equally the truth would have lost. Literal on-screen text is a reading; a
description is an interpretation, and this module ranks them accordingly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger

logger = get_logger("gaming.content", LogChannel.PIPELINE)


class ContentState(str, Enum):
    """What the screen is showing, as a class of thing rather than an event.

    Deliberately not part of :class:`GameEventType`. That enum is the
    vocabulary of things that *happen in the game* -- a kill, a death, a
    victory -- and every consumer of it treats a member as material worth
    selecting. Putting ``MENU`` in the same vocabulary would make a menu
    selectable, which is the defect this module exists to remove.
    """

    #: The default, and the only state anything downstream may select from.
    GAMEPLAY = "gameplay"
    #: Any navigable game interface: main menu, pause menu, inventory shell.
    MENU = "menu"
    #: A loading screen or transition, whether or not it names what it loads.
    LOADING = "loading"
    #: The game paused. Distinct from MENU because a pause interrupts play
    #: that is still in progress, and an attempt is not over when one appears.
    PAUSE = "pause"
    #: The game's own opening: title cards, briefings, "Welcome to ...".
    GAME_INTRO = "game_intro"
    #: The screen shown when a run ends badly. This is what ends an attempt.
    MISSION_FAILED = "mission_failed"
    #: A restart prompt or confirmation. This is what begins the next one.
    RESTART = "restart"
    #: Black or near-black frames, from the encoder's own measurement.
    BLACK_SCREEN = "black_screen"
    #: A non-interactive scene. Kept deliberately weak from text alone --
    #: nothing written on screen names a cutscene -- but the vision model has
    #: a label for it, and that is where this state usually comes from.
    CUTSCENE = "cutscene"
    #: The recorder's own window: OBS chrome, the desktop, a file dialog.
    #: Never text; always the vision model's word.
    DESKTOP = "desktop"

    @property
    def is_gameplay(self) -> bool:
        return self is ContentState.GAMEPLAY


#: How the vision model's own vocabulary maps onto this one.
#:
#: :mod:`backend.analysis.frame_state` already turns vision labels into spans,
#: with decay, bridging and a label table argued from real footage -- including
#: the counter-case where ``inventory`` means a HUD and not a menu. None of
#: that is rebuilt here. This module's addition is the OCR half, and the two
#: are merged rather than run in parallel: measured on the 88-minute session,
#: the vision spans and the text reads refuse **ten and sixteen** clips of the
#: shipped render and share only **four**. Neither source alone is enough.
#:
#: ``HUD_ONLY`` maps to gameplay on purpose: a scoreboard drawn over a fight is
#: a fight. ``UNKNOWN`` is absent for the reason ``FrameState`` gives it --
#: an unlabelled frame is not evidence of anything.
FROM_FRAME_STATE: Final[dict[str, ContentState]] = {
    "menu": ContentState.MENU,
    "loading": ContentState.LOADING,
    "pause": ContentState.PAUSE,
    "cutscene": ContentState.CUTSCENE,
    "desktop": ContentState.DESKTOP,
    "transition": ContentState.LOADING,
}


#: States that end an attempt at the mission being played (V2-P0.5 reads this).
ENDS_ATTEMPT: Final[frozenset[ContentState]] = frozenset(
    {ContentState.MISSION_FAILED, ContentState.RESTART}
)

#: States that mark the boundary between one attempt and the next.
BEGINS_ATTEMPT: Final[frozenset[ContentState]] = frozenset(
    {ContentState.LOADING, ContentState.GAME_INTRO}
)

#: Below this, a reading is recorded but nothing is excluded on its strength.
#: Set where it is because the states this module is confident about are read
#: as literal text: a rule that cannot reach 0.6 is guessing.
EXCLUSION_FLOOR: Final[float] = 0.60

#: What one vision span is worth on its own -- at the floor, not below it.
#:
#: The first cut of this module put it at 0.45, on the grounds that the vision
#: model had called a MISSION FAILED screen "a combat situation". That was the
#: wrong lesson from the right observation: the sentence was a *description*,
#: and `frame_state` does not read descriptions. It reads the model's
#: **labels**, and at that same instant the label was ``loading`` -- correct.
#: Distrusting labels for a description's error cost six real menus, measured:
#: they reached the shipped render and this module refused to refuse them.
#:
#: So a vision span is trusted exactly as much as the existing consumer of
#: `frame_state` already trusts it, which is fully. Text then raises it further.
VISION_ALONE: Final[float] = 0.60

#: What a run of agreeing observations reaches. Four frames in a row is a
#: stronger claim than one, and still short of a literal read.
VISION_AGREED: Final[float] = 0.85

#: How much agreement between a text read and a description is worth. Applied
#: once, not per matching line: ten OCR lines from the same menu are one
#: observation of one menu.
AGREEMENT_BONUS: Final[float] = 0.15

#: The widest gap between two refusals that may be closed. Borrowed from
#: `frame_state.BRIDGE_SECONDS`, and bound by the same guard it uses: a gap is
#: only closed when no detector looked inside it. Six seconds is under one OCR
#: sampling interval on the footage this was measured against, so a bridge
#: spans at most one unsampled frame.
BRIDGE_SECONDS: Final[float] = 6.0


@dataclass(frozen=True, slots=True)
class Evidence:
    """One observation that supports a state, kept whole for the record."""

    #: ``ocr`` | ``vision`` | ``black`` -- which store this came from.
    source: str
    #: Where in the recording it was observed.
    at: float
    #: The text or phrase that matched, truncated for logging.
    what: str
    #: Which rule matched, by name.
    rule: str


@dataclass(frozen=True, slots=True)
class GameplayState:
    """A stretch of recording, and what was on the screen during it.

    A span rather than an instant, because that is what the thing being
    described is. ``confidence`` is what this reading is worth; ``evidence``
    is why; ``reason`` is the sentence a person reads in a log.
    """

    state: ContentState
    start: float
    end: float
    confidence: float
    evidence: tuple[Evidence, ...] = ()
    reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def excludes(self) -> bool:
        """Whether this reading is strong enough to keep footage out."""
        return not self.state.is_gameplay and self.confidence >= EXCLUSION_FLOOR

    def covers(self, start: float, end: float) -> bool:
        """Whether this state overlaps ``start``-``end`` at all.

        Overlap, not containment. A moment that merely *touches* a menu is a
        moment whose footage contains a menu, and the containment test is the
        one that let three minutes of source through in the first place.
        """
        return start < self.end and self.start < end


@dataclass(frozen=True, slots=True)
class ContentRule:
    """Text or description that identifies a state, and how far it reaches.

    The plain-dataclass twin of :class:`backend.gaming.profiles.ContentRule`,
    which is its JSON form. Profiles are configuration and validate through
    pydantic; this module stays a domain object that knows nothing about files
    -- the same split :class:`FusionRule` already uses.
    """

    state: ContentState
    name: str
    patterns: tuple[str, ...] = ()
    #: Restrict OCR matching to these named regions. Empty matches anywhere,
    #: which is what an unknown layout has to do.
    regions: tuple[str, ...] = ()
    #: What one match of this rule is worth on its own, from OCR.
    confidence: float = 0.8
    #: How far back the state was already true before the frame that proved
    #: it, and how far forward it persists after. Both matter: a menu is on
    #: screen for seconds before the sampled frame that reads it.
    lead_seconds: float = 4.0
    hold_seconds: float = 8.0
    #: Whether a vision description alone may raise this rule at all. False
    #: for anything whose whole identity is literal text.
    vision_may_raise: bool = True
    #: How many *distinct* patterns must match on one frame before the rule
    #: raises. One is the ordinary rule: a line that says MISSION FAILED is a
    #: mission failed. Above one the rule is a conjunction, and exists for the
    #: words that mean nothing alone -- INVENTORY is a hotbar in Grounded and
    #: a tab in HITMAN's pause menu, and only the company it keeps tells them
    #: apart (V2-P0.4).
    min_matches: int = 1

    def matches(self, text: str, *, region: str | None = None) -> bool:
        """Whether ``text`` (optionally from ``region``) satisfies this rule."""
        if self.regions and (region is None or region not in self.regions):
            return False
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in self.patterns)

    def matched_patterns(self, detections: Iterable[Any]) -> set[int]:
        """Which of this rule's patterns the frame's detections satisfy.

        Indices rather than a count, so ten OCR lines that all read RESUME
        are one match and not ten -- a conjunction needs different words.
        """
        found: set[int] = set()
        for detection in detections:
            text = str(getattr(detection, "text", "") or "")
            region = getattr(detection, "region", None)
            if self.regions and (region is None or region not in self.regions):
                continue
            for index, pattern in enumerate(self.patterns):
                if index not in found and re.search(pattern, text, re.IGNORECASE):
                    found.add(index)
        return found


def _rule(
    state: ContentState,
    name: str,
    *patterns: str,
    confidence: float = 0.8,
    lead: float = 4.0,
    hold: float = 8.0,
    vision_may_raise: bool = True,
    min_matches: int = 1,
) -> ContentRule:
    return ContentRule(
        state=state,
        name=name,
        patterns=tuple(patterns),
        confidence=confidence,
        lead_seconds=lead,
        hold_seconds=hold,
        vision_may_raise=vision_may_raise,
        min_matches=min_matches,
    )


#: The table every game gets before its own profile is consulted.
#:
#: Written from on-screen text this project has actually read, not from
#: imagination: every pattern here appeared in the OCR or the vision
#: descriptions of the 88-minute HITMAN session, or in the Grounded and GTA
#: recordings analysed earlier. Wording that only one game uses belongs in
#: that game's profile, not here.
#:
#: A profile may add rules of its own, and may disable any of these by name
#: through ``suppressed_content_rules`` -- the same escape hatch
#: ``suppressed_generic_rules`` already gives the fusion table, and for the
#: same reason: the common case is written here, and a game that knows better
#: says so by name.
GENERIC_CONTENT_RULES: Final[tuple[ContentRule, ...]] = (
    # -- failure ---------------------------------------------------------
    _rule(
        ContentState.MISSION_FAILED,
        "mission_failed_text",
        r"\bMISSION\s*FAILED\b",
        r"\bAGENT\s+DOWN\b",
        r"\bYOU\s+(?:DIED|WERE\s+KILLED)\b",
        r"\bGAME\s+OVER\b",
        confidence=0.9,
        lead=3.0,
        hold=10.0,
    ),
    # -- restart ---------------------------------------------------------
    _rule(
        ContentState.RESTART,
        "restart_prompt",
        r"\bRESTART\s+MISSION\b",
        r"\bRESTART\s+(?:CHECKPOINT|LEVEL|FROM)\b",
        r"\bRETRY\b",
        r"restart\s+the\s+mission\?",
        confidence=0.85,
        lead=2.0,
        hold=8.0,
    ),
    # -- menus -----------------------------------------------------------
    _rule(
        ContentState.MENU,
        "menu_navigation",
        r"\bEXIT\s+TO\s+MENU\b",
        r"\bREPLAN\s*MISSION\b",
        r"\bMAIN\s+MENU\b",
        r"\bQUIT\s+TO\s+(?:MENU|DESKTOP)\b",
        confidence=0.85,
        lead=4.0,
        hold=8.0,
    ),
    # -- pause -----------------------------------------------------------
    #
    # Separate from MENU because a pause does not end an attempt: the run is
    # still in progress behind it. V2-P0.5 depends on that distinction.
    _rule(
        ContentState.PAUSE,
        "pause_screen",
        r"\bGAME\s+PAUSED\b",
        r"\bPAUSED\b",
        r"^\s*RESUME\s*$",
        r"\bPAUSE\s+MENU\b",
        confidence=0.8,
        lead=3.0,
        hold=6.0,
    ),
    # A pause menu's tabs, read off one frame. None of these words is a menu
    # on its own: INVENTORY is Grounded's hotbar label 79 times in the stored
    # reads, MAP and OPTIONS are button prompts. Three distinct ones on the
    # same frame have been a menu every time on this machine -- 45 stored
    # frames across every recording, zero exceptions -- and are the reading that
    # HITMAN's pause screen, which never says PAUSED, gives the OCR (V2-P0.4).
    _rule(
        ContentState.PAUSE,
        "menu_tabs",
        r"^\s*OBJECTIVES\s*$",
        r"^\s*MAP\s*$",
        r"^\s*INVENTORY\s*$",
        r"^\s*MISSION\s+STORIES\s*$",
        r"^\s*INTEL\s*$",
        r"^\s*OPTIONS\s*$",
        r"^\s*SETTINGS\s*$",
        r"^\s*SAVE\s*$",
        r"^\s*LOAD\s*$",
        r"^\s*RESUME\s*$",
        r"^\s*QUIT\s*$",
        r"^\s*PHOTO\s+MODE\s*$",
        r"^\s*SURVIVAL\s+GUIDE\s*$",
        r"^\s*CONTROLS\s*$",
        r"^\s*PAUSE\s+MENU\s*$",
        confidence=0.8,
        lead=3.0,
        hold=6.0,
        vision_may_raise=False,
        min_matches=3,
    ),
    # -- loading ---------------------------------------------------------
    _rule(
        ContentState.LOADING,
        "loading_screen",
        r"\bLOADING\b",
        r"\bPLEASE\s+WAIT\b",
        r"\bCHECKPOINT\s+REACHED\b",
        confidence=0.75,
        lead=3.0,
        hold=8.0,
    ),
    # -- the game's own opening ------------------------------------------
    #
    # `T[O0]` rather than `TO`, and it is not over-fitting. Engines confuse
    # the letter O with the digit zero constantly, and on the recording this
    # was written from the OCR read "Welcome t0" three times out of four --
    # the fourth, spelled correctly, is the only reason this rule fired at
    # all. The same frames produced "Sapienza Ilaly" for Italy. That is a
    # property of reading text off a compressed frame, not of one game.
    _rule(
        ContentState.GAME_INTRO,
        "welcome_briefing",
        r"\bWELCOME\s+T[O0]\b",
        confidence=0.7,
        lead=4.0,
        hold=10.0,
    ),
    # -- cutscene, and why it is weak ------------------------------------
    #
    # Nothing stored names a cutscene. This rule fires on the vision model's
    # own words and is worth less than the exclusion floor on its own,
    # deliberately: it is a reading worth recording and not yet worth acting
    # on. Raising it needs a measurement this project has not made.
    _rule(
        ContentState.CUTSCENE,
        "cutscene_described",
        r"\bcut\s?scene\b",
        r"\bcinematic\s+(?:sequence|shot|cut)\b",
        confidence=0.5,
        lead=2.0,
        hold=6.0,
    ),
)


def rules_for(profile: Any) -> tuple[ContentRule, ...]:
    """The rules in force for one game: the generic table, then its own.

    A profile's rules are appended rather than substituted, and the generic
    ones it names in ``suppressed_content_rules`` are dropped. Appending is
    what makes an unknown game work at all -- the generic table is written for
    the common case, and most games say ``LOADING`` the same way.
    """
    suppressed = set(getattr(profile, "suppressed_content_rules", ()) or ())
    generic = tuple(rule for rule in GENERIC_CONTENT_RULES if rule.name not in suppressed)
    own = tuple(
        ContentRule(
            state=ContentState(rule.state),
            name=rule.name,
            patterns=tuple(rule.patterns),
            regions=tuple(getattr(rule, "regions", ()) or ()),
            confidence=float(rule.confidence),
            lead_seconds=float(getattr(rule, "lead_seconds", 4.0)),
            hold_seconds=float(getattr(rule, "hold_seconds", 8.0)),
            vision_may_raise=bool(getattr(rule, "vision_may_raise", True)),
            min_matches=int(getattr(rule, "min_matches", 1) or 1),
        )
        for rule in (getattr(profile, "content_rules", ()) or ())
    )
    if suppressed:
        logger.info(
            "A profile suppressed generic content rules",
            extra={"suppressed": sorted(suppressed)},
        )
    return generic + own


def read(
    *,
    detections: Sequence[Any] = (),
    frame_spans: Sequence[Any] = (),
    black_spans: Sequence[tuple[float, float]] = (),
    profile: Any = None,
    duration_seconds: float | None = None,
) -> list[GameplayState]:
    """Read the recording's content states from the evidence already stored.

    Args:
        detections: OCR reads, each carrying ``text``, ``timestamp`` and an
            optional ``region``. The strongest evidence there is: literal text.
        frame_spans: the vision half, as :class:`frame_state.StateSpan` --
            already decayed, bridged and mapped from labels by a module that
            was tuned on real footage. Not re-derived here. Supporting rather
            than deciding: see :data:`VISION_ALONE` and the measurement in
            :data:`FROM_FRAME_STATE`.
        black_spans: ``(start, end)`` from the encoder's own black detection,
            which QA already runs. Measured, not inferred.
        profile: the game's profile, for its own rules and suppressions.
        duration_seconds: the recording's length, to bound the last span.

    Returns:
        Non-gameplay states in time order, merged so that one menu is one
        state however many frames read it. Gameplay is the absence of these
        and is never returned: a list of everything that is *not* the game is
        smaller, and saying "this stretch is gameplay" is a claim this module
        has no evidence for.
    """
    rules = rules_for(profile)
    single = [rule for rule in rules if rule.min_matches <= 1]
    joint = [rule for rule in rules if rule.min_matches > 1]
    raised: list[tuple[ContentState, float, float, float, Evidence]] = []

    for detection in detections:
        text = str(getattr(detection, "text", "") or "")
        if not text.strip():
            continue
        at = float(getattr(detection, "timestamp", 0.0) or 0.0)
        region = getattr(detection, "region", None)
        for rule in single:
            if not rule.matches(text, region=region):
                continue
            raised.append(
                (
                    rule.state,
                    max(0.0, at - rule.lead_seconds),
                    at + rule.hold_seconds,
                    rule.confidence,
                    Evidence("ocr", at, text[:60], rule.name),
                )
            )

    # A conjunction is judged per frame: the words have to share a screen.
    if joint:
        frames: dict[float, list[Any]] = {}
        for detection in detections:
            if str(getattr(detection, "text", "") or "").strip():
                at = round(float(getattr(detection, "timestamp", 0.0) or 0.0), 3)
                frames.setdefault(at, []).append(detection)
        for at, lines in sorted(frames.items()):
            for rule in joint:
                found = rule.matched_patterns(lines)
                if len(found) < rule.min_matches:
                    continue
                words = [
                    str(getattr(line, "text", "")).strip()
                    for line in lines
                    if rule.matches(str(getattr(line, "text", "") or ""))
                ]
                raised.append(
                    (
                        rule.state,
                        max(0.0, at - rule.lead_seconds),
                        at + rule.hold_seconds,
                        rule.confidence,
                        Evidence("ocr", at, " / ".join(words)[:60], rule.name),
                    )
                )

    for span in frame_spans:
        mapped = FROM_FRAME_STATE.get(str(getattr(getattr(span, "state", None), "value", "")))
        if mapped is None:
            continue
        seen = int(getattr(span, "observations", 1) or 1)
        raised.append(
            (
                mapped,
                float(span.start_seconds),
                float(span.end_seconds),
                # Four frames agreeing is stronger than one, and still short of
                # what a literal read is worth. The ceiling keeps a long
                # confident-looking run of descriptions from outvoting text.
                min(VISION_AGREED, VISION_ALONE + 0.08 * (seen - 1)),
                Evidence(
                    "vision",
                    float(span.start_seconds),
                    f"{span.state.value} over {span.duration:.1f}s, {seen} observations",
                    "frame_state",
                ),
            )
        )

    for start, end in black_spans:
        raised.append(
            (
                ContentState.BLACK_SCREEN,
                float(start),
                float(end),
                0.95,
                Evidence("black", float(start), f"{end - start:.2f}s of black", "blackdetect"),
            )
        )

    states = _merged(raised)
    if duration_seconds is not None:
        states = [
            GameplayState(
                state=item.state,
                start=min(item.start, duration_seconds),
                end=min(item.end, duration_seconds),
                confidence=item.confidence,
                evidence=item.evidence,
                reason=item.reason,
            )
            for item in states
            if item.start < duration_seconds
        ]
    return [item for item in states if item.duration > 0.0]


def _merged(
    raised: Iterable[tuple[ContentState, float, float, float, Evidence]],
) -> list[GameplayState]:
    """One state per stretch, however many frames raised it.

    Merging by state and overlap rather than by frame is what turns a sampled
    signal back into the thing it was sampled from. Ten OCR lines off one menu
    are one menu, and confidence is the strongest single reading plus a bonus
    when two different stores agree -- not a sum, which would let a chatty
    menu out-vote a certain one.
    """
    by_state: dict[ContentState, list[list[Any]]] = {}
    for state, start, end, confidence, evidence in sorted(
        raised, key=lambda item: (item[0].value, item[1])
    ):
        buckets = by_state.setdefault(state, [])
        if buckets and start <= buckets[-1][1]:
            bucket = buckets[-1]
            bucket[1] = max(bucket[1], end)
            bucket[2] = max(bucket[2], confidence)
            bucket[3].append(evidence)
        else:
            buckets.append([start, end, confidence, [evidence]])

    merged: list[GameplayState] = []
    for state, buckets in by_state.items():
        for start, end, confidence, evidence in buckets:
            sources = {item.source for item in evidence}
            score = confidence
            if len(sources) > 1:
                score = min(1.0, score + AGREEMENT_BONUS)
            names = sorted({item.rule for item in evidence})
            merged.append(
                GameplayState(
                    state=state,
                    start=round(start, 3),
                    end=round(end, 3),
                    confidence=round(min(1.0, max(0.0, score)), 4),
                    evidence=tuple(evidence),
                    reason=(
                        f"{state.value} read from {', '.join(sorted(sources))} "
                        f"by {', '.join(names)}"
                    ),
                )
            )
    merged.sort(key=lambda item: (item.start, item.state.value))
    return merged


def excluded_spans(
    states: Sequence[GameplayState],
    *,
    observed_at: Sequence[float] = (),
    bridge_seconds: float = BRIDGE_SECONDS,
) -> list[tuple[float, float]]:
    """The stretches nothing downstream may select from, merged and ordered.

    The one shape most callers want, so that no caller has to remember the
    floor. States below :data:`EXCLUSION_FLOOR` are absent from the result and
    present in ``states`` -- recorded, and not acted on.

    **Short gaps between two refusals are closed**, and this is not tidiness.
    The first render built with this layer still carried three frames of a
    loading screen, in a 1.75-second island between a menu read at 3:51.50 and
    a title card read at 4:07.25. Nothing was sampled inside that island; it
    was called gameplay because nobody looked, not because anybody saw a game.
    A one-second stretch of play between a menu and a mission briefing is not
    a thing that happens.

    ``observed_at`` is the guard that keeps this honest, and it is
    :mod:`frame_state`'s own: a gap is bridged **only when no observation
    falls inside it**. Where a detector did look and saw the game, the gap
    stands, however short.
    """
    spans = sorted((item.start, item.end) for item in states if item.excludes)
    if not spans:
        return []
    looked = sorted(float(at) for at in observed_at)
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        gap = start - merged[-1][1]
        bridgeable = gap <= bridge_seconds and not _looked_between(
            looked, merged[-1][1], start
        )
        if gap <= 0 or bridgeable:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(round(a, 3), round(b, 3)) for a, b in merged]


def _looked_between(looked: Sequence[float], start: float, end: float) -> bool:
    """Whether any detector sampled a frame strictly inside ``start``-``end``."""
    return any(start < at < end for at in looked)


def overlaps(spans: Sequence[tuple[float, float]], start: float, end: float) -> bool:
    """Whether ``start``-``end`` touches any excluded span at all."""
    return any(start < b and a < end for a, b in spans)


__all__ = [
    "AGREEMENT_BONUS",
    "BEGINS_ATTEMPT",
    "BRIDGE_SECONDS",
    "ENDS_ATTEMPT",
    "EXCLUSION_FLOOR",
    "GENERIC_CONTENT_RULES",
    "VISION_ALONE",
    "ContentRule",
    "ContentState",
    "Evidence",
    "GameplayState",
    "excluded_spans",
    "overlaps",
    "read",
    "rules_for",
]
