"""Evidence projection (Phase A).

Phase 0 set the constraint: *"No evidence table. The analysis tables are the
evidence; Phase A will project over them, not copy them."* So there is nothing
here about storage and everything about attribution, because attribution is
where the four hand-rolled versions of this went wrong.

The failure worth naming: an earlier gatherer read `media_id` off a stored
vision observation, which has never had one. Every lookup returned nothing,
every caller saw an empty result, and an empty result looks exactly like quiet
footage.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.core.models.enums import GameEventType
from backend.evidence import Span, Stores, project
from backend.gaming.correlation import GameEvent

pytestmark = pytest.mark.unit

MEDIA = "media-0000000000"
OTHER = "media-1111111111"


@dataclass(frozen=True)
class _Seen:
    timestamp: float
    description: str = ""
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Said:
    start: float
    text: str


def _event(start: float, kind: GameEventType = GameEventType.COMBAT) -> GameEvent:
    return GameEvent(
        event_type=kind,
        start_seconds=start,
        end_seconds=start + 2.0,
        confidence=0.8,
        importance=0.6,
        sources=("audio", "vision"),
    )


def _span(start: float = 0.0, end: float = 60.0, media: str = MEDIA) -> Span:
    return Span(media_id=media, start_seconds=start, end_seconds=end)


class TestAttribution:
    def test_only_the_recording_the_span_belongs_to_is_read(self) -> None:
        """Two recordings of one session both have a second 40.

        An observation attributed to the wrong one describes footage it never
        saw, which is why the stores are keyed and not concatenated.
        """
        evidence = project(
            _span(),
            Stores(
                seen={
                    MEDIA: [_Seen(10.0, "a menu", ("menu",))],
                    OTHER: [_Seen(10.0, "a boss fight", ("combat",))],
                }
            ),
        )

        assert evidence.labels == ("menu",)
        assert len(evidence.seen) == 1

    def test_a_recording_nothing_was_fetched_for_reads_as_empty(self) -> None:
        evidence = project(_span(media=OTHER), Stores(seen={MEDIA: [_Seen(10.0)]}))

        assert evidence.is_empty

    def test_records_outside_the_span_are_left_out(self) -> None:
        evidence = project(
            _span(10.0, 20.0),
            Stores(seen={MEDIA: [_Seen(5.0), _Seen(15.0), _Seen(25.0)]}),
        )

        assert [item.timestamp for item in evidence.seen] == [15.0]

    def test_each_store_is_read_by_its_own_time_field(self) -> None:
        # Vision calls it `timestamp`, transcript calls it `start`, events call
        # it `start_seconds`. Duck-typing across three names is how a store
        # whose shape changes starts matching nothing in silence.
        evidence = project(
            _span(),
            Stores(
                seen={MEDIA: [_Seen(10.0)]},
                said={MEDIA: [_Said(20.0, "watch this")]},
                events={MEDIA: [_event(30.0)]},
            ),
        )

        assert len(evidence.seen) == 1
        assert len(evidence.said) == 1
        assert len(evidence.events) == 1

    def test_everything_comes_back_in_time_order(self) -> None:
        evidence = project(_span(), Stores(seen={MEDIA: [_Seen(30.0), _Seen(10.0), _Seen(20.0)]}))

        assert [item.timestamp for item in evidence.seen] == [10.0, 20.0, 30.0]


class TestWhatItSays:
    def test_nothing_recorded_is_a_state_worth_naming(self) -> None:
        """ "Nothing happened" and "nobody looked" are different statements.

        Only the second is a reason to distrust everything else said about a
        stretch, and measured on a real edit one clip in eleven -- thirty
        seconds of a finished video -- came back with neither observations nor
        words nor events.
        """
        assert project(_span(), Stores()).is_empty
        assert not project(_span(), Stores(seen={MEDIA: [_Seen(10.0)]})).is_empty

    def test_labels_come_back_commonest_first(self) -> None:
        evidence = project(
            _span(),
            Stores(
                seen={
                    MEDIA: [
                        _Seen(10.0, labels=("driving",)),
                        _Seen(20.0, labels=("driving", "combat")),
                    ]
                }
            ),
        )

        assert evidence.labels == ("driving", "combat")

    def test_an_event_nobody_could_name_is_not_a_named_event(self) -> None:
        evidence = project(
            _span(),
            Stores(
                events={
                    MEDIA: [
                        _event(10.0, GameEventType.UNEXPECTED_EVENT),
                        _event(20.0, GameEventType.DEFEAT),
                    ]
                }
            ),
        )

        assert len(evidence.events) == 2
        assert [e.event_type for e in evidence.named_events] == [GameEventType.DEFEAT]

    def test_the_sources_answer_the_provenance_question(self) -> None:
        evidence = project(_span(), Stores(events={MEDIA: [_event(10.0)]}))

        assert evidence.sources == ("audio", "vision")

    def test_the_words_are_joined_and_can_be_cut(self) -> None:
        evidence = project(
            _span(), Stores(said={MEDIA: [_Said(10.0, "watch"), _Said(20.0, "this")]})
        )

        assert evidence.words() == "watch this"
        assert evidence.words(limit=6).endswith("...")

    def test_a_span_with_no_speech_has_no_words(self) -> None:
        assert project(_span(), Stores()).words() == ""


class TestTheSpan:
    def test_widening_never_reaches_before_the_recording(self) -> None:
        widened = _span(5.0, 20.0).widened(30.0)

        assert widened.start_seconds == 0.0
        assert widened.end_seconds == 50.0

    def test_widening_keeps_the_recording_it_belongs_to(self) -> None:
        assert _span(5.0, 20.0).widened(1.0).media_id == MEDIA

    def test_the_end_is_exclusive(self) -> None:
        # A record exactly on the boundary belongs to the next span, not both.
        span = _span(10.0, 20.0)
        assert span.contains(10.0)
        assert not span.contains(20.0)

    def test_a_missing_time_is_never_inside(self) -> None:
        assert not _span().contains(None)
