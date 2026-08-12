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

import dataclasses
import os
import shutil
import subprocess
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest

from ai.speech.fake_provider import FakeSpeechProvider
from ai.vision.fake_provider import FakeVisionProvider
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
from backend.pipeline.workers.vision_workers import VisionWorker
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
    node_ready, node_reason = _remotion_ready()
    skip_node = pytest.mark.skip(reason=node_reason)
    skip_models = pytest.mark.skip(reason=f"set {MODELS_ENV_VAR}=1 to run against real models")
    for item in items:
        if not has_ffmpeg and "requires_ffmpeg" in item.keywords:
            item.add_marker(skip_ffmpeg)
        if not models_enabled and "requires_models" in item.keywords:
            item.add_marker(skip_models)
        if not node_ready and "requires_node" in item.keywords:
            item.add_marker(skip_node)


def _remotion_ready() -> tuple[bool, str]:
    """Whether an overlay can actually be rendered on this machine.

    Reported through the same function the application uses, so a test skipping
    and the pipeline degrading agree about why.
    """
    from backend.config.loader import load_config
    from backend.rendering.remotion import is_available

    try:
        config = load_config()
    except Exception as exc:  # pragma: no cover - a broken config fails elsewhere
        return False, f"configuration could not be loaded: {exc}"
    return is_available(config.remotion, find_repository_root())


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return find_repository_root()


@pytest.fixture(scope="session")
def config_dir(repo_root: Path) -> Path:
    return repo_root / "config"


@pytest.fixture
def config(config_dir: Path) -> Iterator[AppConfig]:
    """The real shipped configuration, loaded fresh -- with narration off.

    Every other model in the pipeline is injected as a fake by the fixture that
    needs it. The narration reader builds its own provider lazily, so a machine
    with Ollama running had the pipeline tests quietly consulting a real 7B
    model: eight of them failed on clip counts that changed between runs, and
    on a machine without Ollama they would have passed. A test whose result
    depends on what is installed is not a test.

    `tests/unit/test_narration.py` injects a fake and exercises the reader
    properly; anything wanting it inside a pipeline run should do the same.
    """
    reset_config_cache()
    loaded = load_config(config_dir)
    yield loaded.model_copy(
        update={
            "analysis": loaded.analysis.model_copy(
                update={"narration": loaded.analysis.narration.model_copy(
                    update={"enabled": False}
                )}
            )
        }
    )
    reset_config_cache()


@pytest.fixture
def paths(config: AppConfig, tmp_path: Path) -> Paths:
    """Application paths rooted in a temporary directory.

    ``profiles_dir`` is redirected too. Game profiles ship with the code, so
    :func:`build_paths` resolves them against the repository -- and a test that
    writes one would be writing into the developer's checkout. The generic
    profile is a constant in code, not a file, so nothing is lost by pointing
    this at a temporary directory.
    """
    resolved = build_paths(config, data_root=tmp_path).create()
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    return dataclasses.replace(resolved, profiles_dir=profiles)


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
    """A fully wired application state on temporary storage.

    ``profiles_dir`` is redirected for the same reason the ``paths`` fixture
    redirects it: game profiles ship with the code, and a test that writes one
    would be writing into the developer's checkout. That is not hypothetical --
    the profile API tests wrote a deliberately broken profile into the real
    ``profiles/`` directory before this existed, and the next test run found it
    and failed.
    """
    from backend.gaming.profiles import clear_profile_cache

    state = build_state(config=config, data_root=tmp_path)
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    for shipped in state.paths.profiles_dir.glob("*/profile.json"):
        target = profiles / shipped.parent.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shipped, target / shipped.name)
    state.paths = dataclasses.replace(state.paths, profiles_dir=profiles)
    clear_profile_cache()
    yield state
    clear_profile_cache()
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
def duplicated_track_clip(media_fixtures_dir: Path) -> Path:
    """A recording with the *same* mix written to both audio tracks.

    What a capture tool produces when it is told to record two tracks but the
    second was never routed to a separate source. Every recording on the
    machine this project was first run against had this shape, so it is not an
    edge case -- it is the default one.
    """
    target = media_fixtures_dir / "duplicated_tracks.mp4"
    if target.is_file():
        return target

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-map", "0:v", "-map", "1:a", "-map", "1:a",
            "-t", "8",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return target


@pytest.fixture(scope="session")
def silent_mic_clip(media_fixtures_dir: Path) -> Path:
    """A recording whose second track was armed but never connected.

    The shape that breaks the "second track is the microphone" convention: the
    track exists, so it is detected, but it carries nothing. Transcribing it
    would replace a usable transcript with an empty one, so the stage has to
    notice and fall back.
    """
    target = media_fixtures_dir / "silent_mic.mp4"
    if target.is_file():
        return target

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-t", "8",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return target


@pytest.fixture(scope="session")
def scene_clip(media_fixtures_dir: Path) -> Path:
    """Nine seconds in three visually distinct shots, changing at 3 s and 6 s.

    Colour bars, then solid red, then solid blue: unambiguous boundaries at
    known times, so a test can assert *where* they were found rather than only
    how many.
    """
    target = media_fixtures_dir / "scenes.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3",
            "-f", "lavfi", "-i", "color=c=red:size=320x240:rate=30:duration=3",
            "-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=30:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=9",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-map", "3:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
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


def workers_through(stage) -> dict:
    """The default workers, stopping after ``stage``.

    A phase's integration test should exercise that phase, not every phase
    after it. Once RENDER gained a worker, ``run_project`` in a Phase 4 vision
    test began encoding an MP4 -- minutes of CPU to prove something about
    keyframes. Limiting the registry restores both the runtime and the meaning:
    the runner stops at the first stage this file does not care about, and
    :func:`assert_frontier_waits` sees it waiting.
    """
    from backend.core.models.enums import JobStage
    from backend.core.models.jobs import stages_in_order

    order = list(stages_in_order())
    limit = order.index(JobStage(stage))
    keep = set(order[: limit + 1])
    return {name: worker for name, worker in default_workers().items() if name in keep}


def assert_frontier_waits(runner, project_id: str) -> None:
    """The runner stops cleanly: nothing is left running, and nothing failed.

    A helper rather than an assertion each phase rewrites: naming the frontier
    stage means every landed phase breaks the previous phase's test, which
    happened four times before this existed.

    It covers both eras on purpose. While a stage had no worker, the property
    was "an unimplemented stage waits, it does not fail". Now that every queued
    stage has one, the property is "the run finished and nothing failed" --
    which is what each caller was really asserting all along.
    """
    from backend.core.models.enums import JobStatus

    assert runner.run_next(project_id) is None, "the runner still had work to do"
    jobs = runner.jobs.list_jobs(project_id)

    failed = [job for job in jobs if job.status is JobStatus.FAILED]
    assert not failed, [f"{job.stage.value}: {job.error_message}" for job in failed]

    frontier = next(
        (job for job in jobs if job.stage not in runner.supported_stages), None
    )
    if frontier is not None:
        assert frontier.status is JobStatus.QUEUED
        assert frontier.error_code is None
    else:
        # Every queued stage ran. Delivery is excluded by design (§46: publishing
        # is always an explicit action), so there is nothing left waiting.
        assert all(job.status is JobStatus.COMPLETED for job in jobs)


@pytest.fixture
def frontier_check():
    """Return :func:`assert_frontier_waits`, handed over rather than imported."""
    return assert_frontier_waits


@pytest.fixture
def workers_up_to():
    """Return :func:`workers_through` for tests that stop at their own phase."""
    return workers_through


@pytest.fixture
def vision_provider() -> FakeVisionProvider:
    """A deterministic vision provider.

    It counts the frames it was handed, which is how the §15 acceptance test
    proves the cascade's ceiling holds: the number of frames that reach a
    vision model is the number this one saw.
    """
    return FakeVisionProvider()


@pytest.fixture
def pipeline_runner(
    database: Database,
    paths: Paths,
    config: AppConfig,
    speech_provider: FakeSpeechProvider,
    vision_provider: FakeVisionProvider,
) -> PipelineRunner:
    """A runner whose model-backed stages use test doubles.

    Stops at VISION: its users are the media-engine and vision phases, and a
    registry that reached RENDER would have those tests encoding an MP4 to
    prove something about frame extraction.
    """
    workers = workers_through(JobStage.VISION)
    workers[JobStage.TRANSCRIPT] = TranscriptWorker(speech_provider)
    workers[JobStage.VISION] = VisionWorker(vision_provider)
    return PipelineRunner(database, paths, config, workers=workers)
