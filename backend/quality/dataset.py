"""The golden dataset (SPEC §117).

    Real gameplay, manually annotated with important events, boring segments,
    best moments, reactions and game state. **This is the benchmark.**

Every number this project has reported so far has been a measurement of
*behaviour*: memory stayed flat, the overlay composited, five broken renders
were each caught by name. None of them says whether the moments it picks are
the ones a person would have picked, because nothing here knew what those were.

This module is where that changes, and the shape of it follows from one
constraint: **the labels must not come from the system's own output.** Reading
the pipeline's moments and ticking the plausible ones would produce excellent
precision and mean nothing. So an annotation is written against the recording,
by a person watching it, and the evaluator is never allowed to see it before it
is fixed.

Three consequences:

**The recordings stay out of the repository.** A dataset is a JSON file naming
a recording by path, size and duration. The videos are gigabytes and belong on
the drive they were captured on; what is version-controlled is the judgement
about them.

**Every span carries a confidence of its own.** Some moments are unarguable —
a death, a mission failure. Some are one person's opinion on a Tuesday. An
annotator who is unsure says so, and :mod:`backend.quality.metrics` can be
asked to score against the unarguable subset alone.

**Boring is a label too.** §117 lists "boring segments" beside "best moments",
and it is the more useful of the two: recall on highlights says what was found,
and a highlight overlapping a stretch marked dead says what should not have
been.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.errors import ConfigurationError, ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import GameEventType, MomentType

logger = get_logger("quality.dataset", LogChannel.PIPELINE)

DATASET_SUFFIX: Final[str] = ".dataset.json"

#: Format version. A dataset written against an older shape is refused rather
#: than half-read: a benchmark that silently loses half its labels reports a
#: recall improvement that is really a labelling loss.
SCHEMA_VERSION: Final[int] = 1


class SpanKind(str, Enum):
    """What an annotated span is a claim about (§117)."""

    #: Something happened that a detector should have found: a kill, a death,
    #: a mission ending. Scored against `game_events`.
    EVENT = "event"
    #: A stretch worth putting in the video. Scored against `moments`.
    HIGHLIGHT = "highlight"
    #: A stretch that is not: driving to the next objective, a menu, a pause.
    #: A highlight that lands here is a false positive nobody had to guess at.
    BORING = "boring"
    #: The player reacting — laughing, shouting, going quiet. §19 and §20.
    REACTION = "reaction"
    #: What the game was showing: a wanted level, a round, a score. §24.
    GAME_STATE = "game_state"


class Confidence(str, Enum):
    """How arguable this label is.

    Not a number, because the distinction that matters is categorical: either
    two annotators would agree or they might not, and a 0.8 pretends to a
    precision that a person watching a video does not have.
    """

    #: Unarguable. A death is a death.
    CERTAIN = "certain"
    #: One reasonable person's judgement. Another might disagree.
    OPINION = "opinion"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Span(_Model):
    """One annotated stretch of a recording."""

    kind: SpanKind
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(gt=0.0)

    #: For `event` spans: which detector should have caught it.
    event_type: GameEventType | None = None
    #: For `highlight` spans: what kind of moment a person would call it.
    moment_type: MomentType | None = None
    #: For `game_state` spans: the indicator and its value, e.g.
    #: ``{"wanted_level": 4}``. Free-form because §24's indicators are.
    state: dict[str, Any] = Field(default_factory=dict)

    confidence: Confidence = Confidence.CERTAIN
    #: Why this is labelled. Read by a person, not by code -- but the reason a
    #: label exists is the first thing anyone re-checking it wants.
    note: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> Span:
        if self.end_seconds <= self.start_seconds:
            raise ValueError(
                f"A span must end after it starts: {self.start_seconds} -> {self.end_seconds}."
            )
        if self.kind is SpanKind.EVENT and self.event_type is None:
            raise ValueError("An 'event' span must say which event type it is.")
        if self.kind is SpanKind.GAME_STATE and not self.state:
            raise ValueError("A 'game_state' span must say what the state was.")
        return self

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0

    def overlaps(self, start: float, end: float) -> float:
        """Seconds of overlap with ``start``-``end``. Zero when disjoint."""
        return max(0.0, min(self.end_seconds, end) - max(self.start_seconds, start))


class AnnotatedRecording(_Model):
    """One recording and every judgement made about it."""

    #: Where the recording was when it was annotated. Absolute, because a
    #: dataset is a local benchmark against local files (§50).
    source_path: str = Field(min_length=1)
    #: Both are recorded so a renamed or re-encoded file is caught rather than
    #: silently benchmarked against the wrong labels.
    size_bytes: int = Field(ge=0)
    duration_seconds: float = Field(gt=0.0)

    game: str = ""
    #: The stretch that was actually watched. Recall means nothing without it:
    #: a labeller who watched ten minutes of an hour has not found the other
    #: fifty minutes' events, and scoring against the whole hour would call
    #: that a detector failure.
    annotated_from_seconds: float = Field(default=0.0, ge=0.0)
    annotated_to_seconds: float | None = None

    spans: tuple[Span, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _within_the_recording(self) -> AnnotatedRecording:
        window_end = self.annotated_to_seconds
        if window_end is not None and window_end <= self.annotated_from_seconds:
            raise ValueError("The annotated window must end after it starts.")
        for span in self.spans:
            if span.end_seconds > self.duration_seconds + 1.0:
                raise ValueError(
                    f"A span runs past the end of the recording: "
                    f"{span.end_seconds:.1f}s > {self.duration_seconds:.1f}s."
                )
        return self

    @property
    def window(self) -> tuple[float, float]:
        """The stretch that was watched, as ``(start, end)``."""
        return (
            self.annotated_from_seconds,
            self.annotated_to_seconds
            if self.annotated_to_seconds is not None
            else self.duration_seconds,
        )

    @property
    def annotated_seconds(self) -> float:
        start, end = self.window
        return max(0.0, end - start)

    def of_kind(self, kind: SpanKind, *, certain_only: bool = False) -> tuple[Span, ...]:
        return tuple(
            span
            for span in self.spans
            if span.kind is kind
            and (not certain_only or span.confidence is Confidence.CERTAIN)
        )

    def within_window(self, start: float, end: float) -> bool:
        """Whether a prediction falls inside the stretch that was watched.

        Predictions outside it are neither right nor wrong -- nobody looked --
        so the evaluator discards them rather than counting them as false
        positives.
        """
        window_start, window_end = self.window
        return start >= window_start - 1e-6 and end <= window_end + 1e-6


class GoldenDataset(_Model):
    """The benchmark: several recordings, each annotated (§117)."""

    schema_version: int = SCHEMA_VERSION
    name: str = Field(min_length=1)
    description: str = ""
    recordings: tuple[AnnotatedRecording, ...] = ()

    @model_validator(mode="after")
    def _known_version(self) -> GoldenDataset:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"This dataset is schema version {self.schema_version}; this build reads "
                f"{SCHEMA_VERSION}. Refusing rather than reading it partially -- a "
                "benchmark that silently drops labels reports a recall gain that is a "
                "labelling loss."
            )
        return self

    @property
    def total_spans(self) -> int:
        return sum(len(recording.spans) for recording in self.recordings)

    @property
    def annotated_seconds(self) -> float:
        return sum(recording.annotated_seconds for recording in self.recordings)

    def for_source(self, source_path: str) -> AnnotatedRecording | None:
        """The annotations for one recording, matched by filename.

        By filename rather than by full path: the same recording legitimately
        moves between drives, and a benchmark that stops working when a folder
        is renamed will simply stop being run.
        """
        wanted = Path(source_path).name.casefold()
        for recording in self.recordings:
            if Path(recording.source_path).name.casefold() == wanted:
                return recording
        return None

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for recording in self.recordings:
            for span in recording.spans:
                counts[span.kind.value] = counts.get(span.kind.value, 0) + 1
        return {
            "name": self.name,
            "recordings": len(self.recordings),
            "spans": self.total_spans,
            "annotated_minutes": round(self.annotated_seconds / 60.0, 1),
            "by_kind": counts,
        }


def load_dataset(path: Path) -> GoldenDataset:
    """Read a dataset from disk.

    Raises:
        ConfigurationError: the file is missing, unreadable, or does not
            validate. A benchmark that runs against a broken dataset is worse
            than one that does not run.
    """
    file = Path(path)
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"Golden dataset {file.name!r} could not be read: {exc}",
            code=ErrorCode.CONFIG_INVALID,
            details={"path": str(file)},
            cause=exc,
            recoverable=False,
        ) from exc

    try:
        dataset = GoldenDataset.model_validate(payload)
    except Exception as exc:
        raise ConfigurationError(
            f"Golden dataset {file.name!r} is invalid: {exc}",
            code=ErrorCode.CONFIG_INVALID,
            details={"path": str(file)},
            cause=exc,
            recoverable=False,
        ) from exc

    logger.info("Loaded a golden dataset", extra=dataset.summary())
    return dataset


def available_datasets(directory: Path) -> tuple[Path, ...]:
    """Every dataset file in ``directory``."""
    base = Path(directory)
    if not base.is_dir():
        return ()
    return tuple(sorted(base.glob(f"*{DATASET_SUFFIX}")))


__all__ = [
    "DATASET_SUFFIX",
    "SCHEMA_VERSION",
    "AnnotatedRecording",
    "Confidence",
    "GoldenDataset",
    "Span",
    "SpanKind",
    "available_datasets",
    "load_dataset",
]
