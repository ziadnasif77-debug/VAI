"""Choosing an encoder and building its arguments (SPEC §65, §75, §52).

Two encoders, one interface. NVENC is several times faster than libx264 at
comparable quality for this kind of material, and an 8 GB card can run it while
a model sits in VRAM. libx264 is the fallback, and §52 makes it a *real* one:
the pipeline must run on a machine with no GPU at all, so the CPU path is not a
degraded mode to apologise for -- it is the path most machines will take.

Their quality knobs are not interchangeable, and this is where that is handled
rather than pushed onto callers. libx264 takes a CRF: a quality target, and the
bitrate follows. NVENC's rate control is bitrate-first, so the configured
bitrate for the resolution and frame rate is what it is given, with a ceiling
above it for the moments that need the bits. A caller asking for "1080p60"
should not have to know which of those two things it will get.

Nothing here runs FFmpeg. Argument construction is separated from execution so
that what the encoder will be told is testable without encoding anything, which
matters when a wrong flag costs twenty minutes to discover.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from backend.config.schema import RenderConfig, YoutubePresetConfig
from backend.core.errors import ErrorCode, RenderError
from backend.core.logging import LogChannel, get_logger
from backend.media.ffmpeg import FFmpegRunner

logger = get_logger("rendering.encoder", LogChannel.RENDERING)

#: Encoders this module knows how to configure. An entry in
#: ``render.encoder_preference`` that is not here is a configuration error
#: rather than something to muddle through, because the flags differ enough
#: that guessing would produce a file nobody asked for.
KNOWN_ENCODERS: Final[frozenset[str]] = frozenset(
    {"h264_nvenc", "hevc_nvenc", "libx264", "libx265"}
)

#: Fallback bitrate when the resolution/fps pair has no configured entry.
#: Deliberately generous: an unfamiliar format is not the moment to be stingy,
#: and the ceiling multiplier still bounds it.
_DEFAULT_BITRATE: Final[str] = "12M"


@dataclass(frozen=True, slots=True)
class EncodeTarget:
    """What the finished file should be.

    Separate from the config because a project can override the preset -- §75
    exposes a YouTube target, and a user asking for 720p should not have to
    edit a file to get it.
    """

    width: int
    height: int
    fps: int
    audio_codec: str = "aac"
    audio_bitrate: str = "384k"
    audio_sample_rate: int = 48000
    audio_channels: int = 2

    @property
    def key(self) -> str:
        """The ``1080p60`` form the bitrate table is keyed by."""
        return f"{self.height}p{self.fps}"

    @classmethod
    def from_preset(cls, preset: YoutubePresetConfig, *, width: int) -> EncodeTarget:
        return cls(
            width=width,
            height=preset.resolution,
            fps=preset.fps,
            audio_codec=preset.audio_codec,
            audio_bitrate=preset.audio_bitrate,
            audio_sample_rate=preset.audio_sample_rate,
            audio_channels=preset.audio_channels,
        )


@dataclass(frozen=True, slots=True)
class EncoderChoice:
    """The encoder that will actually be used, and how it was reached."""

    name: str
    hardware: bool
    reason: str

    def summary(self) -> dict[str, Any]:
        return {"encoder": self.name, "hardware": self.hardware, "reason": self.reason}


def select_encoder(config: RenderConfig, runner: FFmpegRunner) -> EncoderChoice:
    """Pick the first configured encoder that can actually encode (§52).

    Listed is not the same as working, and the difference is not theoretical:
    on the machine this was built for, `ffmpeg -encoders` advertises
    `h264_nvenc` while the driver is a version too old for the build to open
    it. Every render would have failed on the first clip, several minutes in,
    with a message about "incorrect parameters".

    So each candidate is *tried* — one frame of generated colour, encoded to
    nowhere — before it is chosen. It costs about a tenth of a second, once per
    render, and it is the same lesson the OCR engine check learned: an
    installed-but-broken component reports as ready right up until the moment
    it matters.
    """
    preference = list(config.encoder_preference)
    unknown = [name for name in preference if name not in KNOWN_ENCODERS]
    if unknown:
        raise RenderError(
            "render.encoder_preference names encoders this build cannot configure: "
            + ", ".join(unknown),
            code=ErrorCode.ENCODING_FAILED,
            details={"known": sorted(KNOWN_ENCODERS)},
            recoverable=False,
        )

    available = runner.available_encoders()
    rejected: list[str] = []
    for name in preference:
        if name not in available:
            rejected.append(f"{name}: not in this FFmpeg build")
            continue
        working, why = encoder_works(name, runner)
        if not working:
            logger.warning(
                "A configured encoder is listed but cannot encode",
                extra={"encoder": name, "reason": why},
            )
            rejected.append(f"{name}: {why}")
            continue

        hardware = name.endswith("_nvenc")
        reason = "hardware encoding available" if hardware else "verified by a test encode"
        if rejected:
            reason += f" (after {len(rejected)} unusable candidate(s))"
        choice = EncoderChoice(name=name, hardware=hardware, reason=reason)
        logger.info("Selected the video encoder", extra=choice.summary())
        return choice

    raise RenderError(
        "None of the configured encoders can encode on this machine.",
        code=ErrorCode.ENCODING_FAILED,
        details={"preference": preference, "rejected": rejected},
        recoverable=False,
    )


def encoder_works(name: str, runner: FFmpegRunner) -> tuple[bool, str]:
    """Whether an encoder can actually open, and the reason when it cannot.

    One frame of generated colour, encoded to the null muxer. Nothing is read
    from disk and nothing is written to it, so this is safe to call whenever
    the answer matters -- which includes the health report, so that a machine
    told "hardware encoding available" can act on it.
    """
    result = runner.run(
        [
            *runner.base_arguments(loglevel="error"),
            "-f", "lavfi",
            "-i", "color=c=black:s=320x240:d=0.04",
            "-frames:v", "1",
            "-c:v", name,
            "-f", "null",
            "-",
        ],
        timeout_seconds=60,
        check=False,
    )
    if result.ok:
        return True, "encoded a test frame"
    return False, _first_error(result.stderr) or f"exited with code {result.returncode}"


def _first_error(stderr: str) -> str:
    """The most useful line of an FFmpeg failure, for a one-line report.

    A driver-version line is chosen over the generic "error while opening
    encoder" that follows it, because the first says what to do about it.
    """
    lines = [_strip_component(line) for line in stderr.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    for line in lines:
        if "driver" in line.lower() or "minimum required" in line.lower():
            return line[:200]
    return lines[0][:200]


def _strip_component(line: str) -> str:
    """Drop FFmpeg's ``[component @ 0x...]`` prefix, keeping the message."""
    text = line.strip()
    if text.startswith("["):
        _, _, remainder = text.partition("] ")
        text = remainder.strip() or text
    return text


def video_arguments(
    choice: EncoderChoice, target: EncodeTarget, config: RenderConfig
) -> list[str]:
    """Encoder flags for the final video stream (§75).

    The GOP is set in frames from ``gop_seconds``, so a two-second keyframe
    interval stays two seconds at any frame rate -- players seek by keyframe,
    and a GOP expressed in frames drifts the moment someone renders at 30.
    """
    gop = max(1, round(config.gop_seconds * target.fps))
    arguments = ["-c:v", choice.name]

    if choice.hardware:
        nvenc = config.nvenc
        bitrate = config.bitrate_for(target.height, target.fps) or _DEFAULT_BITRATE
        arguments += [
            "-preset", nvenc.preset,
            "-tune", nvenc.tune,
            "-rc", nvenc.rate_control,
            "-rc-lookahead", str(nvenc.rc_lookahead),
            "-b:v", bitrate,
            "-maxrate", _scaled(bitrate, config.max_bitrate_multiplier),
            # Twice the ceiling: enough buffer for a burst without letting the
            # rate controller wander for seconds at a time.
            "-bufsize", _scaled(bitrate, config.max_bitrate_multiplier * 2),
        ]
    else:
        x264 = config.libx264
        arguments += [
            "-preset", x264.preset,
            "-crf", str(x264.crf),
            # A ceiling even in CRF mode: quality-targeted encoding can spike
            # far above what a streaming platform will accept for one scene.
            "-maxrate",
            _scaled(
                config.bitrate_for(target.height, target.fps) or _DEFAULT_BITRATE,
                config.max_bitrate_multiplier,
            ),
            "-bufsize",
            _scaled(
                config.bitrate_for(target.height, target.fps) or _DEFAULT_BITRATE,
                config.max_bitrate_multiplier * 2,
            ),
        ]

    arguments += [
        "-pix_fmt", config.pixel_format,
        "-g", str(gop),
        "-keyint_min", str(gop),
        # Closed GOPs: a player seeking to a keyframe must not need frames from
        # before it.
        "-sc_threshold", "0",
        "-r", str(target.fps),
    ]
    return arguments


def audio_arguments(target: EncodeTarget) -> list[str]:
    """Encoder flags for the final audio stream (§75)."""
    return [
        "-c:a", target.audio_codec,
        "-b:a", target.audio_bitrate,
        "-ar", str(target.audio_sample_rate),
        "-ac", str(target.audio_channels),
    ]


def container_arguments(config: RenderConfig) -> list[str]:
    """Muxer flags.

    ``faststart`` moves the index to the front of the file, which is what lets
    a browser begin playing before the whole thing has arrived. It costs a
    second pass over the output and is worth it for anything destined for a
    web player (§75).
    """
    arguments: list[str] = []
    if config.faststart and config.container == "mp4":
        arguments += ["-movflags", "+faststart"]
    return arguments


def intermediate_arguments(choice: EncoderChoice, config: RenderConfig) -> list[str]:
    """Flags for a segment that will be encoded again.

    Visually lossless rather than final-quality: this file exists to be
    concatenated and re-encoded, and every artefact introduced here survives
    into the finished video. The extra disk is cheap; the generation loss is
    not.
    """
    if choice.hardware:
        return [
            "-c:v", choice.name,
            "-preset", "p4",
            "-rc", "constqp",
            "-qp", "18",
            "-pix_fmt", config.pixel_format,
        ]
    return [
        "-c:v", choice.name,
        "-preset", "veryfast",
        "-crf", "16",
        "-pix_fmt", config.pixel_format,
    ]


def _scaled(bitrate: str, multiplier: float) -> str:
    """Scale a bitrate string such as ``16M`` by a multiplier."""
    text = bitrate.strip()
    suffix = ""
    if text and text[-1].upper() in {"K", "M"}:
        suffix = text[-1]
        text = text[:-1]
    try:
        value = float(text)
    except ValueError as exc:
        raise RenderError(
            f"Cannot parse the configured bitrate {bitrate!r}.",
            code=ErrorCode.ENCODING_FAILED,
            details={"bitrate": bitrate},
            recoverable=False,
            cause=exc,
        ) from exc
    scaled = value * multiplier
    # Whole units: FFmpeg accepts fractions, but "21.6M" in a log is harder to
    # compare against a configured "16M" than "21M".
    return f"{scaled:.0f}{suffix}"


__all__ = [
    "KNOWN_ENCODERS",
    "EncodeTarget",
    "EncoderChoice",
    "audio_arguments",
    "container_arguments",
    "encoder_works",
    "intermediate_arguments",
    "select_encoder",
    "video_arguments",
]
