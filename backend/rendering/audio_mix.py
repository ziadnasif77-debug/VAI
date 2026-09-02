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

from backend.audio_director.plan import LOUD_ENOUGH
from backend.config.schema import AudioConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import EffectType
from backend.timeline.models import TimelineClip
from backend.timeline.retime import ClipRetime

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
class Stinger:
    """One sound effect placed on the programme timeline (§68, §73).

    The asset is a local file. Nothing is downloaded, ever: §73 governs music
    and the same rule governs every other sample, because "the tool fetched a
    sound from somewhere" is a licensing question the user did not agree to.
    """

    path: Path
    at_seconds: float
    gain_db: float = -6.0


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
    events: Iterable[Sequence[float]], config: AudioConfig
) -> list[DuckSpan]:
    """Spans covering important game audio, at §74's shallower depth.

    The depth is set by how loud the event actually got (V2-P2.6). Each item
    is ``(start, end)`` or ``(start, end, peak)``, where ``peak`` is the audio
    lane's highest value inside the span, on the same 0-1 scale as the
    threshold that defined it.

    Interpolated between ``game_event_duck_floor_db`` at the threshold and
    ``game_event_duck_db`` at full scale, so the configured depth keeps its
    meaning -- it is what an event at 1.0 gets, and nothing ducks further.
    Without a peak the configured depth applies unchanged, which is what every
    caller got before the lane's measurement was kept.
    """
    ducking = config.ducking
    deep = float(ducking.game_event_duck_db)
    shallow = float(ducking.game_event_duck_floor_db)
    spans: list[DuckSpan] = []
    for event in events:
        start, end = float(event[0]), float(event[1])
        if end <= start:
            continue
        peak = float(event[2]) if len(event) > 2 else None
        spans.append(DuckSpan(start=start, end=end, depth_db=_duck_depth(peak, shallow, deep)))
    return spans


def _duck_depth(peak: float | None, shallow: float, deep: float) -> float:
    """How far the bed steps aside for an event that peaked at ``peak``.

    Clamped at both ends rather than extrapolated: a lane value above 1.0 is
    not a louder-than-possible explosion, it is a lane that needs looking at,
    and inventing a duck deeper than the configured one on the strength of it
    would be the wrong response.
    """
    if peak is None:
        return deep
    fraction = (peak - LOUD_ENOUGH) / (1.0 - LOUD_ENOUGH)
    fraction = min(1.0, max(0.0, fraction))
    return round(shallow + fraction * (deep - shallow), 3)


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
    stingers: Sequence[Stinger] = (),
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

    for position, stinger in enumerate(stingers):
        if not stinger.path.is_file():
            notes.append(f"sound effect {stinger.path.name!r} is missing and was skipped")
            continue
        index = len(inputs)
        inputs.append(MixInput(path=stinger.path, role="sfx"))
        label = f"sfx{position}"
        # adelay places it on the programme clock; apad keeps the stream alive
        # to the end so amix does not truncate the mix to the shortest input.
        delay_ms = max(0, round(stinger.at_seconds * 1000))
        chains.append(
            f"[{index}:a]{MIX_FORMAT},volume={stinger.gain_db}dB,"
            f"adelay={delay_ms}|{delay_ms},apad=whole_dur={duration_seconds:.3f}[{label}]"
        )
        mixed.append(label)
    if stingers:
        notes.append(f"{len(stingers)} sound effect(s) placed")

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


def find_music(directory: Path, *, mean_intensity: float | None = None) -> list[Path]:
    """Local music files, in a stable order (§73), intensity-aware (§15).

    Nothing is downloaded and no catalogue is consulted. A directory the user
    filled is the only source, which is what keeps the finished video free of
    a licence nobody agreed to.

    When the person sorts their music into ``low/``, ``build/`` and ``peak/``
    subfolders, the edit's own mean intensity picks the shelf -- a calm
    session gets the calm bed. Flat directories behave exactly as before,
    and a missing shelf falls back to whatever shelves exist.
    """
    if not directory.is_dir():
        return []
    if mean_intensity is not None:
        shelf = (
            "peak" if mean_intensity >= 0.6 else "low" if mean_intensity < 0.45 else "build"
        )
        for name in (shelf, "build", "low", "peak"):
            candidate = directory / name
            if candidate.is_dir():
                chosen = find_music(candidate)
                if chosen:
                    return chosen
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


# ---------------------------------------------------------------------------
# time-warped clip audio (doctrine §11, §12)
# ---------------------------------------------------------------------------
#
# When the EDL re-lays a clip for a freeze or a ramp, the *audio* for that clip
# must occupy the same re-laid seconds as the picture, or every later clip's
# sound lands early by the added time. Both audio assemblies -- the render
# worker's concat graph and the J/L amix graph -- build one chain per clip, so
# the warped construction lives here, once, where MIX_FORMAT already does.

#: Seam fade inside a warped clip: enough to take the waveform through zero at
#: the silence boundary of a freeze and the tempo joins of a ramp, mirroring
#: the concat path's join fade.
_WARP_SEAM_FADE_SECONDS: Final[float] = 0.03


def atempo_steps(factor: float) -> str:
    """``atempo`` filters realising one tempo factor, chained when needed.

    ``atempo`` refuses factors below 0.5, and the doctrine's default slowdown
    is 0.4 -- so a single filter cannot express the shape the config asks for.
    Factors halve out of the chain until the remainder is legal: 0.4 becomes
    ``atempo=0.5,atempo=0.8`` (0.5 x 0.8 = 0.4). The symmetric clamp at 2.0
    is handled the same way, although nothing in the library speeds audio up
    today.
    """
    if factor <= 0.0:
        raise ValueError(f"an atempo factor must be positive, not {factor}")
    steps: list[float] = []
    remaining = factor
    while remaining < 0.5:
        steps.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        steps.append(2.0)
        remaining /= 2.0
    if not steps or abs(remaining - 1.0) > 1e-9:
        steps.append(remaining)
    return ",".join(f"atempo={step:.6f}" for step in steps)


def warped_clip_audio(
    input_index: int,
    clip: TimelineClip,
    warp: ClipRetime,
    *,
    out_label: str,
    fade_in: float,
    fade_out: float,
    delay_ms: int = 0,
) -> str:
    """One retimed clip's audio as ``;``-joined filtergraph statements.

    Mirrors the picture's construction piece for piece so the two cannot
    disagree about where the added seconds sit. Under a freeze the added time
    is **silence** (``apad``): a hold with looping audio is worse than quiet,
    and stingers are a later layer (§72). Under a ramp the slow window is
    stretched with :func:`atempo_steps`, pitch-preserved, exactly as long as
    the slowed picture.

    Args:
        input_index: the clip's ``-i`` position in the caller's command.
        clip: the timeline clip (``source_in``/``source_out``/``duration``).
        warp: the clip's :class:`~backend.timeline.retime.ClipRetime`.
        out_label: label the final statement ends with, without brackets.
        fade_in / fade_out: the caller's edge fades, applied on the *output*
            clock -- the fade-out belongs at the end of the extended clip.
        delay_ms: placement delay for amix-style callers (the J/L graph); the
            concat caller leaves it zero.
    """
    source_in, source_out = clip.source_in, clip.source_out
    source = source_out - source_in
    total = clip.duration
    at = min(max(warp.at_seconds, 0.0), source)
    seam = min(_WARP_SEAM_FADE_SECONDS, source / 4)
    prefix = f"aw{input_index}"

    base = (
        f"[{input_index}:a:0]atrim=start={source_in:.6f}:end={source_out:.6f},"
        f"asetpts=N/SR/TB,{MIX_FORMAT}"
    )
    statements: list[str] = []

    if warp.effect is EffectType.SPEED_RAMP:
        start = at
        end = min(at + warp.window_seconds, source)
        # The same edge collapse and factor re-derivation as the picture: a
        # sub-frame piece is dropped into the window, and the factor bends so
        # the stretched length still equals window + extra exactly.
        if start <= 0.05:
            start = 0.0
        if end >= source - 0.05:
            end = source
        window = max(end - start, 1e-3)
        factor = window / (window + warp.extra_seconds)
        slow_length = window + warp.extra_seconds
        # atempo's WSOLA loses a window or two at the stream's end -- measured
        # 30 ms short on a 2 s stretch -- and the concat assembly would hand
        # that drift to every later clip. The true tail is faded through
        # ``areverse`` (a stamped st would fade padding, not sound), then the
        # piece is pinned to its exact arithmetic length: padded up, trimmed
        # down, whichever the build's atempo produced.
        slow = (
            f"atrim=start={start:.6f}:end={end:.6f},asetpts=N/SR/TB,{atempo_steps(factor)}"
            f",areverse,afade=t=in:st=0:d={seam:.3f},areverse"
            f",apad=whole_dur={slow_length:.6f},atrim=end={slow_length:.6f},asetpts=N/SR/TB"
        )
        pieces: list[tuple[str, float, bool]] = []
        if start > 0.0:
            pieces.append((f"atrim=end={start:.6f},asetpts=N/SR/TB", start, False))
        pieces.append((slow, slow_length, True))
        if end < source:
            pieces.append((f"atrim=start={end:.6f},asetpts=N/SR/TB", source - end, False))
        if len(pieces) == 1:
            joined = f"{base},{pieces[0][0]}"
        else:
            statements.append(f"{base}[{prefix}]")
            labels = "".join(f"[{prefix}s{index}]" for index in range(len(pieces)))
            statements.append(f"[{prefix}]asplit={len(pieces)}{labels}")
            for index, (piece, length, faded) in enumerate(pieces):
                fades = _seam_fades(index, len(pieces), length, seam, tail_handled=faded)
                statements.append(f"[{prefix}s{index}]{piece}{fades}[{prefix}p{index}]")
            inputs = "".join(f"[{prefix}p{index}]" for index in range(len(pieces)))
            joined = f"{inputs}concat=n={len(pieces)}:v=0:a=1"
    elif at <= 0.05:
        # Freeze at the head: the silence leads, so the sound starts late by
        # the hold -- adelay puts it there and the seam fade softens the entry.
        joined = f"{base},afade=t=in:st=0:d={seam:.3f},adelay={round(warp.extra_seconds * 1000)}"
        joined += f"|{round(warp.extra_seconds * 1000)}"
    elif at >= source - 0.05:
        joined = f"{base},afade=t=out:st={max(0.0, source - seam):.3f}:d={seam:.3f}"
        joined += f",apad=pad_dur={warp.extra_seconds:.6f}"
    else:
        statements.append(f"{base}[{prefix}]")
        statements.append(f"[{prefix}]asplit=2[{prefix}s0][{prefix}s1]")
        statements.append(
            f"[{prefix}s0]atrim=end={at:.6f},asetpts=N/SR/TB,"
            f"afade=t=out:st={max(0.0, at - seam):.3f}:d={seam:.3f},"
            f"apad=pad_dur={warp.extra_seconds:.6f}[{prefix}p0]"
        )
        statements.append(
            f"[{prefix}s1]atrim=start={at:.6f},asetpts=N/SR/TB,"
            f"afade=t=in:st=0:d={seam:.3f}[{prefix}p1]"
        )
        joined = f"[{prefix}p0][{prefix}p1]concat=n=2:v=0:a=1"

    if fade_in > 0:
        joined += f",afade=t=in:st=0:d={fade_in:.3f}"
    if fade_out > 0:
        joined += f",afade=t=out:st={max(0.0, total - fade_out):.3f}:d={fade_out:.3f}"
    if delay_ms:
        joined += f",adelay={delay_ms}|{delay_ms}"
    statements.append(f"{joined}[{out_label}]")
    return ";".join(statements)


def _seam_fades(
    index: int, count: int, length: float, seam: float, *, tail_handled: bool = False
) -> str:
    """Micro fades at a warped clip's internal joins, never at its outer edges.

    ``atempo`` reassembles the waveform, so the samples either side of a piece
    boundary no longer line up; an unfaded join clicks exactly the way the
    concat path's joins did before its micro fades existed. ``length`` is the
    piece's *post-filter* duration; a piece whose chain already faded its own
    tail (the slow piece, through ``areverse``) says so via ``tail_handled``.
    """
    fade = min(seam, length / 4)
    steps = ""
    if index > 0:
        steps += f",afade=t=in:st=0:d={fade:.3f}"
    if index < count - 1 and not tail_handled:
        steps += f",afade=t=out:st={max(0.0, length - fade):.3f}:d={fade:.3f}"
    return steps


__all__ = [
    "ENVELOPE_SAMPLE_RATE",
    "MIX_FORMAT",
    "MUSIC_SUFFIXES",
    "DuckSpan",
    "MixInput",
    "MixPlan",
    "atempo_steps",
    "build_envelope",
    "event_spans",
    "find_music",
    "game_under_speech_spans",
    "merge_spans",
    "plan_mix",
    "speech_spans",
    "warped_clip_audio",
    "write_envelope",
]
