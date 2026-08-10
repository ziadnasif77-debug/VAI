"""A deterministic speech provider for tests (SPEC sections 13, 113-119).

Whisper large-v3 is a 3 GB download and minutes of GPU time per run. Almost
nothing the pipeline does with a transcript needs a *real* one: chunk offsets,
timeline alignment, persistence, the §127 re-edit path and the moment
detector's use of speech all care about the shape of the result, not its
meaning.

So this provider produces transcripts with the right shape, derived from the
audio file's actual duration and seeded by its path -- the same file always
yields the same transcript, which is what makes an assertion about a timestamp
meaningful.

It is a test double and never a fallback. A machine without Whisper degrades
through the §95 chain with no transcript at all; quietly substituting invented
words would put fabricated captions in a finished video.
"""

from __future__ import annotations

import contextlib
import hashlib
import wave
from pathlib import Path
from typing import Final

from ai.providers.base import ModelInfo, TranscriptSegment, TranscriptWord

#: A small closed vocabulary of gameplay callouts. Recognisable in a failing
#: test's output, and short enough to keep word timings easy to read.
_VOCABULARY: Final[tuple[str, ...]] = (
    "okay", "wait", "behind", "reloading", "got", "him", "no", "way",
    "push", "left", "right", "cover", "one", "shot", "nice", "run",
)

FAKE_VERSION: Final[str] = "fake-speech-1"


class FakeSpeechProvider:
    """Produces deterministic transcripts without loading a model."""

    def __init__(
        self,
        *,
        segment_seconds: float = 3.0,
        gap_seconds: float = 0.5,
        words_per_segment: int = 4,
        language: str = "en",
        silent: bool = False,
        available: bool = True,
    ) -> None:
        """
        Args:
            segment_seconds: length of each produced utterance.
            gap_seconds: silence between utterances.
            words_per_segment: how many words each utterance carries.
            silent: produce nothing, to exercise the no-speech path.
            available: report unavailable, to exercise the §95 fallback path.
        """
        self._segment_seconds = segment_seconds
        self._gap_seconds = gap_seconds
        self._words_per_segment = max(int(words_per_segment), 1)
        self._language = language
        self._silent = silent
        self._available = available
        self.load_count = 0
        self.unload_count = 0
        self.transcribe_calls: list[tuple[Path, float]] = []

    # -- provider protocol ----------------------------------------------

    def info(self) -> ModelInfo:
        return ModelInfo(
            name="fake-speech",
            version=FAKE_VERSION,
            provider="fake",
            device="cpu",
            estimated_vram_mb=0,
        )

    def is_available(self) -> bool:
        return self._available

    def load(self) -> None:
        """Count loads, so a test can assert the model is loaded once per stage.

        Loading Whisper per chunk instead of per stage costs tens of seconds
        each time on a recording with dozens of chunks, and that regression is
        invisible without an assertion.
        """
        self.load_count += 1

    def unload(self) -> None:
        self.unload_count += 1

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        start_offset: float = 0.0,
    ) -> tuple[TranscriptSegment, ...]:
        source = Path(audio_path)
        self.transcribe_calls.append((source, start_offset))
        if self._silent:
            return ()

        duration = _wav_duration(source)
        if duration <= 0:
            return ()

        seed = int.from_bytes(
            hashlib.sha256(source.name.encode("utf-8")).digest()[:4], "big"
        )
        stride = self._segment_seconds + self._gap_seconds
        segments: list[TranscriptSegment] = []
        index = 0
        cursor = 0.0

        while cursor + self._segment_seconds <= duration:
            end = cursor + self._segment_seconds
            segments.append(
                TranscriptSegment(
                    start=cursor + start_offset,
                    end=end + start_offset,
                    text=" ".join(
                        _word(seed, index, position)
                        for position in range(self._words_per_segment)
                    ),
                    language=language or self._language,
                    confidence=0.9,
                    words=self._words(seed, index, cursor + start_offset, end + start_offset),
                )
            )
            index += 1
            cursor += stride
        return tuple(segments)

    # -- internals ------------------------------------------------------

    def _words(
        self, seed: int, index: int, start: float, end: float
    ) -> tuple[TranscriptWord, ...]:
        """Lay words evenly across the segment, inside its bounds.

        Word timings that escape their segment are a real failure mode for
        caption timing (§71), so the double never produces them.
        """
        count = self._words_per_segment
        span = (end - start) / count
        return tuple(
            TranscriptWord(
                word=_word(seed, index, position),
                start=start + position * span,
                end=start + (position + 1) * span,
                confidence=0.9,
            )
            for position in range(count)
        )


def _word(seed: int, segment_index: int, position: int) -> str:
    return _VOCABULARY[(seed + segment_index * 7 + position * 3) % len(_VOCABULARY)]


def _wav_duration(path: Path) -> float:
    """Duration of a WAV file, or ``0.0`` if it cannot be read."""
    with (
        contextlib.suppress(wave.Error, OSError, EOFError),
        wave.open(str(path), "rb") as stream,
    ):
        rate = stream.getframerate()
        if rate > 0:
            return stream.getnframes() / rate
    return 0.0


__all__ = ["FAKE_VERSION", "FakeSpeechProvider"]
