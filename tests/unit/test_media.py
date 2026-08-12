"""Media engine logic that needs no FFmpeg (Phase 2).

Chunking, sampling budgets and output parsing are pure functions, and they are
where the expensive mistakes live: an interval that silently truncates coverage
or a frame rate rounded from 59.94 produces a video that is wrong rather than
broken, which is far harder to notice.
"""

from __future__ import annotations

import math

import pytest

from backend.config.schema import FrameSamplingConfig
from backend.core.errors import ErrorCode, MediaError, ValidationError
from backend.media.chunking import MERGE_REMAINDER_FRACTION, plan_chunks, total_work_seconds
from backend.media.ffmpeg import (
    STDERR_TAIL_LINES,
    CancelledError,
    FFmpegRunner,
    _consume_progress_line,
    format_seconds,
    parse_rational,
    parse_timestamp,
    progress_arguments,
    tail,
)
from backend.media.frames import (
    SECONDS_PER_HOUR,
    merge_regions,
    plan_base_sampling,
    plan_candidate_sampling,
)

#: §7 lists the source lengths the system must handle.
LONG_SOURCE_HOURS = (0.5, 1, 2, 4, 6, 8)


class TestRationalParsing:
    """ffprobe reports frame rates as exact rationals for a reason."""

    def test_ntsc_rate_is_not_rounded(self) -> None:
        assert parse_rational("60000/1001") == pytest.approx(59.94005994, rel=1e-9)
        assert parse_rational("30000/1001") == pytest.approx(29.97002997, rel=1e-9)

    def test_unknown_rate_is_none_not_zero(self) -> None:
        # 0/0 is ffprobe's "I don't know". Zero would make every derived
        # interval a division by zero; None makes the caller decide.
        assert parse_rational("0/0") is None
        assert parse_rational("N/A") is None
        assert parse_rational(None) is None
        assert parse_rational("") is None

    def test_integer_and_decimal_rates(self) -> None:
        assert parse_rational("60/1") == 60.0
        assert parse_rational("30") == 30.0

    def test_malformed_input_does_not_raise(self) -> None:
        assert parse_rational("sixty") is None
        assert parse_rational("60/") is None
        assert parse_rational("1/0") is None

    def test_drift_over_two_hours_is_material(self) -> None:
        # The reason this matters: rounding 59.94 to 60 loses seven seconds of
        # alignment over a two-hour recording.
        exact = parse_rational("60000/1001")
        assert exact is not None
        frames = 7200 * exact
        assert abs(frames / 60.0 - 7200) > 7.0


class TestTimestampParsing:
    def test_clock_and_seconds_forms(self) -> None:
        assert parse_timestamp("01:02:03.5") == pytest.approx(3723.5)
        assert parse_timestamp("00:00:05.000000") == pytest.approx(5.0)
        assert parse_timestamp("12:30") == pytest.approx(750.0)
        assert parse_timestamp("12.5") == pytest.approx(12.5)

    def test_missing_values(self) -> None:
        assert parse_timestamp("N/A") is None
        assert parse_timestamp(None) is None

    def test_format_seconds_is_precise_and_non_negative(self) -> None:
        assert format_seconds(12.3456789) == "12.345679"
        assert format_seconds(-5) == "0.000000"


class TestProgressParsing:
    def test_block_becomes_a_report(self) -> None:
        block: dict[str, str] = {}
        report = None
        for line in ("frame=120", "fps=25.0", "out_time_us=5000000",
                     "speed=2.5x", "progress=continue"):
            report = _consume_progress_line(line, block, 10.0)
        assert report is not None
        assert report.out_time_seconds == pytest.approx(5.0)
        assert report.fraction == pytest.approx(0.5)
        assert report.frame == 120
        assert report.speed == pytest.approx(2.5)
        assert not report.finished

    def test_partial_block_reports_nothing(self) -> None:
        block: dict[str, str] = {}
        assert _consume_progress_line("frame=10", block, 10.0) is None
        assert block["frame"] == "10"

    def test_end_block_is_complete(self) -> None:
        block: dict[str, str] = {}
        _consume_progress_line("out_time_us=9000000", block, 10.0)
        report = _consume_progress_line("progress=end", block, 10.0)
        assert report is not None
        assert report.finished
        # Reported complete even though out_time stopped short of the total:
        # FFmpeg finishing is the authority, not the arithmetic.
        assert report.fraction == 1.0

    def test_fraction_is_none_without_a_total(self) -> None:
        # A fabricated 0.0 would show a progress bar that never moves.
        block: dict[str, str] = {}
        _consume_progress_line("out_time_us=5000000", block, None)
        report = _consume_progress_line("progress=continue", block, None)
        assert report is not None
        assert report.fraction is None

    def test_unknown_out_time_falls_back_to_the_clock_field(self) -> None:
        block: dict[str, str] = {}
        _consume_progress_line("out_time_us=N/A", block, 10.0)
        _consume_progress_line("out_time=00:00:02.500000", block, 10.0)
        report = _consume_progress_line("progress=continue", block, 10.0)
        assert report is not None
        assert report.out_time_seconds == pytest.approx(2.5)

    def test_progress_arguments_request_machine_output(self) -> None:
        assert progress_arguments() == ["-progress", "pipe:1", "-nostats"]


class TestStderrTail:
    def test_keeps_the_end_where_the_error_is(self) -> None:
        text = "\n".join(f"line {index}" for index in range(200))
        excerpt = tail(text)
        assert excerpt.endswith("line 199")
        assert len(excerpt.splitlines()) == STDERR_TAIL_LINES

    def test_character_cap_applies_to_one_long_line(self) -> None:
        assert len(tail("x" * 50_000)) <= 4000

    def test_empty_input(self) -> None:
        assert tail("") == ""


class TestBinaryResolution:
    def test_missing_binary_is_a_typed_dependency_error(self, config) -> None:
        runner = FFmpegRunner(config.ffmpeg.model_copy(update={"binary": "definitely-not-ffmpeg"}))
        with pytest.raises(Exception) as exc_info:
            _ = runner.ffmpeg_path
        assert exc_info.value.code is ErrorCode.FFMPEG_NOT_FOUND
        assert exc_info.value.recoverable is False

    def test_missing_ffprobe_reports_its_own_code(self, config) -> None:
        runner = FFmpegRunner(
            config.ffmpeg.model_copy(update={"ffprobe_binary": "definitely-not-ffprobe"})
        )
        with pytest.raises(Exception) as exc_info:
            _ = runner.ffprobe_path
        assert exc_info.value.code is ErrorCode.FFPROBE_NOT_FOUND

    def test_cancellation_is_not_a_failure(self) -> None:
        # §82: a cancelled stage leaves a consistent project, so it must never
        # be retried like a transient failure.
        error = CancelledError()
        assert error.code is ErrorCode.JOB_CANCELLED
        assert error.recoverable is False


class TestChunking:
    """§7: 30 min to 8 h, never in RAM. Chunk cores must tile exactly."""

    @pytest.mark.parametrize("hours", LONG_SOURCE_HOURS)
    def test_every_required_length_is_covered_exactly(self, hours: float) -> None:
        duration = hours * 3600
        plan = plan_chunks(duration, chunk_seconds=600, overlap_seconds=15)
        assert plan.covers()
        assert sum(chunk.core_duration for chunk in plan) == pytest.approx(duration)

    def test_every_instant_has_exactly_one_owner(self) -> None:
        plan = plan_chunks(1800, chunk_seconds=600, overlap_seconds=15)
        for timestamp in (0.0, 1.0, 599.999, 600.0, 600.001, 1200.0, 1799.9, 1800.0):
            owners = [chunk for chunk in plan if chunk.owns(timestamp)]
            assert len(owners) == 1, f"{timestamp} has {len(owners)} owners"

    def test_boundaries_read_context_from_both_sides(self) -> None:
        plan = plan_chunks(1800, chunk_seconds=600, overlap_seconds=15)
        middle = plan[1]
        assert middle.start == 585
        assert middle.end == 1215
        assert middle.lead_in == 15
        assert middle.lead_out == 15
        # The source has no context before it starts or after it ends.
        assert plan[0].lead_in == 0
        assert plan[-1].lead_out == 0

    def test_short_source_is_a_single_chunk(self) -> None:
        plan = plan_chunks(120, chunk_seconds=600, overlap_seconds=15)
        assert len(plan) == 1
        assert plan[0].start == 0
        assert plan[0].end == 120

    def test_short_remainder_is_merged_rather_than_left_alone(self) -> None:
        # Ten minutes plus four seconds is one unit of work, not two.
        plan = plan_chunks(604, chunk_seconds=600, overlap_seconds=15)
        assert len(plan) == 1
        assert plan[0].core_end == 604
        assert plan.covers()

    def test_a_substantial_remainder_keeps_its_own_chunk(self) -> None:
        remainder = 600 * MERGE_REMAINDER_FRACTION + 60
        plan = plan_chunks(600 + remainder, chunk_seconds=600, overlap_seconds=15)
        assert len(plan) == 2
        assert plan.covers()

    def test_overlap_costs_extra_reading(self) -> None:
        plan = plan_chunks(3600, chunk_seconds=600, overlap_seconds=15)
        # Six chunks with overlap on ten interior edges.
        assert total_work_seconds(plan.chunks) == pytest.approx(3600 + 10 * 15)

    def test_near_boundary_flags_only_the_risky_timestamps(self) -> None:
        plan = plan_chunks(1800, chunk_seconds=600, overlap_seconds=15,
                           event_overlap_seconds=5)
        assert plan.near_boundary(600.0)
        assert plan.near_boundary(603.0)
        assert not plan.near_boundary(300.0)
        # The end of the source is not a boundary between chunks.
        assert not plan.near_boundary(1800.0)

    def test_unknown_duration_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            plan_chunks(0, chunk_seconds=600)
        with pytest.raises(ValidationError):
            plan_chunks(-1, chunk_seconds=600)

    def test_overlap_may_not_swallow_the_chunk(self) -> None:
        with pytest.raises(ValidationError):
            plan_chunks(3600, chunk_seconds=600, overlap_seconds=600)


class TestFrameSamplingBudget:
    """§16: sampling density is a budget, and the budget widens the interval."""

    @staticmethod
    def _config(*, base_interval_seconds: float = 3.0, **overrides) -> FrameSamplingConfig:
        """Build a sampling configuration around a chosen base interval.

        The denser levels are derived from the base rather than fixed, because
        the configuration layer enforces ``base > high_activity >= candidate``
        (§16). At the default 3 s this reproduces the shipped values exactly.
        """
        defaults = {
            "base_interval_seconds": base_interval_seconds,
            "high_activity_interval_seconds": base_interval_seconds / 3,
            "candidate_interval_seconds": base_interval_seconds / 6,
            "candidate_pre_roll_seconds": 20.0,
            "candidate_post_roll_seconds": 20.0,
            "max_frames_per_hour": 1800,
            "max_frames_per_candidate": 40,
        }
        return FrameSamplingConfig(**{**defaults, **overrides})

    def test_shipped_defaults_stay_inside_the_budget(self) -> None:
        plan = plan_base_sampling(7200, self._config())
        assert plan.interval_seconds == 3.0
        assert not plan.was_capped
        assert plan.count == 2400

    def test_a_tight_interval_is_widened_not_truncated(self) -> None:
        # The trap: truncating the list would sample the first forty minutes of
        # a two-hour recording and abandon the rest.
        plan = plan_base_sampling(7200, self._config(base_interval_seconds=0.5))
        assert plan.was_capped
        assert plan.interval_seconds == pytest.approx(2.0)
        assert plan.timestamps[-1] > 7000, "coverage must reach the end of the source"

    @pytest.mark.parametrize("hours", LONG_SOURCE_HOURS)
    def test_the_hourly_ceiling_holds_at_every_length(self, hours: float) -> None:
        config = self._config(base_interval_seconds=0.25)
        plan = plan_base_sampling(hours * 3600, config)
        allowed = config.max_frames_per_hour * hours
        assert plan.count <= math.ceil(allowed) + 1

    def test_coverage_stays_uniform_when_capped(self) -> None:
        plan = plan_base_sampling(3600, self._config(base_interval_seconds=0.25))
        gaps = [
            round(later - earlier, 6)
            for earlier, later in zip(plan.timestamps, plan.timestamps[1:], strict=False)
        ]
        assert len(set(gaps)) == 1

    def test_a_generous_budget_leaves_the_interval_alone(self) -> None:
        plan = plan_base_sampling(3600, self._config(max_frames_per_hour=100_000))
        assert plan.interval_seconds == 3.0
        assert not plan.was_capped

    def test_zero_duration_plans_nothing(self) -> None:
        assert plan_base_sampling(0, self._config()).count == 0

    def test_budget_arithmetic_matches_the_configured_ceiling(self) -> None:
        config = self._config(base_interval_seconds=0.1, max_frames_per_hour=600)
        plan = plan_base_sampling(3600, config)
        assert plan.interval_seconds == pytest.approx(SECONDS_PER_HOUR / 600)


class TestCandidateSampling:
    @staticmethod
    def _config(**overrides) -> FrameSamplingConfig:
        return TestFrameSamplingBudget._config(**overrides)

    def test_pre_roll_and_post_roll_widen_the_region(self) -> None:
        plan = plan_candidate_sampling(100, 110, self._config(), duration_seconds=7200)
        assert plan.timestamps[0] == pytest.approx(80.0)
        assert plan.timestamps[-1] <= 130.0

    def test_the_region_is_clamped_to_the_source(self) -> None:
        plan = plan_candidate_sampling(2, 5, self._config(), duration_seconds=20)
        assert plan.timestamps[0] == 0.0
        assert plan.timestamps[-1] < 20.0

    def test_the_per_candidate_cap_widens_rather_than_truncates(self) -> None:
        plan = plan_candidate_sampling(100, 110, self._config(), duration_seconds=7200)
        assert plan.count <= 40
        assert plan.was_capped
        # The post-roll end of the region is still sampled.
        assert plan.timestamps[-1] > 125

    def test_high_activity_uses_the_middle_interval(self) -> None:
        plan = plan_candidate_sampling(
            100, 101, self._config(), duration_seconds=7200, high_activity=True
        )
        assert plan.level == "high_activity"
        assert plan.requested_interval_seconds == 1.0

    def test_an_empty_region_plans_nothing(self) -> None:
        config = self._config(candidate_pre_roll_seconds=0, candidate_post_roll_seconds=0)
        assert plan_candidate_sampling(50, 50, config).count == 0


class TestRegionMerging:
    def test_overlapping_regions_become_one(self) -> None:
        assert merge_regions([(0, 10), (5, 20)]) == [(0.0, 20.0)]

    def test_a_gap_within_tolerance_is_bridged(self) -> None:
        assert merge_regions([(0, 10), (13, 20)], gap_seconds=5) == [(0.0, 20.0)]

    def test_a_real_gap_is_preserved(self) -> None:
        assert merge_regions([(0, 10), (30, 40)], gap_seconds=5) == [(0.0, 10.0), (30.0, 40.0)]

    def test_unordered_input_is_sorted(self) -> None:
        assert merge_regions([(30, 40), (0, 10)]) == [(0.0, 10.0), (30.0, 40.0)]

    def test_degenerate_regions_are_dropped(self) -> None:
        assert merge_regions([(10, 10), (5, 4)]) == []


class TestEightHourSourceIsBounded:
    """§7 as an arithmetic guarantee, before any file is opened.

    The eight-hour case is the one the design has to survive, and these numbers
    are what stop it from being discovered on a real recording.
    """

    def test_chunk_count_stays_small(self) -> None:
        plan = plan_chunks(8 * 3600, chunk_seconds=600, overlap_seconds=15)
        assert len(plan) == 48
        assert all(chunk.duration <= 630 for chunk in plan)

    def test_frame_count_stays_within_the_budget(self, config) -> None:
        plan = plan_base_sampling(8 * 3600, config.analysis.frame_sampling)
        assert plan.count == 9600
        assert plan.count <= config.analysis.frame_sampling.max_frames_per_hour * 8

    def test_no_single_chunk_is_longer_than_configured(self, config) -> None:
        analysis = config.analysis
        plan = plan_chunks(
            8 * 3600,
            chunk_seconds=analysis.chunk_seconds,
            overlap_seconds=analysis.chunk_overlap_seconds,
        )
        limit = analysis.chunk_seconds + 2 * analysis.chunk_overlap_seconds
        assert max(chunk.duration for chunk in plan) <= limit


class TestErrorTyping:
    def test_media_errors_carry_a_code_and_details(self) -> None:
        error = MediaError(
            "boom", code=ErrorCode.PROXY_GENERATION_FAILED, details={"segment": 3}
        )
        payload = error.to_dict()
        assert payload["error_code"] == "PROXY_GENERATION_FAILED"
        assert payload["details"]["segment"] == 3
        # §81: the retry policy reads this, so proxy failures must be retryable.
        assert payload["recoverable"] is True


class TestProxyCodecArguments:
    """One preset/crf config, translated per encoder family.

    NVENC ignores -crf silently and its presets are p1-p7, so a config written
    for libx264 fed to h264_nvenc unchanged produces a default-quality encode
    at a preset the encoder never heard of. Found while making the analysis
    proxy hardware-encoded for a one-hour recording.
    """

    def test_software_encoders_keep_crf_and_preset(self) -> None:
        from backend.media.proxy import codec_arguments

        assert codec_arguments("libx264", "veryfast", 28) == [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        ]

    def test_nvenc_gets_cq_instead_of_crf(self) -> None:
        from backend.media.proxy import codec_arguments

        arguments = codec_arguments("h264_nvenc", "p4", 28)

        assert "-cq" in arguments
        assert "-crf" not in arguments

    def test_a_software_preset_on_nvenc_is_mapped_not_passed(self) -> None:
        # "veryfast" reaching NVENC is an error or a silent default; p4 is the
        # balanced hardware preset.
        from backend.media.proxy import codec_arguments

        arguments = codec_arguments("h264_nvenc", "veryfast", 28)

        assert arguments[arguments.index("-preset") + 1] == "p4"
