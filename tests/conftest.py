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

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.api.dependencies import AppState, build_state
from backend.config.loader import load_config, reset_config_cache
from backend.config.paths import Paths, build_paths, find_repository_root
from backend.config.schema import AppConfig
from backend.core.logging import shutdown_logging
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.media.ffmpeg import FFmpegRunner
from backend.services.job_manager import JobManager
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip FFmpeg-dependent tests when FFmpeg is not installed.

    Reported as skips, not passes: a suite that goes green on a machine with no
    FFmpeg would be claiming the media engine works when nothing exercised it.
    """
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    skip = pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH")
    for item in items:
        if "requires_ffmpeg" in item.keywords:
            item.add_marker(skip)


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
