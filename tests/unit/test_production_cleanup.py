"""A produced video's pictures are removed, and nothing reads the empty folder silently.

The owner's request (2026-10-20): once a video is made, delete its pictures so
nothing accumulates. The frames, the scene previews and the post-render
critic's frames go; the rows, the renders and every read taken from the
pictures stay. A stage that would read the pictures again fails by name rather
than completing over nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config.paths import project_paths
from backend.core.errors import AnalysisError
from backend.media.cleanup import (
    CLEARED_MARKER,
    clear_production_images,
    frames_cleared,
    mark_frames_present,
    require_frames,
)


def _picture(path: Path, size: int = 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def produced(tmp_path: Path):
    paths = project_paths(tmp_path, "proj-aaaaaaaaaaaa")
    _picture(paths.frames / "media-a" / "base" / "000001.jpg")
    _picture(paths.frames / "media-a" / "candidate" / "t00000001_250.jpg")
    _picture(paths.previews / "media-a" / "scenes" / "scene-0001.jpg")
    _picture(paths.analysis / "critic2-frames" / "f-0001.jpg")
    kept = [
        _picture(paths.renders / "rnd-aaaaaaaaaaaa.mp4"),
        _picture(paths.analysis / "report.json"),
        _picture(paths.assets / "thumbnail.jpg"),
        _picture(paths.proxy / "media-a.mp4"),
    ]
    return paths, kept


def test_cleanup_removes_every_picture_of_the_production(produced) -> None:
    paths, _kept = produced

    summary = clear_production_images(paths)

    assert summary["files"] == 4
    assert summary["bytes"] == 400
    assert not any(p.is_file() for p in paths.frames.rglob("*.jpg"))
    assert not any(p.is_file() for p in paths.previews.rglob("*"))
    assert not any((paths.analysis / "critic2-frames").rglob("*"))


def test_cleanup_keeps_the_video_its_thumbnail_and_everything_else(produced) -> None:
    paths, kept = produced

    clear_production_images(paths)

    assert all(p.is_file() for p in kept)


def test_cleanup_leaves_a_marker_a_later_stage_can_read(produced) -> None:
    paths, _kept = produced
    assert not frames_cleared(paths)

    clear_production_images(paths)

    assert frames_cleared(paths)
    assert (paths.frames / CLEARED_MARKER).is_file()


def test_cleanup_twice_is_harmless(produced) -> None:
    paths, _kept = produced
    clear_production_images(paths)

    again = clear_production_images(paths)

    assert again["files"] == 0
    assert frames_cleared(paths)


def test_a_stage_that_reads_frames_fails_by_name_after_cleanup(produced) -> None:
    paths, _kept = produced
    require_frames(paths, "ocr")  # pictures present: nothing to say

    clear_production_images(paths)

    with pytest.raises(AnalysisError, match="Re-run the FRAMES stage first"):
        require_frames(paths, "ocr")


def test_frames_extracted_again_lift_the_cleanup(produced) -> None:
    paths, _kept = produced
    clear_production_images(paths)

    mark_frames_present(paths)

    assert not frames_cleared(paths)
    require_frames(paths, "vision")


def _context(config, paths, *, clear: bool):
    sampling = config.analysis.frame_sampling.model_copy(update={"clear_after_production": clear})
    analysis = config.analysis.model_copy(update={"frame_sampling": sampling})
    return SimpleNamespace(config=config.model_copy(update={"analysis": analysis}), paths=paths)


def test_critic2_clears_the_pictures_when_the_video_is_produced(
    config, produced, monkeypatch
) -> None:
    from backend.pipeline.workers.critic2_worker import Critic2Worker

    paths, _kept = produced
    monkeypatch.setattr(Critic2Worker, "_run", lambda self, context: {"revision": 0})

    result = Critic2Worker().run(_context(config, paths, clear=True))

    assert result["revision"] == 0
    assert result["images_cleared"]["files"] == 4
    assert frames_cleared(paths)


def test_critic2_keeps_the_pictures_when_the_switch_is_off(config, produced, monkeypatch) -> None:
    from backend.pipeline.workers.critic2_worker import Critic2Worker

    paths, _kept = produced
    monkeypatch.setattr(Critic2Worker, "_run", lambda self, context: {"revision": 0})

    result = Critic2Worker().run(_context(config, paths, clear=False))

    assert "images_cleared" not in result
    assert (paths.frames / "media-a" / "base" / "000001.jpg").is_file()


def test_a_re_edit_after_cleanup_says_its_menu_check_did_not_run(config, produced) -> None:
    from backend.pipeline.workers.edl_worker import EdlWorker

    paths, _kept = produced
    clear_production_images(paths)
    reads = config.narrative.planned_frame_reads.model_copy(update={"enabled": True})
    narrative = config.narrative.model_copy(update={"planned_frame_reads": reads})
    context = SimpleNamespace(
        config=config.model_copy(update={"narrative": narrative}), paths=paths
    )

    summary = EdlWorker()._read_planned_frames(context, [])

    assert summary["skipped"] is True
    assert summary["frames_cleared"] is True
    assert "re-run FRAMES" in summary["reason"]
