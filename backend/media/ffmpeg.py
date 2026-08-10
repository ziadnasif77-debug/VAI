"""The FFmpeg process layer (SPEC sections 65, 82, 85).

Every external media command in the application is issued from here. FFmpeg is
the low-level media layer -- transcoding, proxy generation, audio extraction,
frame extraction, probing, final encoding (§65) -- and this module is the only
place that knows how to start it.

Three rules shape the design:

* **No shell, ever** (§85). Commands are explicit ``list[str]`` argv passed with
  ``shell=False``. Nothing here interpolates a user string into a command line.
* **Failures are typed.** FFmpeg reports errors on stderr and exits non-zero;
  that is turned into a :class:`~backend.core.errors.GamingEditorError` with an
  :class:`~backend.core.errors.ErrorCode` and a bounded stderr excerpt, so the
  retry policy and the API agree on what went wrong (§81).
* **Long runs are observable and interruptible.** A two-hour proxy takes
  minutes, so ``-progress`` output is parsed into a fraction for the analysis
  screen (§60) and a cancellation callback is polled on a fixed tick, letting a
  worker stop at a checkpoint instead of being killed mid-write (§82).

Reading both stdout and stderr of a long-running process is where naive
implementations deadlock: whichever pipe is not being drained fills its buffer
and blocks FFmpeg. Both streams are therefore drained by reader threads and the
control loop consumes a queue, which also gives a steady tick for the timeout
and cancellation checks even when FFmpeg emits nothing at all.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

from backend.config.schema import FfmpegConfig
from backend.core.errors import DependencyError, ErrorCode, GamingEditorError, MediaError
from backend.core.logging import LogChannel, get_logger

logger = get_logger("media.ffmpeg", LogChannel.PIPELINE)

#: How much stderr to keep from a failed command. FFmpeg can emit thousands of
#: lines; the tail is where the actual error is, and an unbounded excerpt would
#: end up in a database row and an API response.
STDERR_TAIL_LINES: Final[int] = 40
STDERR_TAIL_CHARS: Final[int] = 4000

#: Control-loop tick. Bounds how long cancellation takes to be noticed and how
#: often the timeout is re-checked, independently of FFmpeg's own output rate.
_POLL_SECONDS: Final[float] = 0.5

#: Grace period between asking FFmpeg to stop and killing it.
_TERMINATE_GRACE_SECONDS: Final[float] = 5.0

#: ``-progress`` emits ``key=value`` lines; a block ends with ``progress=``.
_PROGRESS_LINE = re.compile(r"^(?P<key>[a-z_]+)=(?P<value>.*)$")

#: ``HH:MM:SS.ffffff`` as printed by ``out_time`` and by ``ffprobe``.
_TIMESTAMP = re.compile(r"^(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2}(?:\.\d+)?)$")

#: One row of ``ffmpeg -encoders``: flags, name, description.
_ENCODER_LINE = re.compile(r"^\s*[A-Z.]{6}\s+(?P<name>[\w.\-]+)\s")


class CancelledError(GamingEditorError):
    """A command stopped because cancellation was requested (§82).

    Distinct from a failure: the project is consistent, nothing is retried, and
    the job ends CANCELLED rather than FAILED.
    """

    default_code = ErrorCode.JOB_CANCELLED

    def __init__(self, message: str = "Cancelled at the next checkpoint.", **kwargs) -> None:
        kwargs.setdefault("recoverable", False)
        super().__init__(message, **kwargs)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a completed FFmpeg or ffprobe run."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(slots=True)
class ProgressReport:
    """One ``-progress`` block, translated into terms a job can report."""

    #: Position in the *output* timeline, in seconds.
    out_time_seconds: float
    #: ``out_time`` divided by the expected total, clamped to [0, 1]. ``None``
    #: when the total duration is unknown, which is why it is not a bare float:
    #: reporting a fabricated 0.0 would show a stalled progress bar.
    fraction: float | None
    frame: int | None = None
    fps: float | None = None
    speed: float | None = None
    #: True for the terminal block FFmpeg writes just before exiting.
    finished: bool = False


ProgressCallback = Callable[[ProgressReport], None]
CancelCheck = Callable[[], bool]


@dataclass
class FFmpegRunner:
    """Starts FFmpeg and ffprobe, and turns their failures into typed errors.

    Args:
        config: the ``ffmpeg`` section of ``config/rendering.yaml``. Supplies
            the binary names, the log level, the overwrite policy and the
            default timeout.
    """

    config: FfmpegConfig
    _resolved: dict[str, str] = field(default_factory=dict, repr=False)
    _encoders: frozenset[str] | None = field(default=None, repr=False)

    # -- binaries -------------------------------------------------------

    @property
    def ffmpeg_path(self) -> str:
        """Absolute path to the ffmpeg binary.

        Raises:
            DependencyError: if it is not installed. Required, not optional:
                without FFmpeg there is no media layer at all (§65).
        """
        return self._resolve(
            self.config.binary,
            code=ErrorCode.FFMPEG_NOT_FOUND,
            remediation="Install FFmpeg and put it on PATH, or set ffmpeg.binary "
            "in config/rendering.yaml to its full path.",
        )

    @property
    def ffprobe_path(self) -> str:
        """Absolute path to the ffprobe binary."""
        return self._resolve(
            self.config.ffprobe_binary,
            code=ErrorCode.FFPROBE_NOT_FOUND,
            remediation="Install FFmpeg (ffprobe ships with it), or set "
            "ffmpeg.ffprobe_binary in config/rendering.yaml to its full path.",
        )

    @property
    def is_available(self) -> bool:
        """Whether both binaries can be found, without raising."""
        return (
            shutil.which(self.config.binary) is not None
            and shutil.which(self.config.ffprobe_binary) is not None
        )

    def _resolve(self, binary: str, *, code: ErrorCode, remediation: str) -> str:
        cached = self._resolved.get(binary)
        if cached is not None:
            return cached
        # An absolute path in configuration is honoured as-is; a bare name is
        # looked up on PATH.
        candidate = Path(binary).expanduser()
        resolved = str(candidate) if candidate.is_file() else shutil.which(binary)
        if resolved is None:
            raise DependencyError(
                f"{binary} was not found.",
                code=code,
                details={"binary": binary, "remediation": remediation},
                recoverable=False,
            )
        self._resolved[binary] = resolved
        return resolved

    # -- capabilities ---------------------------------------------------

    def available_encoders(self) -> frozenset[str]:
        """Return the encoder names compiled into this FFmpeg build.

        Cached: the answer cannot change while the process is running, and the
        query costs a subprocess start.
        """
        if self._encoders is None:
            result = self.run(
                [self.ffmpeg_path, "-hide_banner", "-encoders"],
                timeout_seconds=30,
                error_code=ErrorCode.ENVIRONMENT_INVALID,
            )
            self._encoders = frozenset(
                match.group("name")
                for line in result.stdout.splitlines()
                if (match := _ENCODER_LINE.match(line))
            )
        return self._encoders

    def require_encoder(self, name: str) -> str:
        """Return ``name`` after confirming this build provides it.

        Checking up front turns "your FFmpeg has no libx264" into one clear
        error before a two-hour job starts, instead of a cryptic failure
        minutes in.
        """
        if name not in self.available_encoders():
            raise DependencyError(
                f"Encoder {name!r} is not available in this FFmpeg build.",
                code=ErrorCode.ENVIRONMENT_INVALID,
                details={"encoder": name, "binary": self.ffmpeg_path},
                recoverable=False,
            )
        return name

    def first_available_encoder(self, preference: Sequence[str]) -> str:
        """Return the first encoder from ``preference`` this build provides (§52)."""
        available = self.available_encoders()
        for name in preference:
            if name in available:
                return name
        raise DependencyError(
            "None of the preferred encoders are available in this FFmpeg build.",
            code=ErrorCode.ENVIRONMENT_INVALID,
            details={"preference": list(preference)},
            recoverable=False,
        )

    # -- command construction -------------------------------------------

    def base_arguments(self, *, loglevel: str | None = None) -> list[str]:
        """The prefix every ffmpeg invocation shares.

        ``-nostdin`` matters more than it looks: without it FFmpeg reads the
        inherited stdin, and a backend started from a terminal can have a job
        silently swallow keystrokes or block.

        Args:
            loglevel: raise the verbosity for a command whose *output* is the
                point. Measurement filters -- ``ebur128``, ``silencedetect``,
                ``astats`` -- print their results at ``info``, so at the
                configured ``error`` level they run and report nothing.
        """
        return [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            loglevel or self.config.loglevel,
            "-y" if self.config.overwrite_output else "-n",
        ]

    def input_arguments(self, source: Path, *, start: float | None = None,
                        duration: float | None = None) -> list[str]:
        """Input arguments with a fast seek.

        ``-ss`` **before** ``-i`` seeks by index rather than by decoding from
        the start. On a two-hour recording that is the difference between
        milliseconds and minutes per extraction, and it is what makes chunked
        processing viable (§7).
        """
        arguments: list[str] = []
        if start is not None and start > 0:
            arguments += ["-ss", format_seconds(start)]
        arguments += ["-i", str(source)]
        if duration is not None:
            arguments += ["-t", format_seconds(duration)]
        return arguments

    # -- execution ------------------------------------------------------

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int | None = None,
        error_code: ErrorCode = ErrorCode.MEDIA_PROBE_FAILED,
        error_type: type[GamingEditorError] = MediaError,
        error_message: str | None = None,
        details: dict[str, object] | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Run a command to completion and capture its output.

        For short commands: probing, encoder queries, single-frame extraction.
        Long encodes should use :meth:`run_with_progress` so the user sees
        movement and cancellation is possible.
        """
        command = [str(part) for part in argv]
        timeout = timeout_seconds if timeout_seconds is not None else self.config.timeout_seconds
        started = time.perf_counter()
        try:
            completed = subprocess.run(  # explicit argv, no shell (§85)
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
                **_platform_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise error_type(
                f"{Path(command[0]).name} timed out after {timeout}s.",
                code=error_code,
                details={"command": _redacted(command), "timeout_seconds": timeout,
                         **(details or {})},
                cause=exc,
            ) from exc
        except OSError as exc:
            raise error_type(
                f"Cannot run {command[0]}: {exc}",
                code=error_code,
                details={"command": _redacted(command), **(details or {})},
                cause=exc,
            ) from exc

        result = CommandResult(
            argv=tuple(command),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_seconds=time.perf_counter() - started,
        )
        if check and not result.ok:
            raise error_type(
                error_message or f"{Path(command[0]).name} exited with code {result.returncode}.",
                code=error_code,
                details={
                    "command": _redacted(command),
                    "returncode": result.returncode,
                    "stderr": tail(result.stderr),
                    **(details or {}),
                },
            )
        return result

    def run_with_progress(
        self,
        argv: Sequence[str],
        *,
        total_seconds: float | None = None,
        on_progress: ProgressCallback | None = None,
        should_cancel: CancelCheck | None = None,
        timeout_seconds: int | None = None,
        error_code: ErrorCode = ErrorCode.ENCODING_FAILED,
        error_type: type[GamingEditorError] = MediaError,
        error_message: str | None = None,
        details: dict[str, object] | None = None,
    ) -> CommandResult:
        """Run a long FFmpeg command, reporting progress and honouring cancellation.

        ``argv`` must already contain ``-progress pipe:1``; :func:`progress_arguments`
        produces it. Anything FFmpeg writes to stdout other than progress blocks
        is ignored, so this must not be used for commands that pipe media to
        stdout.

        Args:
            total_seconds: expected output duration, used to turn ``out_time``
                into a fraction. Omit it and the fraction is ``None`` rather
                than a guess.
            on_progress: called for each progress block. Must be cheap: it runs
                on the control loop.
            should_cancel: polled every tick. Returning ``True`` terminates
                FFmpeg and raises :class:`CancelledError`.

        Raises:
            CancelledError: cancellation was requested.
            GamingEditorError: of ``error_type``, on failure or timeout.
        """
        command = [str(part) for part in argv]
        timeout = timeout_seconds if timeout_seconds is not None else self.config.timeout_seconds
        deadline = time.monotonic() + timeout
        started = time.perf_counter()

        try:
            process = subprocess.Popen(  # explicit argv, no shell (§85)
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                **_platform_kwargs(),
            )
        except OSError as exc:
            raise error_type(
                f"Cannot run {command[0]}: {exc}",
                code=error_code,
                details={"command": _redacted(command), **(details or {})},
                cause=exc,
            ) from exc

        lines: queue.Queue[str | None] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        readers = [
            _start_reader(process.stdout, lines.put, on_close=lambda: lines.put(None)),
            _start_reader(process.stderr, stderr_tail.append),
        ]

        block: dict[str, str] = {}
        cancelled = False
        try:
            while True:
                try:
                    line = lines.get(timeout=_POLL_SECONDS)
                except queue.Empty:
                    line = ""
                else:
                    if line is None:  # stdout closed: FFmpeg is finishing
                        break
                    report = _consume_progress_line(line, block, total_seconds)
                    if report is not None and on_progress is not None:
                        on_progress(report)

                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                if time.monotonic() > deadline:
                    _stop(process)
                    raise error_type(
                        f"{Path(command[0]).name} timed out after {timeout}s.",
                        code=error_code,
                        details={"command": _redacted(command), "timeout_seconds": timeout,
                                 "stderr": tail("\n".join(stderr_tail)), **(details or {})},
                    )

            if cancelled:
                _stop(process)
                raise CancelledError(
                    details={"command": _redacted(command), **(details or {})}
                )

            returncode = _wait(process, deadline)
        finally:
            if process.poll() is None:  # pragma: no cover - only on an unexpected exit path
                _stop(process)
            for reader in readers:
                reader.join(timeout=_TERMINATE_GRACE_SECONDS)

        result = CommandResult(
            argv=tuple(command),
            returncode=returncode,
            stdout="",
            stderr="\n".join(stderr_tail),
            duration_seconds=time.perf_counter() - started,
        )
        if not result.ok:
            raise error_type(
                error_message or f"{Path(command[0]).name} exited with code {returncode}.",
                code=error_code,
                details={
                    "command": _redacted(command),
                    "returncode": returncode,
                    "stderr": tail(result.stderr),
                    **(details or {}),
                },
            )
        return result


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------


def progress_arguments() -> list[str]:
    """Arguments that make FFmpeg emit machine-readable progress on stdout.

    ``-nostats`` suppresses the human-readable stderr counter, which would
    otherwise interleave with real errors and make the captured tail useless.
    """
    return ["-progress", "pipe:1", "-nostats"]


def format_seconds(value: float) -> str:
    """Format a timestamp for ``-ss`` / ``-t``.

    Plain seconds with microsecond precision. FFmpeg accepts this form
    everywhere and it avoids the rounding that ``HH:MM:SS`` invites.
    """
    return f"{max(float(value), 0.0):.6f}"


def parse_rational(value: str | None) -> float | None:
    """Parse an FFmpeg rational such as ``60000/1001``.

    ffprobe reports frame rates as exact rationals precisely because 59.94 is
    not representable; ``float("60000/1001")`` raises, and rounding to 60 makes
    every derived timestamp drift. ``0/0`` means "unknown" and returns ``None``.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or text in {"N/A", "0/0"}:
        return None
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            top, bottom = float(numerator), float(denominator)
        except ValueError:
            return None
        if bottom == 0:
            return None
        return top / bottom
    try:
        return float(text)
    except ValueError:
        return None


def parse_timestamp(value: str | None) -> float | None:
    """Parse ``HH:MM:SS.ffffff`` or a bare seconds value into seconds."""
    if value is None:
        return None
    text = value.strip()
    if not text or text.upper() == "N/A":
        return None
    match = _TIMESTAMP.match(text)
    if match is not None:
        hours = float(match.group("h") or 0)
        return hours * 3600 + float(match.group("m")) * 60 + float(match.group("s"))
    try:
        return float(text)
    except ValueError:
        return None


def tail(text: str, *, lines: int = STDERR_TAIL_LINES, chars: int = STDERR_TAIL_CHARS) -> str:
    """Return the last ``lines`` lines of ``text``, capped at ``chars``.

    Failures are stored on job rows and returned over the API; the tail carries
    the actual FFmpeg error without letting a chatty command flood either.
    """
    if not text:
        return ""
    excerpt = "\n".join(text.splitlines()[-lines:])
    if len(excerpt) > chars:
        excerpt = excerpt[-chars:]
    return excerpt.strip()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _consume_progress_line(
    line: str, block: dict[str, str], total_seconds: float | None
) -> ProgressReport | None:
    """Accumulate one ``-progress`` line; return a report when a block ends."""
    match = _PROGRESS_LINE.match(line.strip())
    if match is None:
        return None
    key, value = match.group("key"), match.group("value").strip()
    if key != "progress":
        block[key] = value
        return None

    # `out_time_us` is authoritative. `out_time_ms` is misnamed -- FFmpeg has
    # always written microseconds into it -- so it is deliberately not used.
    out_time = _parse_microseconds(block.get("out_time_us"))
    if out_time is None:
        out_time = parse_timestamp(block.get("out_time"))
    finished = value == "end"

    fraction: float | None = None
    if total_seconds and total_seconds > 0 and out_time is not None:
        fraction = min(max(out_time / total_seconds, 0.0), 1.0)
    if finished:
        fraction = 1.0

    report = ProgressReport(
        out_time_seconds=out_time or 0.0,
        fraction=fraction,
        frame=_parse_int(block.get("frame")),
        fps=_parse_float(block.get("fps")),
        speed=_parse_float((block.get("speed") or "").rstrip("x")),
        finished=finished,
    )
    block.clear()
    return report


def _start_reader(
    stream: IO[str] | None,
    sink: Callable[[str], None],
    *,
    on_close: Callable[[], None] | None = None,
) -> threading.Thread:
    """Drain ``stream`` on a daemon thread.

    Both pipes must be drained continuously: whichever one is left unread fills
    its OS buffer and blocks FFmpeg mid-encode. ``on_close`` fires once the
    stream ends, which is how the control loop learns FFmpeg is finishing.
    """

    def pump() -> None:
        try:
            if stream is not None:
                for line in stream:
                    sink(line.rstrip("\r\n"))
        except (ValueError, OSError):  # pragma: no cover - stream closed under us
            pass
        finally:
            if on_close is not None:
                on_close()

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return thread


def _stop(process: subprocess.Popen) -> None:
    """Ask FFmpeg to stop, then insist.

    ``terminate`` lets it close the output file cleanly; the kill is the
    fallback for a process that ignores it. Either way the partial output is
    treated as garbage by the caller (§82: a cancelled project stays
    consistent).
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)


def _wait(process: subprocess.Popen, deadline: float) -> int:
    remaining = max(deadline - time.monotonic(), 1.0)
    try:
        return process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:  # pragma: no cover - stdout closed but no exit
        _stop(process)
        return process.returncode if process.returncode is not None else -1


def _platform_kwargs() -> dict[str, object]:
    """Platform-specific ``subprocess`` options.

    Windows is the target platform, and a backend that spawns FFmpeg dozens of
    times per analysis would otherwise flash a console window each time.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _redacted(command: Iterable[str]) -> list[str]:
    """Command for logs and error details.

    Paths are part of the diagnosis here, and everything runs locally against
    the user's own files, so the argv is kept intact -- but it goes through one
    place, so a future credential-bearing argument has somewhere to be masked.
    """
    return [str(part) for part in command]


def _parse_microseconds(value: str | None) -> float | None:
    parsed = _parse_int(value)
    return None if parsed is None else parsed / 1_000_000


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


__all__ = [
    "STDERR_TAIL_CHARS",
    "STDERR_TAIL_LINES",
    "CancelCheck",
    "CancelledError",
    "CommandResult",
    "FFmpegRunner",
    "ProgressCallback",
    "ProgressReport",
    "format_seconds",
    "parse_rational",
    "parse_timestamp",
    "progress_arguments",
    "tail",
]
