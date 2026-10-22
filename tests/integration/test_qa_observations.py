"""Render-level observations: measured and recorded, never a verdict.

Four things QA claimed or needed and did not do: silencedetect was documented
but never in the graph; the rendered duration was compared with a probe of the
same file, never with the edit; true peak was requested from ebur128 and never
read; and a loudness that could not be measured left no finding at all. A
fifth is new: whether any cut lands inside a spoken word.

These are OBSERVE-ONLY. There is no threshold yet -- the raw values are
recorded so thresholds can be set from data -- so none of them may warn,
fail, block or change the verdicts of the checks that already existed.

Acceptance (item 4):
* silencedetect runs and its runs are recorded;
* the rendered duration is compared with ``timeline.duration``;
* true peak is parsed;
* loudness that cannot be measured is a NOT_MEASURED finding;
* a join audit reports cuts that land inside a stored word;
* thresholds are TBD-from-data: nothing observed blocks or warns.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.config.loader import load_config
from backend.core.models.enums import QAStatus, TrackKind
from backend.media.ffmpeg import FFmpegRunner
from backend.qa import content, technical
from backend.qa.report import build_report
from backend.timeline.models import Timeline, TimelineClip, Track

WIDTH, HEIGHT, FPS, SECONDS = 320, 240, 15, 6
OBSERVED = ("silence", "timeline_duration")

#: A 440 Hz tone with true silence from 1.0s to 4.0s.
GAPPED = f"aevalsrc='if(between(t,1,4),0,0.5*sin(2*PI*440*t))':s=48000:d={SECONDS}"


def _make(target: Path, audio: str | None) -> Path:
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    argv += ["-f", "lavfi", "-i", f"testsrc=s={WIDTH}x{HEIGHT}:r={FPS}:d={SECONDS}"]
    if audio is not None:
        argv += ["-f", "lavfi", "-i", audio]
    argv += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    argv += ["-c:a", "aac", "-shortest"] if audio is not None else ["-an"]
    subprocess.run([*argv, str(target)], check=True, capture_output=True, timeout=300)
    return target


@pytest.fixture(scope="module")
def qa_config():
    return load_config()


@pytest.fixture(scope="module")
def runner(qa_config):
    return FFmpegRunner(qa_config.ffmpeg)


@pytest.fixture(scope="module")
def files(tmp_path_factory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("observe")
    return {
        "gapped": _make(directory / "gapped.mp4", GAPPED),
        "no_audio": _make(directory / "no_audio.mp4", None),
    }


def _expected(timeline_seconds: float | None) -> technical.Expected:
    return technical.Expected(
        duration_seconds=float(SECONDS),
        width=WIDTH,
        height=HEIGHT,
        fps=float(FPS),
        timeline_seconds=timeline_seconds,
    )


def _finding(findings, check):
    return next((item for item in findings if item.check == check), None)


@pytest.mark.integration
@pytest.mark.requires_ffmpeg
class TestRenderObservations:
    def test_silencedetect_runs(self, files, runner, qa_config) -> None:
        measured = technical.decode(files["gapped"], runner, qa_config.qa, total_seconds=SECONDS)

        assert len(measured.silence_runs) == 1
        start, end = measured.silence_runs[0]
        assert start == pytest.approx(1.0, abs=0.2)
        assert end == pytest.approx(4.0, abs=0.2)

        finding = _finding(
            technical.inspect(files["gapped"], _expected(6.0), runner=runner, config=qa_config.qa),
            "silence",
        )
        assert finding.category == "observation"
        assert finding.measured["runs"] == [[round(start, 3), round(end, 3)]]
        assert "TBD" in finding.message

    def test_true_peak_is_parsed(self, files, runner, qa_config) -> None:
        measured = technical.decode(files["gapped"], runner, qa_config.qa, total_seconds=SECONDS)

        # A 0.5-amplitude tone, which the AAC encode lifts a few dB: a real
        # figure below full scale, not a parse of something else.
        assert measured.true_peak_dbfs is not None
        assert -10.0 < measured.true_peak_dbfs < 0.0
        findings = technical.inspect(
            files["gapped"], _expected(6.0), runner=runner, config=qa_config.qa
        )
        assert _finding(findings, "loudness").measured["true_peak_dbfs"] == pytest.approx(
            measured.true_peak_dbfs, abs=0.01
        )

    def test_the_render_is_compared_with_the_timeline(self, files, runner, qa_config) -> None:
        findings = technical.inspect(
            files["gapped"], _expected(10.0), runner=runner, config=qa_config.qa
        )

        observed = _finding(findings, "timeline_duration")
        assert observed.category == "observation"
        assert observed.measured["timeline_seconds"] == 10.0
        assert observed.measured["drift_seconds"] == pytest.approx(4.0, abs=0.2)
        # Observe only: four seconds out is recorded, not judged.
        assert observed.status is QAStatus.PASSED
        # The existing check is untouched: it still compares with the render row.
        assert _finding(findings, "duration").status is QAStatus.PASSED

    def test_no_timeline_duration_is_not_measured(self, files, runner, qa_config) -> None:
        findings = technical.inspect(
            files["gapped"], _expected(None), runner=runner, config=qa_config.qa
        )

        assert _finding(findings, "timeline_duration").status is QAStatus.NOT_MEASURED

    def test_unmeasurable_loudness_is_not_measured(self, files, runner, qa_config) -> None:
        findings = technical.inspect(
            files["no_audio"], _expected(6.0), runner=runner, config=qa_config.qa
        )

        loudness = _finding(findings, "loudness")
        assert loudness is not None
        assert loudness.status is QAStatus.NOT_MEASURED
        assert loudness.remedy
        assert _finding(findings, "silence").status is QAStatus.NOT_MEASURED

    def test_observations_never_change_the_verdict(self, files, runner, qa_config) -> None:
        for name in files:
            findings = technical.inspect(
                files[name], _expected(60.0), runner=runner, config=qa_config.qa
            )
            observed = [item for item in findings if item.check in OBSERVED]
            assert observed
            assert not any(item.failed or item.warned for item in observed)

            judged = [item for item in findings if item.check not in OBSERVED]
            with_them = build_report(findings, qa_config.qa)
            without = build_report(judged, qa_config.qa)
            assert (with_them.status, with_them.blocks_export, with_them.needs_review) == (
                without.status,
                without.blocks_export,
                without.needs_review,
            )


@pytest.mark.unit
class TestParsing:
    def test_an_unterminated_silence_runs_to_the_end(self) -> None:
        stderr = (
            "[silencedetect @ 0x1] silence_start: 2.5\n"
            "[silencedetect @ 0x1] silence_end: 3.25 | silence_duration: 0.75\n"
            "[silencedetect @ 0x1] silence_start: 8\n"
        )

        assert technical._parse_silence(stderr, 10.0) == ((2.5, 3.25), (8.0, 10.0))

    def test_true_peak_is_the_summary_figure(self) -> None:
        stderr = (
            "[Parsed_ebur128_0 @ 0x1] t: 1 TPK: -7.1 -7.2 dBFS\n"
            "  True peak:\n"
            "    Peak:       -5.9 dBFS\n"
        )

        assert technical._parse_true_peak(stderr) == -5.9

    def test_no_peak_figure_is_none(self) -> None:
        assert technical._parse_true_peak("nothing here") is None


@pytest.mark.unit
class TestJoinAudit:
    MEDIA = "media-aaaaaaaaaaaa"

    def _timeline(self, *spans) -> Timeline:
        clips = []
        at = 0.0
        for index, (start, end) in enumerate(spans):
            clips.append(
                TimelineClip(
                    id=f"clip-{index:012d}",
                    media_id=self.MEDIA,
                    clip_index=index,
                    source_in=start,
                    source_out=end,
                    timeline_start=at,
                    timeline_end=at + end - start,
                )
            )
            at += end - start
        return Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
            Track(kind=TrackKind.VIDEO, clips=tuple(clips))
        )

    def test_a_cut_inside_a_word_is_reported(self) -> None:
        timeline = self._timeline((10.0, 20.0), (30.0, 40.0))
        words = {self.MEDIA: [(19.8, 20.3, "incredible"), (29.0, 29.5, "and")]}

        finding = content.cut_word_audit(timeline, words)

        assert finding.status is QAStatus.PASSED
        assert finding.category == "observation"
        assert finding.measured["cuts"] == 4
        assert finding.measured["inside_words"] == 1
        assert finding.measured["examples"] == [
            {"clip": 0, "edge": "out", "at": 20.0, "word": "incredible"}
        ]
        assert "TBD" in finding.message

    def test_a_cut_on_a_word_boundary_is_not_inside_it(self) -> None:
        timeline = self._timeline((10.0, 20.0))
        words = {self.MEDIA: [(9.5, 10.0, "go"), (20.0, 20.4, "now")]}

        assert content.cut_word_audit(timeline, words).measured["inside_words"] == 0

    def test_no_stored_words_is_not_measured(self) -> None:
        timeline = self._timeline((10.0, 20.0))

        finding = content.cut_word_audit(timeline, {})

        assert finding.status is QAStatus.NOT_MEASURED
        assert finding.remedy


@pytest.mark.integration
class TestTheWorkerReadsStoredWords:
    """The QA stage feeds the audit from the transcript table, per recording."""

    def test_stored_words_reach_the_audit(self, database, project_manager) -> None:
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from ai.providers.base import TranscriptSegment, TranscriptWord
        from backend.core.ids import new_id
        from backend.core.models.enums import VideoMode
        from backend.core.models.media import Media
        from backend.core.models.project import ProjectCreate
        from backend.database.repositories.media import MediaRepository
        from backend.database.repositories.transcript import TranscriptRepository
        from backend.pipeline.workers.qa_worker import QaWorker

        project_id = project_manager.create(
            ProjectCreate(name="Words", target_duration_seconds=600, mode=VideoMode.STORY)
        ).id
        now = datetime.now(timezone.utc)
        media_id = (
            MediaRepository(database)
            .create(
                Media(
                    id=new_id("media"),
                    project_id=project_id,
                    source_path="/recordings/talk.mp4",
                    filename="talk.mp4",
                    container=".mp4",
                    size_bytes=1024,
                    checksum="1" * 64,
                    created_at=now,
                    updated_at=now,
                )
            )
            .id
        )
        TranscriptRepository(database).replace_for_media(
            project_id,
            media_id,
            [
                TranscriptSegment(
                    start=19.0,
                    end=21.0,
                    text="that was incredible",
                    words=(TranscriptWord(word="incredible", start=19.8, end=20.3),),
                )
            ],
        )
        timeline = Timeline(project_id=project_id).with_track(
            Track(
                kind=TrackKind.VIDEO,
                clips=(
                    TimelineClip(
                        id="clip-000000000000",
                        media_id=media_id,
                        clip_index=0,
                        source_in=10.0,
                        source_out=20.0,
                        timeline_start=0.0,
                        timeline_end=10.0,
                    ),
                ),
            )
        )
        context = SimpleNamespace(database=database, project_id=project_id)

        words = QaWorker()._spoken_words(context, timeline)

        assert words == {media_id: [(19.8, 20.3, "incredible")]}
        assert content.cut_word_audit(timeline, words).measured["inside_words"] == 1
