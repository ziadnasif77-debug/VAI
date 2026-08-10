"""Transcript persistence (SPEC sections 14, 45).

One row per utterance, with its word timings in a JSON column. Words live with
their segment rather than in a table of their own because nothing ever queries
a word without its segment, and a two-hour recording holds tens of thousands of
them -- a row each would dominate the database for no gain.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence

from ai.providers.base import TranscriptSegment, TranscriptWord
from backend.core.ids import new_id
from backend.database.connection import Database, dumps, loads

_COLUMNS = (
    "id, project_id, media_id, segment_index, start_seconds, end_seconds, text, "
    "language, speaker, confidence, words"
)


class TranscriptRepository:
    """CRUD for the ``transcript_segments`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_media(
        self, project_id: str, media_id: str, segments: Iterable[TranscriptSegment]
    ) -> int:
        """Replace a file's whole transcript.

        Wholesale rather than incremental: re-running TRANSCRIPT means the
        model, its version or the audio changed, and merging a new transcript
        into an old one would leave utterances from a model nobody is using any
        more (§49).
        """
        rows = [
            {
                "id": new_id("transcript_segment"),
                "project_id": project_id,
                "media_id": media_id,
                "segment_index": index,
                "start_seconds": segment.start,
                "end_seconds": max(segment.end, segment.start),
                "text": segment.text,
                "language": segment.language,
                "speaker": segment.speaker,
                "confidence": segment.confidence,
                "words": dumps(
                    [
                        {
                            "word": word.word,
                            "start": word.start,
                            "end": word.end,
                            "confidence": word.confidence,
                        }
                        for word in segment.words
                    ]
                ),
            }
            for index, segment in enumerate(segments)
        ]
        self._db.execute("DELETE FROM transcript_segments WHERE media_id = ?", (media_id,))
        if rows:
            self._db.executemany(
                f"INSERT INTO transcript_segments ({_COLUMNS}) VALUES ("
                ":id, :project_id, :media_id, :segment_index, :start_seconds, :end_seconds, "
                ":text, :language, :speaker, :confidence, :words)",
                rows,
            )
        return len(rows)

    def list_for_media(
        self,
        media_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
    ) -> list[TranscriptSegment]:
        """Utterances in chronological order, optionally within a time span."""
        sql = f"SELECT {_COLUMNS} FROM transcript_segments WHERE media_id = ?"
        parameters: list[object] = [media_id]
        if start is not None:
            sql += " AND end_seconds >= ?"
            parameters.append(start)
        if end is not None:
            sql += " AND start_seconds <= ?"
            parameters.append(end)
        sql += " ORDER BY segment_index ASC"
        return [_from_row(row) for row in self._db.fetch_all(sql, parameters)]

    def list_for_project(self, project_id: str) -> list[TranscriptSegment]:
        return [
            _from_row(row)
            for row in self._db.fetch_all(
                f"SELECT {_COLUMNS} FROM transcript_segments WHERE project_id = ? "
                "ORDER BY media_id ASC, segment_index ASC",
                (project_id,),
            )
        ]

    def count_for_media(self, media_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS total FROM transcript_segments WHERE media_id = ?",
            (media_id,),
        )
        return int(row["total"]) if row is not None else 0

    def spoken_seconds(self, media_id: str) -> float:
        """Total speech duration, for the §30 dead-time and §37 pacing passes."""
        row = self._db.fetch_one(
            "SELECT COALESCE(SUM(end_seconds - start_seconds), 0.0) AS total "
            "FROM transcript_segments WHERE media_id = ?",
            (media_id,),
        )
        return float(row["total"]) if row is not None else 0.0

    def search(self, project_id: str, term: str, *, limit: int = 50) -> list[TranscriptSegment]:
        """Find utterances containing ``term``.

        Backs the §17-addendum Q&A path: "what did I say before the boss
        fight" is answered from stored data, with the timestamp as its
        citation.
        """
        return [
            _from_row(row)
            for row in self._db.fetch_all(
                f"SELECT {_COLUMNS} FROM transcript_segments "
                "WHERE project_id = ? AND text LIKE ? ESCAPE '\\' "
                "ORDER BY start_seconds ASC LIMIT ?",
                (project_id, f"%{_escape_like(term)}%", limit),
            )
        ]

    def delete_for_media(self, media_id: str) -> int:
        return self._db.execute(
            "DELETE FROM transcript_segments WHERE media_id = ?", (media_id,)
        ).rowcount


def _from_row(row: sqlite3.Row) -> TranscriptSegment:
    words: Sequence[dict] = loads(row["words"]) or []
    return TranscriptSegment(
        start=row["start_seconds"],
        end=row["end_seconds"],
        text=row["text"],
        language=row["language"],
        speaker=row["speaker"],
        confidence=row["confidence"],
        words=tuple(
            TranscriptWord(
                word=str(item.get("word", "")),
                start=float(item.get("start", 0.0)),
                end=float(item.get("end", 0.0)),
                confidence=item.get("confidence"),
            )
            for item in words
            if isinstance(item, dict)
        ),
    )


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search for "100%" is not a match-everything."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["TranscriptRepository"]
