"""Content QA and the report policy (SPEC §77, §78, §79).

These need no video at all, which is the point: every check reads analysis the
pipeline already produced, so the question each asks is about the **edit**
rather than about the footage. "Is there a menu in this recording" is nearly
always yes and tells you nothing; "did a menu end up in the video" is the check.

The report tests carry the §79 rule that matters most in practice: a finding
without a remedy has moved the problem rather than solved it.
"""

from __future__ import annotations

import pytest

from backend.config.loader import load_config
from backend.core.models.enums import QAStatus, TrackKind
from backend.gaming.profiles import GameProfile, Region
from backend.qa import content, technical
from backend.qa.report import build_report, failure, passed, warning
from backend.timeline.captions import Caption
from backend.timeline.models import Timeline, TimelineClip, Track

pytestmark = pytest.mark.unit

MEDIA = "media-aaaaaaaaaaaa"


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture
def timeline() -> Timeline:
    """Three 20-second clips, taken from 0, 100 and 200 seconds of one file."""
    clips = tuple(
        TimelineClip(
            id=f"clip-{index:012d}",
            media_id=MEDIA,
            clip_index=index,
            source_in=index * 100.0,
            source_out=index * 100.0 + 20.0,
            timeline_start=index * 20.0,
            timeline_end=index * 20.0 + 20.0,
        )
        for index in range(3)
    )
    return Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
        Track(kind=TrackKind.VIDEO, clips=clips)
    )


def _inspect(timeline, config, captions=(), **kwargs):
    return content.inspect(
        timeline,
        captions,
        content.ContentInputs(**kwargs),
        config=config.qa,
        caption_config=config.captions,
    )


def _finding(findings, check: str):
    return next(item for item in findings if item.check == check)


class TestMenusInTheEdit:
    def test_a_menu_inside_a_clip_is_flagged(self, timeline, config) -> None:
        # 105 s is inside clip 1 (100-120 s of the source).
        findings = _inspect(timeline, config, observations=[(MEDIA, 105.0, ["menu", "ui"])])

        assert _finding(findings, "accidental_menu_section").status is QAStatus.WARNING

    def test_a_menu_the_edit_left_out_is_not_flagged(self, timeline, config) -> None:
        # 60 s is between clips: the recording has a menu, the video does not.
        findings = _inspect(timeline, config, observations=[(MEDIA, 60.0, ["menu"])])

        assert _finding(findings, "accidental_menu_section").status is QAStatus.PASSED

    def test_gameplay_labels_are_not_menus(self, timeline, config) -> None:
        findings = _inspect(timeline, config, observations=[(MEDIA, 105.0, ["combat", "driving"])])

        assert _finding(findings, "accidental_menu_section").status is QAStatus.PASSED

    def test_a_label_that_merely_contains_menu_is_not_a_menu(self, timeline, config) -> None:
        findings = _inspect(timeline, config, observations=[(MEDIA, 105.0, ["menuever"])])

        assert _finding(findings, "accidental_menu_section").status is QAStatus.PASSED

    def test_a_menu_in_another_recording_is_not_matched(self, timeline, config) -> None:
        findings = _inspect(timeline, config, observations=[("media-other0000", 105.0, ["menu"])])

        assert _finding(findings, "accidental_menu_section").status is QAStatus.PASSED


class TestSilence:
    def test_a_long_silence_inside_the_edit_warns(self, timeline, config) -> None:
        limit = config.qa.content.thresholds.max_silence_seconds
        findings = _inspect(timeline, config, silences=[(MEDIA, 100.0, 100.0 + limit + 5)])

        assert _finding(findings, "extreme_silence").status is QAStatus.WARNING

    def test_only_the_part_the_edit_kept_is_counted(self, timeline, config) -> None:
        # A silence starting inside clip 1 but running long past its out point
        # is only as long as the clip shows it.
        findings = _inspect(timeline, config, silences=[(MEDIA, 115.0, 400.0)])

        # Clip 1 ends at 120 s, so only 5 s of that silence is in the video.
        assert _finding(findings, "extreme_silence").status is QAStatus.PASSED

    def test_a_silence_the_edit_skipped_is_ignored(self, timeline, config) -> None:
        findings = _inspect(timeline, config, silences=[(MEDIA, 40.0, 90.0)])

        assert _finding(findings, "extreme_silence").status is QAStatus.PASSED


class TestSequenceAndTransitions:
    def test_the_pacing_report_is_reused_rather_than_recomputed(self, timeline, config) -> None:
        # §38 already measured this; a second opinion from less information
        # would be worse.
        findings = _inspect(
            timeline, config, pacing_warnings=["8 clips of the same type run consecutively"]
        )
        finding = _finding(findings, "broken_sequence")

        assert finding.status is QAStatus.WARNING
        assert "8 clips" in finding.message

    def test_no_pacing_warnings_is_a_pass(self, timeline, config) -> None:
        assert _finding(_inspect(timeline, config), "broken_sequence").status is QAStatus.PASSED

    def test_a_clip_too_short_to_read_warns(self, config) -> None:
        flash = TimelineClip(
            id="clip-000000000000",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=0.4,
            timeline_start=0.0,
            timeline_end=0.4,
        )
        timeline = Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
            Track(kind=TrackKind.VIDEO, clips=(flash,))
        )

        assert _finding(_inspect(timeline, config), "bad_transition").status is QAStatus.WARNING

    def test_normal_clips_pass(self, timeline, config) -> None:
        assert _finding(_inspect(timeline, config), "bad_transition").status is QAStatus.PASSED


class TestCaptionsOverHud:
    def _caption(self) -> Caption:
        return Caption(
            id="cap-000000000000",
            index=0,
            timeline_start=1.0,
            timeline_end=2.0,
            text="no way",
        )

    def test_a_hud_element_under_the_caption_band_warns(self, timeline, config) -> None:
        # A scoreboard along the bottom is exactly where captions go.
        profile = GameProfile(
            id="testgame",
            regions={"scoreboard": Region(x=0.1, y=0.85, width=0.8, height=0.12)},
        )
        findings = _inspect(timeline, config, captions=[self._caption()], profile=profile)
        finding = _finding(findings, "caption_covers_hud")

        assert finding.status is QAStatus.WARNING
        assert "scoreboard" in finding.message

    def test_a_hud_element_elsewhere_is_fine(self, timeline, config) -> None:
        profile = GameProfile(
            id="testgame",
            regions={"kill_feed": Region(x=0.7, y=0.05, width=0.28, height=0.2)},
        )
        findings = _inspect(timeline, config, captions=[self._caption()], profile=profile)

        assert _finding(findings, "caption_covers_hud").status is QAStatus.PASSED

    def test_the_generic_profile_declares_nothing_to_collide_with(self, timeline, config) -> None:
        # §23: an unknown game has no regions, and saying "nothing is known"
        # beats silence.
        findings = _inspect(timeline, config, captions=[self._caption()], profile=None)
        finding = _finding(findings, "caption_covers_hud")

        assert finding.status is QAStatus.PASSED
        assert "no HUD regions" in finding.message


class TestReportPolicy:
    def test_a_technical_failure_blocks_and_needs_review(self, config) -> None:
        report = build_report([failure("duration", "wrong", remedy="re-render")], config.qa)

        assert report.status is QAStatus.FAILED
        assert report.blocks_export
        assert report.needs_review

    def test_content_warnings_do_not_block(self, config) -> None:
        report = build_report([warning("extreme_silence", "long", remedy="trim it")], config.qa)

        assert report.status is QAStatus.WARNING
        assert not report.blocks_export

    def test_only_passes_is_a_pass(self, config) -> None:
        report = build_report([passed("duration", "fine")], config.qa)

        assert report.status is QAStatus.PASSED
        assert not report.blocks_export
        assert not report.needs_review

    def test_enough_warnings_asks_for_review(self, config) -> None:
        # A video with eight small oddities is more likely wrong than one with
        # two, even when no single warning is serious.
        limit = config.qa.policy.max_warnings_before_review
        report = build_report(
            [warning(f"check{index}", "odd", remedy="look") for index in range(limit + 1)],
            config.qa,
        )

        assert report.needs_review

    def test_the_explanation_pairs_each_problem_with_its_remedy(self, config) -> None:
        report = build_report(
            [
                passed("duration", "fine"),
                failure("black_frames", "3s of black", remedy="check the timeline"),
            ],
            config.qa,
        )
        lines = report.explain()

        assert len(lines) == 2, "a pass should not appear in the explanation"
        assert lines[1].strip().startswith("→")

    def test_a_disabled_check_list_still_runs_unknown_checks(self) -> None:
        # A new check should take effect on upgrade rather than wait for
        # someone to notice it needs enabling.
        from backend.qa.report import enabled_checks

        assert enabled_checks(["old", "new"], {"old": False}) == ["new"]


class TestReadingFreezeRunsBack:
    """§76: what `freezedetect` reports and what "the picture stopped" means.

    These need no video: the filter's output is text, and the bug they pin was
    in reading it.
    """

    #: What ffmpeg actually printed for the recording behind *Ziad 2* at
    #: 2438.47s, at the noise floor `config/qa.yaml` sets. The stretch is one
    #: dialogue scene -- an NPC talking while the player stands still -- and
    #: the filter closes a run the instant a single frame differs, then opens
    #: the next at the same timestamp.
    REAL_DIALOGUE_SCENE = """[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_start: 0.013
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_duration: 2.167
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_end: 2.18
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_start: 2.18
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_duration: 2.783
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_end: 4.963
"""

    def test_back_to_back_runs_are_one_still_stretch(self) -> None:
        runs = technical._parse_freeze(self.REAL_DIALOGUE_SCENE, 5.2)

        assert len(runs) == 1, "a scene that never moved was read as two"
        start, duration = runs[0]
        assert start == pytest.approx(0.013)
        assert duration == pytest.approx(4.95, abs=0.01)

    def test_the_longest_run_now_clears_the_limit(self) -> None:
        # The verdict this decides. Unmerged, the longest run was 2.783s --
        # under config/qa.yaml's 3.0s limit -- so the source read as *moving*
        # and a faithful render of a cutscene was blocked from export.
        limit = load_config().qa.technical.thresholds.max_frozen_run_seconds
        longest = max(
            duration for _, duration in technical._parse_freeze(self.REAL_DIALOGUE_SCENE, 5.2)
        )

        assert longest > limit

    def test_real_movement_still_separates_two_stretches(self) -> None:
        # The merge must not swallow a gap a viewer would see. A second of
        # motion between two still stretches is two still stretches.
        stderr = """\
lavfi.freezedetect.freeze_start: 0.0
lavfi.freezedetect.freeze_duration: 1.0
lavfi.freezedetect.freeze_start: 2.0
lavfi.freezedetect.freeze_duration: 1.0
"""

        runs = technical._parse_freeze(stderr, 4.0)

        assert runs == ((0.0, 1.0), (2.0, 1.0))

    def test_a_freeze_running_to_the_end_is_still_closed(self) -> None:
        # The entire-video-frozen case: a start with no duration, which the
        # merge must not drop.
        stderr = "lavfi.freezedetect.freeze_start: 1.5"

        assert technical._parse_freeze(stderr, 10.0) == ((1.5, 8.5),)


class TestDoctrineSummary:
    """docs/DIRECTION.md §34: one readable score, and the honest list."""

    def test_the_score_is_arithmetic_over_what_qa_measured(
        self, database, paths, config
    ) -> None:
        from types import SimpleNamespace

        from backend.pipeline.workers.qa_worker import QaWorker

        context = SimpleNamespace(database=database, project_id="proj-none")
        report = SimpleNamespace(
            failures=("frozen frames",),
            warnings=("black stretch", "loud peak"),
        )

        quality, uncertainties = QaWorker()._doctrine_summary(context, report)

        # 100 - 25x1 - 6x2, and no stage notes for a project with no jobs.
        assert quality == 63
        assert uncertainties == ["[warning] black stretch", "[warning] loud peak"]

    def test_a_clean_report_scores_a_hundred(self, database) -> None:
        from types import SimpleNamespace

        from backend.pipeline.workers.qa_worker import QaWorker

        context = SimpleNamespace(database=database, project_id="proj-none")
        report = SimpleNamespace(failures=(), warnings=())

        quality, uncertainties = QaWorker()._doctrine_summary(context, report)

        assert quality == 100
        assert uncertainties == []
