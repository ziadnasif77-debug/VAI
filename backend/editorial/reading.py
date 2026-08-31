"""The whole editorial reading of one project, assembled once (V2-P11).

Three layers exist separately and are useless separately:

* :mod:`backend.evidence.projection` reads the analysis stores for a span;
* :mod:`backend.editorial.evidence` reads a moment as a shot;
* :mod:`backend.editorial.situations` reads episodes as a situation.

Something has to fetch the stores once, per recording, and hand the same
fetched data to every reading. Doing it per moment would issue one query per
moment per store -- ninety moments and six stores is five hundred and forty
round trips for data that does not change between them.

So: one gather, then every reading. That is all this module is, and it is the
seam the story stage calls.

**No table, no migration, no cache identity.** The readings are derived from
stores the analysis stages already filled, so a style change, a duration change
or a new selection strategy cannot invalidate them -- there is nothing to
invalidate. §11 stays true without anything being written to keep it true.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.core.logging import LogChannel, get_logger
from backend.editorial import evidence as shots
from backend.editorial import semantics as meaning
from backend.editorial import situations as arcs
from backend.evidence import Span, Stores, project

logger = get_logger("editorial.reading", LogChannel.PIPELINE)


@dataclass(frozen=True, slots=True)
class EditorialReading:
    """What one project's footage amounts to, editorially."""

    #: ``moment id -> the shot it would make``.
    shots: dict[str, shots.EditorialEvidence] = field(default_factory=dict)
    #: ``moment id -> what that shot is for``. Read here rather than by the
    #: caller because the projections it needs are already fetched here, and
    #: fetching them twice would be the per-moment round trip this module
    #: exists to avoid.
    semantics: dict[str, meaning.ShotSemantics] = field(default_factory=dict)
    #: Every situation read, in time order, across every recording.
    situations: tuple[arcs.Situation, ...] = ()
    #: Recordings whose lanes could not be read, so their shots carry no state.
    unread: tuple[str, ...] = ()

    @property
    def compound(self) -> int:
        """Situations made of more than one episode -- where the decisions are."""
        return sum(1 for situation in self.situations if situation.is_compound)

    def shot(self, moment: Any) -> shots.EditorialEvidence | None:
        return self.shots.get(str(getattr(moment, "id", "") or ""))

    def semantics_of(self, moment: Any) -> meaning.ShotSemantics | None:
        """What this shot is for, or None when it was never read.

        Not named `meaning`: that is the module, and a method shadowing it
        inside the class body is a puzzle for whoever reads this next.
        """
        return self.semantics.get(str(getattr(moment, "id", "") or ""))

    def summary(self) -> dict[str, Any]:
        return {
            "shots": len(self.shots),
            "dead_shots": sum(
                1
                for found in self.semantics.values()
                if found.purpose is meaning.ShotPurpose.DEAD
            ),
            "situations": len(self.situations),
            "compound_situations": self.compound,
            "with_arc": sum(1 for s in self.situations if s.arc),
            "observed": sum(1 for s in self.shots.values() if s.observed),
            "unread_recordings": list(self.unread),
        }


def read(
    database: Any,
    config: Any,
    *,
    moments: Sequence[Any],
    media_ids: Sequence[str],
    durations: dict[str, float] | None = None,
) -> EditorialReading:
    """Read every moment as a shot and every episode run as a situation.

    Failure here is never fatal. A recording whose lanes will not load produces
    shots with empty states that say ``unknown``, and the edit is still made --
    the same §95 rule the Director and the Critic follow, applied to a reading.
    """
    durations = durations or {}
    stores = _gather(database, media_ids)
    readers = _readers(database, config, media_ids, durations)
    unread = tuple(sorted(set(media_ids) - set(readers)))

    situations: list[arcs.Situation] = []
    for media_id in media_ids:
        situations.extend(
            arcs.read(
                _episodes(database, media_id),
                media_id=media_id,
                reader=readers.get(media_id),
            )
        )
    situations.sort(key=lambda item: (item.media_id, item.start_seconds))

    read_shots: dict[str, shots.EditorialEvidence] = {}
    read_meanings: dict[str, meaning.ShotSemantics] = {}
    for moment in moments:
        moment_id = str(getattr(moment, "id", "") or "")
        if not moment_id:
            continue
        situation = arcs.situation_of(situations, moment)
        evidence = shots.read(
            moment,
            stores=stores,
            reader=readers.get(str(getattr(moment, "media_id", ""))),
            phases=_phases(moment),
            situation_id=situation.id if situation else "",
        )
        read_shots[moment_id] = evidence
        read_meanings[moment_id] = meaning.read(
            evidence,
            inside=_inside(evidence, stores),
            after=_after(evidence, stores),
        )

    reading = EditorialReading(
        shots=read_shots,
        semantics=read_meanings,
        situations=tuple(situations),
        unread=unread,
    )
    logger.info("The footage was read editorially", extra=reading.summary())
    return reading


def _inside(evidence: shots.EditorialEvidence, stores: Stores) -> Any:
    """What was recorded inside the shot's own span."""
    return project(
        Span(
            media_id=evidence.media_id,
            start_seconds=evidence.source_start,
            end_seconds=evidence.source_end,
        ),
        stores,
    )


def _after(evidence: shots.EditorialEvidence, stores: Stores) -> Any:
    """What was recorded in the stretch following it.

    The only way to see anticipation and reaction. Both are claims about what
    happens *next*, and a reading confined to the shot's own span cannot make
    either -- which is why the pipeline has never made them.
    """
    return project(
        Span(
            media_id=evidence.media_id,
            start_seconds=evidence.source_end,
            end_seconds=evidence.source_end + shots.CONTEXT_SECONDS,
        ),
        stores,
    )


def _gather(database: Any, media_ids: Sequence[str]) -> Stores:
    """Every analysis store, once per recording.

    Each fetch is wrapped: a store that will not answer costs its own kind of
    evidence and nothing else. The projection is explicitly built to take what
    the caller fetched per recording, because the stored rows do not carry
    their own media id and a flat list would make it guess.
    """
    from backend.database.repositories.audio_events import AudioEventRepository
    from backend.database.repositories.gaming import GameEventRepository, OcrRepository
    from backend.database.repositories.scenes import SceneRepository
    from backend.database.repositories.transcript import TranscriptRepository
    from backend.database.repositories.vision import VisionRepository

    def per_media(build, name: str) -> dict[str, list[Any]]:
        found: dict[str, list[Any]] = {}
        for media_id in media_ids:
            try:
                found[media_id] = list(build(media_id))
            except Exception:
                logger.info(
                    "A store could not be read for this recording",
                    extra={"store": name, "media_id": media_id},
                )
                found[media_id] = []
        return found

    return Stores(
        seen=per_media(VisionRepository(database).list_for_media, "vision"),
        said=per_media(TranscriptRepository(database).list_for_media, "transcript"),
        events=per_media(GameEventRepository(database).list_for_media, "events"),
        heard=per_media(AudioEventRepository(database).list_for_media, "audio_events"),
        cuts=per_media(SceneRepository(database).list_for_media, "scenes"),
        read=per_media(OcrRepository(database).list_for_media, "ocr"),
    )


def _readers(
    database: Any, config: Any, media_ids: Sequence[str], durations: dict[str, float]
) -> dict[str, Any]:
    """The semantic lanes per recording, for those that have them."""
    from backend.semantic.timeline import load_timeline

    found: dict[str, Any] = {}
    for media_id in media_ids:
        length = durations.get(media_id)
        if not length:
            continue
        try:
            found[media_id] = load_timeline(
                database, media_id, duration_seconds=float(length), config=config
            )
        except Exception:
            logger.info(
                "No semantic lanes for this recording; its shots carry no state",
                extra={"media_id": media_id},
            )
    return found


def _episodes(database: Any, media_id: str) -> Any:
    """The episode reader's output for one recording, or an empty reading."""
    from backend.database.repositories.gaming import GameEventRepository
    from backend.gaming.episodes import read as read_episodes

    try:
        events = GameEventRepository(database).list_for_media(media_id)
        return read_episodes(events, media_id=media_id)
    except Exception:
        logger.info("No episodes for this recording", extra={"media_id": media_id})
        from backend.gaming.episodes import Reading

        return Reading()


def _phases(moment: Any) -> Any:
    """V2-P2's phase reading, if the moment carries one.

    Stored in the moment's metadata by the moments stage rather than
    recomputed: the classifier already ran, and running it again here would be
    a second opinion on a question that has an answer.
    """
    stored = (getattr(moment, "metadata", None) or {}).get("phases")
    if not isinstance(stored, dict):
        return None
    return type(
        "Phases",
        (),
        {
            "phase": stored.get("phase", ""),
            "confidence": stored.get("confidence", 0.0),
        },
    )()


__all__ = ["EditorialReading", "read"]
