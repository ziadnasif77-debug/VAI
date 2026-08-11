"""Precision, recall, and the rest of §118.

    Event precision/recall · moment precision/recall · false positive rate ·
    false negative rate · target duration error · render failure rate.

The arithmetic is the easy part. Two decisions decide whether the numbers mean
anything, and both are made here rather than left implicit:

**When does a prediction match a label?** Not "same timestamp" — nothing is.
Two rules, chosen per kind because they answer different questions:

* An **event** is an instant. It matches when its midpoint lands within a
  tolerance of the labelled span. Asking a kill detected at 812.4s to overlap a
  label written as 812-815 by 50% would fail a correct detection.
* A **moment** is a stretch, and a moment that shares one second with a
  labelled highlight has not found it. So moments match on **overlap** — by
  default half of the shorter span, which is lenient enough for a boundary
  drawn by hand and strict enough that a clip covering a different scene fails.

**What is outside the annotated window?** Discarded. An annotator who watched
ten minutes of an hour has not found the other fifty minutes' events, and
scoring a prediction there as a false positive measures how long they watched
rather than how well the system works. This is the single easiest way to make a
benchmark lie, and it lies in the flattering direction.

One prediction never matches two labels, and one label is never found twice —
matching is greedy on the best overlap, so five clips over one highlight are
one true positive and four false ones. Which is right: they are four clips a
person would delete.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.core.logging import LogChannel, get_logger
from backend.quality.dataset import AnnotatedRecording, Span, SpanKind

logger = get_logger("quality.metrics", LogChannel.PIPELINE)

#: How far an event's midpoint may sit from its labelled span, in seconds.
#: Generous because a hand-written label marks when a person *noticed*, which
#: trails the detector by a beat.
EVENT_TOLERANCE_SECONDS: float = 3.0

#: Fraction of the shorter span two moments must share to be the same moment.
MOMENT_OVERLAP: float = 0.5


class Predicted(Protocol):
    """What the evaluator needs from anything the pipeline produced."""

    start_seconds: float
    end_seconds: float


@dataclass(frozen=True, slots=True)
class Prediction:
    """One thing the system claimed, reduced to what scoring needs."""

    start_seconds: float
    end_seconds: float
    label: str = ""
    confidence: float = 1.0

    @property
    def midpoint(self) -> float:
        return (self.start_seconds + self.end_seconds) / 2.0

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True, slots=True)
class Score:
    """Precision, recall and the counts behind them (§118).

    The counts are kept because the ratios hide the thing worth knowing. "0.5
    precision" is one hit and one miss, or fifty and fifty, and only one of
    those is worth acting on.
    """

    true_positives: int
    false_positives: int
    false_negatives: int
    #: Labels that were ignored because they carry `opinion` confidence and the
    #: caller asked for certainties only. Reported so a recall of 1.0 over two
    #: labels is visibly that.
    excluded: int = 0
    #: Predictions dropped for landing outside the annotated window.
    out_of_window: int = 0

    @property
    def predicted(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def actual(self) -> int:
        return self.true_positives + self.false_negatives

    @property
    def precision(self) -> float:
        """Of what it claimed, how much was real. 1.0 when it claimed nothing."""
        return self.true_positives / self.predicted if self.predicted else 1.0

    @property
    def recall(self) -> float:
        """Of what was there, how much it found. 1.0 when there was nothing."""
        return self.true_positives / self.actual if self.actual else 1.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    @property
    def false_positive_rate(self) -> float:
        """§118. Of what it claimed, how much was not there."""
        return self.false_positives / self.predicted if self.predicted else 0.0

    @property
    def false_negative_rate(self) -> float:
        """§118. Of what was there, how much it missed."""
        return self.false_negatives / self.actual if self.actual else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "false_negative_rate": round(self.false_negative_rate, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "excluded_opinions": self.excluded,
            "out_of_window": self.out_of_window,
        }


@dataclass(frozen=True, slots=True)
class Match:
    """One prediction paired with the label it found, for inspection.

    §118 asks for numbers; anyone acting on them needs the cases. A precision
    of 0.6 is a number, and the four predictions that missed are the work.
    """

    prediction: Prediction
    span: Span | None
    overlap_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Evaluation:
    """A full scoring of one recording (§118)."""

    events: Score
    moments: Score
    #: Highlights the system chose that a person marked *boring*. Not a
    #: false positive by the arithmetic above -- a boring stretch is not a
    #: missing highlight -- but the most actionable single number here: it is
    #: footage nobody wanted, chosen anyway.
    boring_selected: int = 0
    boring_seconds_selected: float = 0.0
    matches: tuple[Match, ...] = field(default_factory=tuple)
    misses: tuple[Span, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "events": self.events.as_dict(),
            "moments": self.moments.as_dict(),
            "boring_selected": self.boring_selected,
            "boring_seconds_selected": round(self.boring_seconds_selected, 1),
        }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def score_events(
    predictions: Sequence[Prediction],
    recording: AnnotatedRecording,
    *,
    tolerance_seconds: float = EVENT_TOLERANCE_SECONDS,
    certain_only: bool = False,
) -> tuple[Score, tuple[Match, ...], tuple[Span, ...]]:
    """Score detected events against the labelled ones (§118).

    An event matches when its midpoint lands within ``tolerance_seconds`` of a
    labelled span -- an event is an instant, and asking two instants to overlap
    is asking the wrong question.
    """
    labels = recording.of_kind(SpanKind.EVENT, certain_only=certain_only)
    excluded = len(recording.of_kind(SpanKind.EVENT)) - len(labels)
    return _match(
        predictions,
        labels,
        recording,
        excluded=excluded,
        distance=lambda prediction, span: _instant_distance(prediction, span, tolerance_seconds),
    )


def score_moments(
    predictions: Sequence[Prediction],
    recording: AnnotatedRecording,
    *,
    min_overlap: float = MOMENT_OVERLAP,
    certain_only: bool = False,
) -> tuple[Score, tuple[Match, ...], tuple[Span, ...]]:
    """Score selected moments against the labelled highlights (§118).

    A moment is a stretch, so this matches on overlap: at least ``min_overlap``
    of the *shorter* of the two. Shorter rather than either fixed side, because
    a 4-second label inside a 30-second clip and a 30-second label barely
    clipped by a 4-second selection are different failures, and only the first
    is a hit.
    """
    labels = recording.of_kind(SpanKind.HIGHLIGHT, certain_only=certain_only)
    excluded = len(recording.of_kind(SpanKind.HIGHLIGHT)) - len(labels)
    return _match(
        predictions,
        labels,
        recording,
        excluded=excluded,
        distance=lambda prediction, span: _overlap_distance(prediction, span, min_overlap),
    )


def boring_overlap(
    predictions: Sequence[Prediction], recording: AnnotatedRecording
) -> tuple[int, float]:
    """How much of what was selected a person had marked as dead time.

    Returns ``(count, seconds)``. This is the number to act on first: unlike a
    missed highlight, which may be taste, footage chosen out of a stretch
    somebody explicitly called boring is wrong by the annotator's own account.
    """
    boring = recording.of_kind(SpanKind.BORING)
    count = 0
    seconds = 0.0
    for prediction in predictions:
        if not recording.within_window(prediction.start_seconds, prediction.end_seconds):
            continue
        overlap = sum(
            span.overlaps(prediction.start_seconds, prediction.end_seconds) for span in boring
        )
        if overlap > 0:
            count += 1
            seconds += overlap
    return count, seconds


def evaluate(
    recording: AnnotatedRecording,
    *,
    events: Sequence[Prediction] = (),
    moments: Sequence[Prediction] = (),
    certain_only: bool = False,
) -> Evaluation:
    """Score one recording end to end (§118)."""
    event_score, event_matches, event_misses = score_events(
        events, recording, certain_only=certain_only
    )
    moment_score, moment_matches, moment_misses = score_moments(
        moments, recording, certain_only=certain_only
    )
    count, seconds = boring_overlap(moments, recording)
    return Evaluation(
        events=event_score,
        moments=moment_score,
        boring_selected=count,
        boring_seconds_selected=seconds,
        matches=event_matches + moment_matches,
        misses=event_misses + moment_misses,
    )


def duration_error(actual_seconds: float, target_seconds: float) -> dict[str, float]:
    """§118's target duration error, absolute and relative."""
    difference = actual_seconds - target_seconds
    return {
        "target_seconds": round(target_seconds, 1),
        "actual_seconds": round(actual_seconds, 1),
        "error_seconds": round(difference, 1),
        "error_ratio": round(difference / target_seconds, 4) if target_seconds else 0.0,
    }


def render_failure_rate(attempted: int, failed: int) -> dict[str, float]:
    """§118's render failure rate."""
    return {
        "attempted": attempted,
        "failed": failed,
        "rate": round(failed / attempted, 4) if attempted else 0.0,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _instant_distance(prediction: Prediction, span: Span, tolerance: float) -> float | None:
    """How far a prediction is from an instant, or ``None`` if it misses it.

    Both directions are checked, and the second was added because the first
    measurement got a real detection wrong. §27's correlation merges detectors
    that saw the same instant, so a death can arrive as a 26-second span whose
    *midpoint* sits 8 seconds from the moment itself -- reported as a miss when
    the system had found it and said so at 0.97 confidence.

    So: the prediction's midpoint near the label, **or** the label's midpoint
    inside the prediction. A prediction can still only claim one label, so a
    long span covering several is one hit and a false positive for the rest.
    """
    if span.start_seconds <= prediction.midpoint <= span.end_seconds:
        return 0.0
    if prediction.start_seconds <= span.midpoint <= prediction.end_seconds:
        return 0.0
    gap = min(
        abs(prediction.midpoint - span.start_seconds),
        abs(prediction.midpoint - span.end_seconds),
    )
    return gap if gap <= tolerance else None


def _overlap_distance(prediction: Prediction, span: Span, min_overlap: float) -> float | None:
    """Inverted overlap, so that "smaller is better" holds for both rules."""
    shared = span.overlaps(prediction.start_seconds, prediction.end_seconds)
    if shared <= 0:
        return None
    shorter = min(span.duration, max(prediction.duration, 1e-9))
    if shared / shorter < min_overlap:
        return None
    return -shared


def _match(
    predictions: Sequence[Prediction],
    labels: Sequence[Span],
    recording: AnnotatedRecording,
    *,
    excluded: int,
    distance: Any,
) -> tuple[Score, tuple[Match, ...], tuple[Span, ...]]:
    """Greedily pair predictions with labels, best pairing first.

    Greedy rather than optimal: the assignment problem has an exact solution
    and it would change the numbers by nothing a person could act on, while
    making the result harder to explain than the thing it measures.
    """
    inside: list[Prediction] = []
    out_of_window = 0
    for prediction in predictions:
        if recording.within_window(prediction.start_seconds, prediction.end_seconds):
            inside.append(prediction)
        else:
            out_of_window += 1

    candidates: list[tuple[float, int, int]] = []
    for prediction_index, prediction in enumerate(inside):
        for label_index, span in enumerate(labels):
            score = distance(prediction, span)
            if score is not None:
                candidates.append((score, prediction_index, label_index))
    candidates.sort()

    paired_predictions: dict[int, tuple[int, float]] = {}
    paired_labels: set[int] = set()
    for score, prediction_index, label_index in candidates:
        if prediction_index in paired_predictions or label_index in paired_labels:
            continue
        paired_predictions[prediction_index] = (label_index, score)
        paired_labels.add(label_index)

    matches = tuple(
        Match(
            prediction=inside[prediction_index],
            span=labels[label_index],
            overlap_seconds=labels[label_index].overlaps(
                inside[prediction_index].start_seconds, inside[prediction_index].end_seconds
            ),
        )
        for prediction_index, (label_index, _) in sorted(paired_predictions.items())
    )
    unmatched = tuple(
        Match(prediction=prediction, span=None)
        for index, prediction in enumerate(inside)
        if index not in paired_predictions
    )
    misses = tuple(span for index, span in enumerate(labels) if index not in paired_labels)

    score = Score(
        true_positives=len(paired_labels),
        false_positives=len(inside) - len(paired_predictions),
        false_negatives=len(misses),
        excluded=excluded,
        out_of_window=out_of_window,
    )
    return score, matches + unmatched, misses


__all__ = [
    "EVENT_TOLERANCE_SECONDS",
    "MOMENT_OVERLAP",
    "Evaluation",
    "Match",
    "Prediction",
    "Score",
    "boring_overlap",
    "duration_error",
    "evaluate",
    "render_failure_rate",
    "score_events",
    "score_moments",
]
