"""Shared test fixtures.

Every fixture that touches storage points at a ``tmp_path``. No test writes
into the developer's real ``projects/`` tree or database.

Media fixtures generate their clips with FFmpeg's ``testsrc`` and ``sine``
sources rather than committing binaries to the repository: a generated clip is
a few kilobytes of command line, is deterministic, and can be made with exactly
the properties a test needs -- two audio tracks, no audio at all, an odd frame
rate. Tests that need them are marked ``requires_ffmpeg`` and skip cleanly on a
machine without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest

from ai.speech.fake_provider import FakeSpeechProvider
from backend.api.dependencies import AppState, build_state
from backend.config.loader import load_config, reset_config_cache
from backend.config.paths import Paths, build_paths, find_repository_root
from backend.config.schema import AppConfig
from backend.core.logging import shutdown_logging
from backend.core.models.enums import JobStage
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.media.ffmpeg import FFmpegRunner
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers import default_workers
from backend.pipeline.workers.speech_workers import TranscriptWorker
from backend.services.job_manager import JobManager
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager

#: Set to run tests against real AI models. Off by default because the first
#: run downloads gigabytes of weights, and a suite that does that unasked is a
#: suite nobody can run on a metered connection.
MODELS_ENV_VAR = "VAI_TEST_MODELS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests whose external dependency is absent.

    Reported as skips, not passes: a suite that goes green on a machine with no
    FFmpeg would be claiming the media engine works when nothing exercised it.
    """
    has_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    models_enabled = os.environ.get(MODELS_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}

    skip_ffmpeg = pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH")
    skip_models = pytest.mark.skip(reason=f"set {MODELS_ENV_VAR}=1 to run against real models")
    for item in items:
        if not has_ffmpeg and "requires_ffmpeg" in item.keywords:
            item.add_marker(skip_ffmpeg)
        if not models_enabled and "requires_models" in item.keywords:
            item.add_marker(skip_models)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return find_repository_root()


@pytest.fixture(scope="session")
def config_dir(repo_root: Path) -> Path:
    return repo_root / "config"


@pytest.fixture
def config(config_dir: Path) -> Iterator[AppConfig]:
    """The real shipped configuration, loaded fresh."""
    reset_config_cache()
    yield load_config(config_dir)
    reset_config_cache()


@pytest.fixture
def paths(config: AppConfig, tmp_path: Path) -> Paths:
    """Application paths rooted in a temporary directory."""
    return build_paths(config, data_root=tmp_path).create()


@pytest.fixture
def database(paths: Paths, config: AppConfig) -> Iterator[Database]:
    """A migrated database in the temporary data root."""
    db = Database(paths.database_path, config.application.database)
    migrate(db)
    yield db
    db.close()


@pytest.fixture
def project_manager(database: Database, paths: Paths, config: AppConfig) -> ProjectManager:
    return ProjectManager(database, paths, config)


@pytest.fixture
def job_manager(database: Database, config: AppConfig) -> JobManager:
    return JobManager(database, config)


@pytest.fixture
def media_service(
    database: Database, paths: Paths, config: AppConfig, job_manager: JobManager
) -> MediaIngestionService:
    return MediaIngestionService(database, paths, config, job_manager)


@pytest.fixture
def app_state(config: AppConfig, tmp_path: Path) -> Iterator[AppState]:
    """A fully wired application state on temporary storage."""
    state = build_state(config=config, data_root=tmp_path)
    yield state
    state.close()
    shutdown_logging()


@pytest.fixture
def api_client(app_state: AppState) -> Iterator:
    """A ``TestClient`` bound to the temporary application state."""
    from fastapi.testclient import TestClient

    from backend.api.app import create_app

    with TestClient(create_app(state=app_state)) as client:
        yield client


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    """A small file that passes ingestion validation.

    Ingestion checks existence, extension and size; it never decodes. A real
    decodable clip is only needed from Phase 2 onward.
    """
    path = tmp_path / "gameplay.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512)
    return path


# ---------------------------------------------------------------------------
# media fixtures (Phase 2)
# ---------------------------------------------------------------------------


def make_test_clip(
    path: Path,
    *,
    seconds: float = 6.0,
    width: int = 320,
    height: int = 240,
    fps: str = "30",
    audio_tracks: int = 1,
    video: bool = True,
) -> Path:
    """Generate a synthetic clip with FFmpeg and return its path.

    Args:
        fps: passed through verbatim, so a test can ask for ``"30000/1001"``
            and check that the rational is not rounded away.
        audio_tracks: more than one produces the layout §19 cares about -- game
            audio plus a separate microphone.
        video: ``False`` produces an audio-only file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    maps: list[str] = []
    stream = 0

    if video:
        inputs += ["-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}"]
        maps += ["-map", f"{stream}:v"]
        stream += 1
    for index in range(audio_tracks):
        frequency = 440 * (index + 1)
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000"]
        maps += ["-map", f"{stream}:a"]
        stream += 1

    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        *inputs,
        "-t", f"{seconds:.3f}",
        *maps,
    ]
    if video:
        # ultrafast + a high CRF: these clips are read, never watched.
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                    "-pix_fmt", "yuv420p"]
    if audio_tracks:
        command += ["-c:a", "aac", "-b:a", "64k"]
    command.append(str(path))

    subprocess.run(command, check=True, capture_output=True, timeout=600)
    return path


@pytest.fixture(scope="session")
def media_fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped directory for generated clips, so each is made once."""
    return tmp_path_factory.mktemp("clips")


@pytest.fixture(scope="session")
def clip_factory():
    """Return :func:`make_test_clip` for tests that need a bespoke clip.

    Handed over as a fixture rather than imported. ``import tests.conftest``
    resolves against whatever ``tests`` package happens to be first on
    ``sys.path`` -- and an unrelated one installed in site-packages will win.
    """
    return make_test_clip


@pytest.fixture(scope="session")
def test_clip(media_fixtures_dir: Path) -> Path:
    """Six seconds of video with one audio track."""
    return make_test_clip(media_fixtures_dir / "clip.mp4")


@pytest.fixture(scope="session")
def two_track_clip(media_fixtures_dir: Path) -> Path:
    """Six seconds with two audio tracks: game audio plus a microphone (§19)."""
    return make_test_clip(media_fixtures_dir / "two_track.mp4", audio_tracks=2)


@pytest.fixture(scope="session")
def silent_clip(media_fixtures_dir: Path) -> Path:
    """Video with no audio at all. Silent gameplay is a real recording."""
    return make_test_clip(media_fixtures_dir / "silent.mp4", audio_tracks=0)


@pytest.fixture(scope="session")
def ntsc_clip(media_fixtures_dir: Path) -> Path:
    """A 30000/1001 clip: the rational frame rate that must not be rounded."""
    return make_test_clip(media_fixtures_dir / "ntsc.mp4", fps="30000/1001")


@pytest.fixture(scope="session")
def hd_clip(media_fixtures_dir: Path) -> Path:
    """1080p60, so the proxy has something to actually downscale."""
    return make_test_clip(
        media_fixtures_dir / "hd.mp4", seconds=3.0, width=1920, height=1080, fps="60"
    )


def write_wav(path: Path, samples, *, sample_rate: int = 16000) -> Path:
    """Write a mono 16-bit PCM WAV. No FFmpeg involved.

    Audio fixtures are built numerically rather than transcoded, because a test
    for a 5 Hz amplitude modulation needs a signal with a 5 Hz amplitude
    modulation in it, and no synthetic source generator produces that on
    request.
    """
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(data.tobytes())
    return path


def make_reaction_audio(seconds: float = 40.0, sample_rate: int = 16000):
    """Return ``(gameplay, microphone)`` signals with a known event structure.

    Gameplay carries three impacts, at 5 s, 15 s and 25 s. The microphone
    carries a laugh after the first (amplitude-modulated at 5 Hz, which is what
    makes laughter identifiable) and a scream after the second (loud and
    bright). Nothing follows the third, so a test can check that the detector
    does not invent a reaction where there was none.
    """
    import numpy as np

    time = np.arange(int(sample_rate * seconds)) / sample_rate

    gameplay = 0.02 * np.sin(2 * np.pi * 220 * time)
    for impact in (5.0, 15.0, 25.0):
        window = (time >= impact) & (time < impact + 0.4)
        gameplay[window] += 0.7 * np.sin(2 * np.pi * 900 * time[window])

    microphone = 0.01 * np.sin(2 * np.pi * 200 * time)
    laugh = (time >= 6.0) & (time < 8.0)
    microphone[laugh] += (
        0.35
        * (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * time[laugh]))
        * np.sin(2 * np.pi * 400 * time[laugh])
    )
    scream = (time >= 16.0) & (time < 17.2)
    microphone[scream] += 0.85 * np.sin(2 * np.pi * 1400 * time[scream])

    return gameplay, microphone


@pytest.fixture(scope="session")
def reaction_clip(media_fixtures_dir: Path) -> Path:
    """A recording whose second audio track is the player's microphone (§19).

    Built by muxing two numerically generated tracks into one file, which is
    exactly the shape a capture tool produces when the user records their
    microphone separately.
    """
    gameplay, microphone = make_reaction_audio()
    game_wav = write_wav(media_fixtures_dir / "reaction_game.wav", gameplay)
    mic_wav = write_wav(media_fixtures_dir / "reaction_mic.wav", microphone)
    target = media_fixtures_dir / "reaction.mp4"

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-i", str(game_wav),
            "-i", str(mic_wav),
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-t", "40",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p",
            # PCM through to the analysis stage: an AAC pass at 64 kbit would
            # smear the transients this fixture exists to carry.
            "-c:a", "pcm_s16le",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return target


@pytest.fixture(scope="session")
def long_clip(media_fixtures_dir: Path) -> Path:
    """Fifteen minutes: long enough to need more than one proxy segment.

    Small frame size on purpose. The §7 question is whether memory grows with a
    recording's *length*, and a low resolution makes a long source cheap to
    generate without weakening that question at all.
    """
    return make_test_clip(
        media_fixtures_dir / "long.mp4", seconds=900.0, width=640, height=360, fps="30"
    )


@pytest.fixture
def ffmpeg_runner(config: AppConfig) -> FFmpegRunner:
    return FFmpegRunner(config.ffmpeg)


# ---------------------------------------------------------------------------
# AI provider fixtures (Phase 3)
# ---------------------------------------------------------------------------


@pytest.fixture
def speech_provider() -> FakeSpeechProvider:
    """A deterministic speech provider.

    Every test that runs the pipeline gets this one. Whisper large-v3 is a 3 GB
    download and minutes per run, and a test suite that quietly fetches it is a
    test suite nobody can run on a metered connection. Tests that genuinely
    need the real model are marked ``requires_models`` and build it themselves.
    """
    return FakeSpeechProvider()


@pytest.fixture
def pipeline_runner(
    database: Database,
    paths: Paths,
    config: AppConfig,
    speech_provider: FakeSpeechProvider,
) -> PipelineRunner:
    """A runner whose model-backed stages use test doubles."""
    workers = default_workers()
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
    return PipelineRunner(database, paths, config, workers=workers)
