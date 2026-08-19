"""Episodes and links (Phase B).

Phase 0 deferred this until the events had names, and the numbers that decided
its shape came from three real recordings rather than from a design:

* consecutive named events sit a median 12 seconds apart;
* the commonest neighbour is the same type again — `low_health → low_health`
  nineteen times, `combat → combat` eighteen, `collision → collision` eighteen;
* and the gap distribution for same-type pairs is **indistinguishable** from
  different-type pairs (median 12.0 against 11.4, first quartile 8.0 for both).

That last one is why these tests are mostly about type identity. Time cannot
tell "this fight is still going" from "something else happened nearby", so an
episode is a run of one *type* and the window only stops it running away.
"""

from __future__ import annotations

import pytest

from backend.core.models.enums import GameEventType
from backend.gaming.correlation import GameEvent
from backend.gaming.episodes import DEFAULT_GAP_SECONDS, Episode, read

pytestmark = pytest.mark.unit

MEDIA = "media-0000000000"


def _event(
    start: float,
    *,
    seconds: float = 2.0,
    kind: GameEventType = GameEventType.COMBAT,
    confidence: float = 0.7,
    sources: tuple[str, ...] = ("audio", "vision"),
) -> GameEvent:
    return GameEvent(
        event_type=kind,
        start_seconds=start,
        end_seconds=start + seconds,
        confidence=confidence,
        importance=0.6,
        sources=sources,
    )


def _read(*events: GameEvent, **kwargs):
    return read(events, media_id=MEDIA, **kwargs)


class TestWhatBecomesOneSituation:
    def test_a_run_of_one_type_is_one_episode(self) -> None:
        reading = _read(_event(10.0), _event(25.0), _event(40.0))

        assert len(reading.episodes) == 1
        episode = reading.episodes[0]
        assert episode.parts == 3
        assert episode.is_merged
        # The span covers the footage its parts cover, first start to last end.
        assert episode.start_seconds == pytest.approx(10.0)
        assert episode.end_seconds == pytest.approx(42.0)

    def test_a_different_type_ends_the_run(self) -> None:
        # Two true things about one situation, and which was which is the part
        # worth keeping.
        reading = _read(_event(10.0), _event(25.0), _event(30.0, kind=GameEventType.LOW_HEALTH))

        assert [e.event_type for e in reading.episodes] == [
            GameEventType.COMBAT,
            GameEventType.LOW_HEALTH,
        ]
        assert reading.episodes[0].parts == 2

    def test_a_long_enough_pause_ends_the_run(self) -> None:
        reading = _read(_event(10.0), _event(10.0 + DEFAULT_GAP_SECONDS + 30.0))

        assert len(reading.episodes) == 2
        assert all(not episode.is_merged for episode in reading.episodes)

    def test_the_pause_is_measured_from_the_end_not_the_start(self) -> None:
        """A forty-second fight and five seconds later more of it is one fight.

        Measured start to start it would read as a forty-five second pause and
        become two situations, which is the arithmetic error this rule exists
        to not make.
        """
        reading = _read(_event(10.0, seconds=40.0), _event(55.0))

        assert len(reading.episodes) == 1
        assert reading.episodes[0].parts == 2

    def test_the_window_is_configurable_because_it_was_measured(self) -> None:
        # 23% of named events merge at ten seconds, 38% at twenty, 44% at
        # thirty. The default sits at the knee; a caller may disagree.
        events = (_event(10.0), _event(35.0))
        assert len(_read(*events, gap_seconds=10.0).episodes) == 2
        assert len(_read(*events, gap_seconds=30.0).episodes) == 1


class TestWhatIsLeftOut:
    def test_an_event_nobody_could_name_is_not_a_situation(self) -> None:
        # A run of things nobody identified is not a situation, it is a gap.
        reading = _read(
            _event(10.0, kind=GameEventType.UNEXPECTED_EVENT),
            _event(20.0, kind=GameEventType.UNEXPECTED_EVENT),
        )

        assert reading.episodes == ()
        assert reading.links == ()

    def test_a_recording_with_nothing_named_reads_as_nothing(self) -> None:
        assert read((), media_id=MEDIA).episodes == ()

    def test_the_member_events_are_carried_whole(self) -> None:
        # An episode is a reading of the correlator's output, never a
        # replacement for it: nothing downstream loses access to the parts.
        first, second = _event(10.0), _event(25.0)
        episode = _read(first, second).episodes[0]

        assert episode.events == (first, second)


class TestWhatOneSituationIsWorth:
    def test_confidence_is_the_best_part_not_the_average(self) -> None:
        # Three sightings of one fight are not less certain than one, and an
        # average would punish an episode for having been seen twice.
        reading = _read(_event(10.0, confidence=0.9), _event(25.0, confidence=0.5))

        assert reading.episodes[0].confidence == pytest.approx(0.9)

    def test_the_peak_is_where_the_situation_was_strongest(self) -> None:
        reading = _read(_event(10.0, confidence=0.5), _event(25.0, confidence=0.95))

        assert reading.episodes[0].peak_seconds == pytest.approx(25.0)

    def test_the_sources_are_everything_that_contributed(self) -> None:
        reading = _read(_event(10.0, sources=("audio",)), _event(25.0, sources=("ocr", "vision")))

        assert reading.episodes[0].sources == ("audio", "ocr", "vision")


class TestWhatIsRelatedWithoutBeingMerged:
    def test_two_types_close_together_are_linked(self) -> None:
        # The commonest real pair, on a real recording: the player is hurt in
        # a fight. Seven of one direction and six of the other on one project.
        reading = _read(_event(10.0), _event(20.0, kind=GameEventType.LOW_HEALTH))

        assert len(reading.links) == 1
        link = reading.links[0]
        assert link.earlier.event_type is GameEventType.COMBAT
        assert link.later.event_type is GameEventType.LOW_HEALTH
        assert link.gap_seconds == pytest.approx(8.0)

    def test_two_types_far_apart_are_not(self) -> None:
        reading = _read(_event(10.0), _event(300.0, kind=GameEventType.LOW_HEALTH))
        assert reading.links == ()

    def test_only_neighbours_are_related(self) -> None:
        """Not every pair inside the window.

        A busy minute related pairwise is a complete graph, which says nothing
        that "it was busy" does not.
        """
        reading = _read(
            _event(10.0),
            _event(20.0, kind=GameEventType.LOW_HEALTH),
            _event(30.0, kind=GameEventType.COLLISION),
        )

        assert len(reading.links) == 2
        assert all(link.earlier.event_type is not link.later.event_type for link in reading.links)

    def test_an_overlap_is_reported_as_one(self) -> None:
        reading = _read(_event(10.0, seconds=30.0), _event(20.0, kind=GameEventType.LOW_HEALTH))

        assert reading.links[0].overlapping


class TestTheReadingAsAWhole:
    def test_it_counts_what_it_absorbed(self) -> None:
        # Measured on three real recordings: 255 named events read as 159
        # episodes, a consistent 37-38% absorbed on every one of them.
        reading = _read(_event(10.0), _event(25.0), _event(40.0), _event(200.0))

        assert len(reading.episodes) == 2
        assert reading.merged_events == 2
        assert reading.summary()["types"] == ["combat"]

    def test_an_episode_of_one_is_still_an_episode(self) -> None:
        reading = _read(_event(10.0))

        assert len(reading.episodes) == 1
        assert not reading.episodes[0].is_merged
        assert reading.merged_events == 0

    def test_events_out_of_order_are_read_in_order(self) -> None:
        # The repository returns them sorted; a caller assembling from several
        # sources may not.
        reading = _read(_event(40.0), _event(10.0), _event(25.0))

        assert len(reading.episodes) == 1
        assert reading.episodes[0].start_seconds == pytest.approx(10.0)


def test_an_episode_reports_itself_without_its_parts() -> None:
    # The summary goes into logs and job results, where the member events
    # would be noise.
    episode = Episode(
        event_type=GameEventType.COMBAT,
        media_id=MEDIA,
        start_seconds=10.0,
        end_seconds=52.0,
        events=(_event(10.0), _event(25.0)),
    )

    assert episode.summary() == {
        "type": "combat",
        "start": 10.0,
        "seconds": 42.0,
        "parts": 2,
        "confidence": 0.7,
        "sources": ["audio", "vision"],
    }
