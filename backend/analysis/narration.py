"""Reading what the player *said happened* (SPEC §19, §21, §26, §92–§95).

Every other detector reads a signal: a loudness spike, a scene change, a model
describing a frame. None of them reads the one source that already contains the
story in words — the player narrating it.

The cost of that gap is measurable rather than theoretical. On a 41-minute
recording with **658 seconds of speech**, the pipeline produced 24 `surprise`
moments and 3 `tension` ones and nothing else: a vocabulary of two for a
recording in which the player says what is happening, continuously, out loud.
Moments came out 34 seconds long at the median and 164 at the worst, because a
detector that cannot tell one situation from another cannot find the seam
between them. The resulting video reads exactly as it was described: clips from
everywhere, no thread.

**This produces observations, not events.** That matters. Speech is evidence
like any other, and §27's correlation is what decides whether the shout and the
sentence and the scene cut are one thing. A narration observation that nothing
else saw stays a weak single-source suggestion; one that lands on an audio
spike and a scene change becomes a confident event. The model does not get to
declare an event on its own.

**Boundaries come from the narration, which is the point.** A player says "wait,
what's that" *before* the thing happens and "oh my god" *after* it. Asking where
a situation started and where the reaction ended is asking the only witness who
knows, and it is what turns a two-second spike into a clip with a cause.

**No model, no narration events (§95).** The rest of the pipeline is unchanged;
this adds a source, it does not become one the others depend on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ai.providers.base import LLMProvider, TranscriptSegment
from backend.config.schema import AppConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import GameEventType
from backend.core.prompts import load_prompt
from backend.gaming.events import EventObservation

logger = get_logger("analysis.narration", LogChannel.AI)

#: The detector's name in `sources`, so a reader can tell a narration-backed
#: event from an audio spike (§26).
SOURCE: Final[str] = "narration"

#: Shorter than this and there is no situation, only a word. Not configurable:
#: it is a fact about clips, not a tuning knob.
MIN_INCIDENT_SECONDS: Final[float] = 3.0


@dataclass(frozen=True, slots=True)
class Incident:
    """One thing that happened, as the player described it.

    ``climax_seconds`` is what separates this from a span: it is the instant the
    thing actually happened, with the cause before it and the reaction after.
    Downstream, that is the difference between a clip that lands and a clip
    that starts halfway through a joke.
    """

    title: str
    event_type: GameEventType
    start_seconds: float
    climax_seconds: float
    end_seconds: float
    importance: float
    quote: str = ""

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds


def read_incidents(
    segments: Sequence[TranscriptSegment],
    *,
    config: AppConfig,
    provider: LLMProvider | None = None,
) -> list[Incident]:
    """Segment narrated speech into complete incidents.

    Returns an empty list when there is no model, no speech, or nothing the
    model was confident about — all of which are ordinary (§95).
    """
    if not segments:
        return []
    # Whoever caused the model to load is who releases it. An injected provider
    # belongs to the caller and is left alone; one built here is ours, and
    # holding it costs the next stage the card. Measured before this existed:
    # GAME_EVENTS finished and left `qwen2.5:7b-instruct` resident with 5,958
    # MB held, through MOMENTS, STORY, EDL, CRITIQUE and the render (§54).
    ours = provider is None
    if ours:
        provider = _provider(config)
    if provider is None or not provider.is_available():
        logger.info("No model for narration; the other detectors carry the analysis")
        return []

    settings = config.analysis.narration
    prompt = load_prompt("analysis.narration")
    found: list[Incident] = []
    try:
        for window in _windows(segments, settings.window_seconds, settings.overlap_seconds):
            found.extend(_read_window(window, prompt, provider, settings))
    finally:
        if ours:
            provider.unload()

    incidents = _deduplicate(found)
    logger.info(
        "Read incidents from narration",
        extra={
            "segments": len(segments),
            "incidents": len(incidents),
            "types": sorted({item.event_type.value for item in incidents}),
        },
    )
    return incidents


#: How much of the climax an observation spans. An event is an instant; this is
#: wide enough to overlap a detector that saw the same thing a beat later.
CLIMAX_PAD_SECONDS: Final[float] = 4.0


def observations_from_narration(
    incidents: Sequence[Incident],
) -> list[EventObservation]:
    """Turn incidents into §26 observations for correlation.

    **The observation spans the climax, not the incident.** Handing correlation
    the whole cause-to-reaction span was a design error with a measurable cost:
    it produced 150-second "events", which §28 then merged into moments with a
    median length of 81 seconds and a maximum of 314, and the edit came out
    with six backwards jumps where it had none. An event is a thing that
    happened at a time. How much footage around it belongs in the clip is §29's
    question, and it is answered with the bounds carried in ``detail``.

    Confidence is the model's own importance, deliberately: a suggestion
    nothing else saw should not outrank a measured audio spike.
    """
    return [
        EventObservation(
            event_type=incident.event_type,
            start_seconds=max(0.0, incident.climax_seconds - CLIMAX_PAD_SECONDS),
            end_seconds=incident.climax_seconds + CLIMAX_PAD_SECONDS,
            source=SOURCE,
            confidence=incident.importance,
            detail={
                "title": incident.title,
                "quote": incident.quote,
                # What the player's own account says the clip should contain.
                "incident_start": round(incident.start_seconds, 3),
                "incident_end": round(incident.end_seconds, 3),
            },
        )
        for incident in incidents
    ]


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _provider(config: AppConfig) -> LLMProvider | None:
    from backend.core.errors import GamingEditorError

    try:
        from ai.llm import create_llm_provider

        return create_llm_provider(config)
    except GamingEditorError as error:
        logger.info("No language model is configured", extra={"error_code": error.code})
        return None


def _windows(
    segments: Sequence[TranscriptSegment], window_seconds: float, overlap_seconds: float
) -> list[list[TranscriptSegment]]:
    """Split the transcript into overlapping windows, in order."""
    ordered = sorted(segments, key=lambda item: item.start)
    windows: list[list[TranscriptSegment]] = []
    cursor = 0.0
    end = ordered[-1].end
    while cursor < end:
        stop = cursor + window_seconds
        window = [item for item in ordered if cursor <= item.start < stop]
        if window:
            windows.append(window)
        cursor = stop - overlap_seconds
    return windows


def _read_window(
    window: Sequence[TranscriptSegment],
    prompt: Any,
    provider: LLMProvider,
    settings: Any,
) -> list[Incident]:
    lines = "\n".join(
        f"[{item.start:.1f}] {item.text.strip()}" for item in window if item.text.strip()
    )
    if not lines:
        return []

    first = window[0].start
    last = window[-1].end
    rendered = prompt.render(
        transcript=lines,
        window_start=f"{first:.1f}",
        window_end=f"{last:.1f}",
        event_types=", ".join(item.value for item in GameEventType),
    )
    try:
        payload = provider.complete_json(
            rendered,
            schema=prompt.output_schema,
            prompt_id="analysis.narration",
            temperature=0.0,
        )
    except Exception as error:  # a window that fails is a window, not a run
        logger.warning(
            "Could not read a transcript window",
            extra={"error": str(error)[:200], "window_start": round(first, 1)},
        )
        return []

    return [
        incident
        for raw in payload.get("incidents", [])
        if (incident := _validate(raw, first, last, settings, lines)) is not None
    ]


#: Scripts a title must not be in unless the transcript is. qwen is a Chinese
#: model and leaks: reading an Arabic transcript it labelled three incidents
#: "合作" and "称赞". A title in a script that appears nowhere in the speech was
#: not read out of it.
_LEAKED_SCRIPTS = re.compile(r"[　-鿿가-힯぀-ヿ]")


def _title(raw: Any, window_text: str) -> str:
    """The incident's name, or nothing if the model wandered out of the language.

    Decorative -- what drives the edit is the type, the boundaries and the
    importance -- so a bad title is dropped rather than being allowed to
    invalidate an otherwise sound incident.
    """
    title = str(raw.get("title", "")).strip()[:120]
    if title and _LEAKED_SCRIPTS.search(title) and not _LEAKED_SCRIPTS.search(window_text):
        return ""
    return title


def _validate(
    raw: Any, window_start: float, window_end: float, settings: Any, window_text: str = ""
) -> Incident | None:
    """Accept an incident only if it is one, and inside the window it came from.

    A model asked for timestamps will occasionally invent one outside the text
    it was shown. Clamping that silently would move a clip to footage nobody
    looked at, so it is dropped instead.
    """
    if not isinstance(raw, dict):
        return None
    try:
        event_type = GameEventType(str(raw.get("event_type", "")).strip())
        start = float(raw["start_seconds"])
        climax = float(raw["climax_seconds"])
        end = float(raw["end_seconds"])
        importance = float(raw.get("importance", 0.0))
    except (KeyError, TypeError, ValueError):
        return None

    if not (window_start - 1.0 <= start <= climax <= end <= window_end + 1.0):
        return None
    if not MIN_INCIDENT_SECONDS <= end - start <= settings.max_incident_seconds:
        return None
    if importance < settings.min_importance:
        return None

    return Incident(
        title=_title(raw, window_text),
        event_type=event_type,
        start_seconds=max(0.0, start),
        climax_seconds=climax,
        end_seconds=end,
        importance=min(importance, 1.0),
        quote=str(raw.get("quote", "")).strip()[:200],
    )


def _deduplicate(incidents: Sequence[Incident]) -> list[Incident]:
    """Keep one incident per situation, the most important of the overlaps.

    Windows overlap on purpose, so the same situation is usually read twice.
    Two readings of one situation are not two incidents, and keeping both would
    put the same footage in the video twice.
    """
    ordered = sorted(incidents, key=lambda item: (-item.importance, item.start_seconds))
    kept: list[Incident] = []
    for incident in ordered:
        if any(_overlaps(incident, other) for other in kept):
            continue
        kept.append(incident)
    return sorted(kept, key=lambda item: item.start_seconds)


def _overlaps(left: Incident, right: Incident) -> bool:
    """Whether two incidents describe the same stretch of recording."""
    start = max(left.start_seconds, right.start_seconds)
    end = min(left.end_seconds, right.end_seconds)
    if end <= start:
        return False
    shorter = min(left.duration, right.duration) or 1.0
    return (end - start) / shorter >= 0.5


#: §93: the shape the model must answer in. Ollama enforces a schema as a
#: grammar, so every enum here has to be a value that really exists -- a wrong
#: one is *forced* out of the model and then rejected downstream, which fails
#: silently and permanently.
#: The window's shape, for readers and for the tests that hold the prompt file
#: to it. **The schema that runs is the one in `prompts/analysis/narration/`**,
#: read through :func:`load_prompt` -- there used to be a second copy here, and
#: the two had silently drifted: the file constrained times to be non-negative
#: and importance to 0-1, and the copy that was actually sent constrained
#: neither.
#:
#: Both were missing the bounds that matter. Ollama compiles the schema into
#: the grammar it decodes with, so a string with no `maxLength` is an
#: invitation to fill the output budget, and when the budget runs out
#: mid-string the JSON never closes. Every failure here costs a whole
#: six-minute window of speech, silently: :func:`_read_window` catches it, logs
#: one line, and returns nothing, so the recording is analysed as though the
#: player said nothing for six minutes. It was logged repeatedly through a real
#: 77-minute re-analysis before anybody read the line.
MAX_INCIDENTS_PER_WINDOW: Final[int] = 20
MAX_TITLE_CHARACTERS: Final[int] = 120

#: `Incident.quote` is no longer asked of the model, and the field stays for
#: the callers that read it. Measured against the real model on a 77-minute
#: Arabic transcript: three windows in fifteen died inside that field, the
#: model looping on repeated characters until the string never closed, and a
#: `maxLength` did not stop it -- Ollama grammar-constrains the *shape* of the
#: answer, not the length of its strings. Removing the field took those three
#: windows from truncated to parsed, and the words are not lost: every incident
#: carries the span they were said in, and the transcript is stored.
MAX_QUOTE_CHARACTERS: Final[int] = 200


__all__ = [
    "MIN_INCIDENT_SECONDS",
    "SOURCE",
    "Incident",
    "observations_from_narration",
    "read_incidents",
]
