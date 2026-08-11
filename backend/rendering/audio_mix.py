"""The final audio mix (SPEC §72, §73, §74).

§72 fixes the priority — **Speech > Important Game Audio > Music** — and §74
says how it is maintained: music drops when the player speaks and when
important game audio occurs, and returns otherwise.

The interesting decision is *how* the ducking is driven.

The obvious tool is a sidechain compressor, which watches one signal's level
and turns another down. It is the wrong tool here, and for the reason §64
gives about renderers: it would put the decision inside FFmpeg, where a level
threshold has to guess what a speech detector already knows. Worse, on a
recording whose microphone was never on a separate track there is no signal to
watch at all — which describes every recording this project has been run
against.

So the envelope is computed here, in Python, from spans the analysis already
established: transcript segments for speech, game events for the moments that
matter. Attack, release and hold are applied as explicit ramps, so the curve is
exact, click-free by construction, and unit-testable without decoding
anything. FFmpeg's job is one multiplication (`amultiply`).

That also means the mix degrades honestly. No transcript means no speech spans
means no speech ducking, and the result says so rather than silently mixing
music over dialogue.
"""

from __future__ import annotations

import wave
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np

from backend.config.schema import AudioConfig
from backend.core.logging import LogChannel, get_logger

logger = get_logger("rendering.audio_mix", LogChannel.RENDERING)

#: Sample rate of the generated envelopes. They multiply a full-rate signal, so
#: they must be written at the mix rate; 48 kHz is what §75's preset asks for.
ENVELOPE_SAMPLE_RATE: Final[int] = 48000

#: Audio formats every input is forced into before mixing. ``amultiply`` needs
#: both of its inputs to agree, and so does ``amix``; converting once at the
#: top is cheaper than diagnosing a silent channel-layout mismatch.
MIX_FORMAT: Final[str] = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

#: Music files the local directory may offer (§73). Nothing is ever downloaded.
MUSIC_SUFFIXES: Final[frozenset[str]] = frozenset({".mp3", ".wav", ".flac", ".m4a", ".ogg"})


@dataclass(frozen=True, slots=True)
class DuckSpan:
    """A stretch of the finished video during which something must give way."""

    start: float
    end: float
    #: How far down, in dB. Negative.
    depth_db: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class MixInput:
    """One audio file entering the mix, with the input options it needs.

    Options rather than a bare path because looping is an *input* option in
    FFmpeg (`-stream_loop`), not a filter. Expressing it as `apad` instead --
    which was the first attempt -- pads with silence rather than repeating, so
    a two-minute bed under a twenty-minute video gave two minutes of music and
    eighteen of nothing.
    """

    path: Path
    role: str
    options: tuple[str, ...] = ()

    @property
    def is_music(self) -> bool:
        return self.role == "music"

    def argv(self) -> list[str]:
        return [*self.options, "-i", str(self.path)]


@dataclass(frozen=True, slots=True)
class MixPlan:
    """Everything needed to run the mix: inputs, in order, and the graph."""

    inputs: tuple[MixInput, ...]
    filter_complex: str
    output_label: str
    notes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def input_arguments(self) -> list[str]:
        """The FFmpeg input arguments, in the order the graph's indices assume."""
        return [part for item in self.inputs for part in item.argv()]

    def summary(self) -> dict[str, Any]:
        return {
            "inputs": len(self.inputs),
            "notes": list(self.notes),
            **self.metadata,
        }


# ---------------------------------------------------------------------------
# the envelope
# ---------------------------------------------------------------------------


def build_envelope(
    spans: Sequence[DuckSpan],
    *,
    duration_seconds: float,
    config: AudioConfig,
    sample_rate: int = ENVELOPE_SAMPLE_RATE,
) -> np.ndarray:
    """The gain curve to multiply a track by, one sample per output sample.

    Overlapping spans take the **deepest** reduction rather than adding up:
    speech during a big game moment should duck the music once, by the larger
    amount, not twice into inaudibility.

    Attack, release and hold come from §74 and are applied as linear ramps. A
    step would click, and a click in the finished video is more noticeable than
    the music it was trying to hide.
    """
    total = max(1, round(duration_seconds * sample_rate))
    envelope = np.ones(total, dtype=np.float32)
    if not spans:
        return envelope

    attack = max(1, int(config.ducking.attack_ms * sample_rate / 1000))
    release = max(1, int(config.ducking.release_ms * sample_rate / 1000))
    hold = int(config.ducking.hold_ms * sample_rate / 1000)

    for span in spans:
        gain = _db_to_gain(span.depth_db)
        start = round(max(0.0, span.start) * sample_rate)
        end = round(max(span.start, span.end) * sample_rate) + hold
        if start >= total:
            continue
        end = min(end, total)

        # Attack ramps down before the span begins, so the music is already
        # out of the way by the first syllable rather than during it.
        ramp_start = max(0, start - attack)
        if start > ramp_start:
            ramp = np.linspace(1.0, gain, start - ramp_start, endpoint=False, dtype=np.float32)
            envelope[ramp_start:start] = np.minimum(envelope[ramp_start:start], ramp)

        if end > start:
            envelope[start:end] = np.minimum(envelope[start:end], gain)

        release_end = min(total, end + release)
        if release_end > end:
            ramp = np.linspace(gain, 1.0, release_end - end, endpoint=False, dtype=np.float32)
            envelope[end:release_end] = np.minimum(envelope[end:release_end], ramp)

    return envelope


def write_envelope(
    envelope: np.ndarray, destination: Path, *, sample_rate: int = ENVELOPE_SAMPLE_RATE
) -> Path:
    """Write an envelope as a stereo 16-bit WAV for ``amultiply``.

    Stereo because the tracks it multiplies are stereo and ``amultiply``
    requires matching layouts; 16-bit because an envelope needs a fraction of
    the precision that audio does, and this file is written for every render.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    samples = np.clip(envelope, 0.0, 1.0)
    quantised = (samples * 32767.0).astype(np.int16)
    interleaved = np.repeat(quantised[:, None], 2, axis=1).reshape(-1)

    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(interleaved.tobytes())
    return destination


def speech_spans(
    segments: Iterable[tuple[float, float]], config: AudioConfig
) -> list[DuckSpan]:
    """Spans where the player is speaking, at §74's speech depth."""
    return [
        DuckSpan(start=start, end=end, depth_db=config.ducking.speech_duck_db)
        for start, end in segments
        if end > start
    ]


def game_under_speech_spans(
    segments: Iterable[tuple[float, float]], config: AudioConfig
) -> list[DuckSpan]:
    """Speech spans again, at the gameplay track's much shallower depth (§72).

    Separate from :func:`speech_spans` because the two tracks give way by
    different amounts: the music steps aside, the gameplay only leans back.
    """
    return [
        DuckSpan(start=start, end=end, depth_db=config.ducking.game_under_speech_db)
        for start, end in segments
        if end > start
    ]


def event_spans(
    events: Iterable[tuple[float, float]], config: AudioConfig
) -> list[DuckSpan]:
    """Spans covering important game audio, at §74's shallower depth."""
    return [
        DuckSpan(start=start, end=end, depth_db=config.ducking.game_event_duck_db)
        for start, end in events
        if end > start
    ]


def merge_spans(spans: Sequence[DuckSpan], *, gap: float = 0.25) -> list[DuckSpan]:
    """Join spans closer together than ``gap``, keeping the deeper reduction.

    Music that dips between two words of the same sentence sounds broken. The
    envelope would already ramp back up in the gap, so merging first is what
    keeps it down across the whole utterance.
    """
    if not spans:
        return []
    ordered = sorted(spans, key=lambda span: span.start)
    merged = [ordered[0]]
    for span in ordered[1:]:
        last = merged[-1]
        if span.start - last.end <= gap:
            merged[-1] = DuckSpan(
                start=last.start,
                end=max(last.end, span.end),
                depth_db=min(last.depth_db, span.depth_db),
            )
        else:
            merged.append(span)
    return merged


# ---------------------------------------------------------------------------
# the mix
# ---------------------------------------------------------------------------


def plan_mix(
    *,
    game: Path,
    microphone: Path | None,
    music: Sequence[Path],
    music_envelope: Path | None,
    game_envelope: Path | None,
    config: AudioConfig,
    duration_seconds: float,
) -> MixPlan:
    """Build the filter graph for the finished audio (§72–§74).

    Args:
        game: the programme's gameplay audio, already cut to the edit.
        microphone: the player's voice, when it was recorded separately (§19).
        music: local files only (§73). Concatenated and looped to length.
        music_envelope: the ducking curve for music, from :func:`build_envelope`.
        game_envelope: the shallower curve that keeps speech above game audio,
            which is what §72's priority means when the game gets loud.
        duration_seconds: the finished video's length, so music can be trimmed
            to it rather than running past the end.

    Returns:
        A :class:`MixPlan`. ``inputs`` is the FFmpeg input order the graph's
        indices refer to, so the caller must not reorder it.
    """
    gains = config.mix.gains_db
    inputs: list[MixInput] = [MixInput(path=game, role="game")]
    chains: list[str] = []
    mixed: list[str] = []
    notes: list[str] = []

    game_index = 0
    chains.append(f"[{game_index}:a]{MIX_FORMAT},volume={gains.game}dB[game_v]")
    game_label = "game_v"

    microphone_index: int | None = None
    if microphone is not None:
        microphone_index = len(inputs)
        inputs.append(MixInput(path=microphone, role="microphone"))
        chains.append(
            f"[{microphone_index}:a]{MIX_FORMAT},volume={gains.microphone}dB[mic]"
        )
    else:
        notes.append("no separate microphone track; speech is inside the gameplay audio")

    music_index: int | None = None
    if music and config.music.enabled:
        music_index = len(inputs)
        # -stream_loop repeats the file; the atrim below bounds it to the
        # programme. Together they fill the video whether the bed is shorter
        # or longer than it.
        loop = ("-stream_loop", "-1") if config.music.loop else ()
        inputs.extend(MixInput(path=path, role="music", options=loop) for path in music)
        chains.append(_music_chain(music_index, len(music), config, duration_seconds))
    elif config.music.enabled:
        notes.append("no local music was found, so the mix carries none (§73)")

    # §72's priority, applied where it actually bites: when the game is loud
    # and someone is talking over it.
    if game_envelope is not None:
        envelope_index = len(inputs)
        inputs.append(MixInput(path=game_envelope, role="envelope"))
        chains.append(f"[{envelope_index}:a]{MIX_FORMAT}[game_env]")
        chains.append(f"[{game_label}][game_env]amultiply[game_d]")
        game_label = "game_d"
    mixed.append(game_label)

    if microphone_index is not None:
        mixed.append("mic")

    if music_index is not None:
        music_label = "music_v"
        if music_envelope is not None:
            envelope_index = len(inputs)
            inputs.append(MixInput(path=music_envelope, role="envelope"))
            chains.append(f"[{envelope_index}:a]{MIX_FORMAT}[music_env]")
            chains.append(f"[{music_label}][music_env]amultiply[music_d]")
            music_label = "music_d"
        else:
            notes.append("music is not ducked: nothing marked where speech or events are")
        mixed.append(music_label)

    # ``mixed`` always holds at least the gameplay track: a mix is planned
    # from a programme that has audio, and a project without any never reaches
    # here -- the render worker returns before planning one.
    joined = "".join(f"[{label}]" for label in mixed)
    # normalize=0: amix divides by the number of inputs by default, which would
    # silently drop everything by 9 dB the moment music is added. The gains are
    # already set per track and loudnorm handles the programme level.
    chains.append(
        f"{joined}amix=inputs={len(mixed)}:normalize=0:dropout_transition=0[mixed]"
    )

    last = "mixed"
    if config.mix.normalization.enabled:
        normalization = config.mix.normalization
        chains.append(
            f"[{last}]loudnorm=I={normalization.target_lufs}:"
            f"TP={normalization.true_peak_db}:LRA={normalization.loudness_range}[normalised]"
        )
        last = "normalised"

    if config.mix.limiter.enabled:
        ceiling = _db_to_gain(config.mix.limiter.ceiling_db)
        chains.append(f"[{last}]alimiter=limit={ceiling:.4f}[limited]")
        last = "limited"

    fades = config.fades
    if fades.program_fade_in_ms or fades.program_fade_out_ms:
        fade_in = fades.program_fade_in_ms / 1000.0
        fade_out = fades.program_fade_out_ms / 1000.0
        chains.append(
            f"[{last}]afade=t=in:st=0:d={fade_in:.3f},"
            f"afade=t=out:st={max(0.0, duration_seconds - fade_out):.3f}:d={fade_out:.3f}[aout]"
        )
        last = "aout"

    plan = MixPlan(
        inputs=tuple(inputs),
        filter_complex=";".join(chains),
        output_label=last,
        notes=tuple(notes),
        metadata={
            "tracks": len(mixed),
            "music_files": len(music) if music_index is not None else 0,
            "ducked_music": music_envelope is not None,
            "ducked_game": game_envelope is not None,
        },
    )
    logger.info("Planned the audio mix", extra=plan.summary())
    return plan


def find_music(directory: Path) -> list[Path]:
    """Local music files, in a stable order (§73).

    Nothing is downloaded and no catalogue is consulted. A directory the user
    filled is the only source, which is what keeps the finished video free of
    a licence nobody agreed to.
    """
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in MUSIC_SUFFIXES
    )


def _music_chain(
    first_index: int, count: int, config: AudioConfig, duration_seconds: float
) -> str:
    """Concatenate, loop and fade the music bed to the programme's length."""
    music = config.music
    gains = config.mix.gains_db
    labels = "".join(f"[{first_index + offset}:a]" for offset in range(count))
    chain = f"{labels}concat=n={count}:v=0:a=1[music_raw];[music_raw]{MIX_FORMAT}"
    if not music.loop:
        # Without looping the bed may simply run out; pad so the mix does not
        # end early when the music does.
        chain += ",apad"
    chain += f",atrim=0:{duration_seconds:.3f},asetpts=N/SR/TB"
    chain += f",volume={gains.music}dB"
    fade_in = music.fade_in_ms / 1000.0
    fade_out = music.fade_out_ms / 1000.0
    if fade_in:
        chain += f",afade=t=in:st=0:d={fade_in:.3f}"
    if fade_out:
        chain += f",afade=t=out:st={max(0.0, duration_seconds - fade_out):.3f}:d={fade_out:.3f}"
    return chain + "[music_v]"


def _db_to_gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


__all__ = [
    "ENVELOPE_SAMPLE_RATE",
    "MIX_FORMAT",
    "MUSIC_SUFFIXES",
    "DuckSpan",
    "MixInput",
    "MixPlan",
    "build_envelope",
    "event_spans",
    "find_music",
    "game_under_speech_spans",
    "merge_spans",
    "plan_mix",
    "speech_spans",
    "write_envelope",
]
