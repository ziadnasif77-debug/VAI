"""A deterministic vision provider for tests (SPEC §13, §15, §113-§119).

A 7B VLM is a 6 GB download and seconds of GPU time per frame. Almost nothing
the pipeline does with an observation needs a *real* one: the cascade's frame
budget, persistence, timeline placement and the §127 re-edit path all care that
observations exist at the right timestamps, not what they say.

So this returns descriptions derived from the frame's own path and timestamp —
the same frame always yields the same observation, which is what makes an
assertion about a timestamp meaningful.

It also counts calls. That is the point of the acceptance test: the number of
frames that reach a vision provider is the number this one was handed, and no
amount of reading the cascade's code proves it as directly as counting.

A test double, never a fallback. §95 degrades a missing vision model to OCR,
audio, scene detection and the game profile — not to invented descriptions of
frames nobody looked at.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from ai.providers.base import ModelInfo, VisionObservation

FAKE_VERSION: Final[str] = "fake-vision-1"

#: A small closed vocabulary. Recognisable in a failing test's output, and
#: plausible enough that a description reads like one.
#:
#: Every entry is gameplay. The vocabulary used to carry a loading screen and
#: an open menu, which made one frame in four non-gameplay by the hash of its
#: filename -- a recording no game produces. That was harmless while labels
#: were decoration; once the exclusion layer (V2-P0.2) started refusing
#: footage on the strength of a ``loading`` or ``menu`` label, every pipeline
#: fixture lost its moments to menus nobody drew. A test that wants a menu
#: builds the observation and says so.
_SUBJECTS: Final[tuple[str, ...]] = (
    "a firefight in an open courtyard",
    "the player reloading behind cover",
    "a scoreboard overlay",
    "the player climbing onto a rooftop",
    "the player sprinting down a corridor",
    "an explosion filling the frame",
    "the player taking aim from a window",
    "a vehicle crossing rough ground",
)
_LABELS: Final[tuple[tuple[str, ...], ...]] = (
    ("combat",),
    ("combat", "low_health"),
    ("scoreboard",),
    ("traversal",),
    ("traversal",),
    ("combat", "explosion"),
    ("combat",),
    ("driving",),
)


class FakeVisionProvider:
    """Produces deterministic observations without loading a model."""

    def __init__(self, *, available: bool = True, fail_after: int | None = None) -> None:
        """
        Args:
            available: report unavailable, to exercise the §95 fallback path.
            fail_after: raise once this many frames have been described, to
                exercise the failure path of a long stage.
        """
        self._available = available
        self._fail_after = fail_after
        self.load_count = 0
        self.unload_count = 0
        #: Every frame this provider was asked about, in order. The acceptance
        #: test counts these against ``max_frames_per_source_hour``.
        self.described_frames: list[tuple[Path, float]] = []
        self.batch_sizes: list[int] = []

    # -- provider protocol ----------------------------------------------

    def info(self) -> ModelInfo:
        return ModelInfo(
            name="fake-vision",
            version=FAKE_VERSION,
            provider="fake",
            device="cpu",
            estimated_vram_mb=0,
        )

    def is_available(self) -> bool:
        return self._available

    def load(self) -> None:
        self.load_count += 1

    def unload(self) -> None:
        self.unload_count += 1

    def locate_subject(self, frame_path):
        """Scripted subject box, or ``None`` like a model with no answer."""
        self.located = getattr(self, "located", [])
        self.located.append(frame_path)
        return getattr(self, "subject_box", None)

    def describe(
        self,
        frame_paths: tuple[Path, ...],
        timestamps: tuple[float, ...],
        *,
        prompt_id: str = "vision.frame_description",
    ) -> tuple[VisionObservation, ...]:
        from backend.core.errors import ErrorCode, ModelError

        if self._fail_after is not None and len(self.described_frames) >= self._fail_after:
            raise ModelError(
                "Fake vision failure.",
                code=ErrorCode.VISION_FAILED,
                details={"described": len(self.described_frames)},
            )

        self.batch_sizes.append(len(frame_paths))
        observations: list[VisionObservation] = []
        for path, timestamp in zip(frame_paths, timestamps, strict=True):
            self.described_frames.append((Path(path), timestamp))
            index = _seed(Path(path).name) % len(_SUBJECTS)
            observations.append(
                VisionObservation(
                    timestamp=timestamp,
                    description=f"The frame shows {_SUBJECTS[index]}.",
                    labels=_LABELS[index],
                    confidence=0.6 + (index % 4) * 0.1,
                    hud={"score": str(100 + index)} if index % 3 == 0 else {},
                )
            )
        return tuple(observations)


def _seed(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:4], "big")


__all__ = ["FAKE_VERSION", "FakeVisionProvider"]
