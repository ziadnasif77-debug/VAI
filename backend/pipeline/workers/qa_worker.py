"""The QA stage (SPEC §76–§79).

The last thing that looks at the video before a person does. It inspects the
*file* the render produced — not the render's account of it — and separates two
kinds of finding, because they have different consequences:

* a **technical** failure means the file is unfit to publish, and blocks export;
* a **content** warning means the edit may be poor, and goes to a human (§78).

The stage never fails the job. A QA pass that raises turns "your video has a
problem" into "the pipeline crashed", which is strictly less information — the
report is the product, and it is stored either way. Even a missing file is a
*finding* (`file_exists`), because that is a fact about the render worth
recording next to the others.

Nothing to inspect is not a failure either. A recording with no moments in it
produces no timeline, so RENDER skips and there is no video — which is the
pipeline working, not breaking. QA skips too, and says which upstream stage
stopped and why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.core.errors import ErrorCode, GamingEditorError, QAError
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, QAStatus
from backend.database.repositories.audio_events import AudioEventRepository
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.qa import QaRepository
from backend.database.repositories.renders import RenderRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.database.repositories.vision import VisionRepository
from backend.gaming.profiles import load_profile
from backend.pipeline.workers.base import WorkerContext
from backend.qa import content, technical
from backend.qa.report import QaReport, build_report

logger = get_logger("pipeline.workers.qa", LogChannel.QA)


class QaWorker:
    """QA -- verify the finished video before anyone publishes it (§76, §77)."""

    stage = JobStage.QA

    def run(self, context: WorkerContext) -> dict[str, Any]:
        skipped = self._render_skipped(context)
        if skipped is not None:
            # The render stage had nothing to work with and said so. Repeating
            # that as a QA failure would turn "there was nothing to edit" into
            # "QA failed", which is the same fact stated worse (§95).
            context.report(1.0, "Nothing to check: no video was rendered")
            return {
                "skipped": True,
                "reason": skipped,
                "status": QAStatus.PASSED.value,
                "blocks_export": False,
                "needs_review": False,
            }

        render = RenderRepository(context.database).latest(context.project_id)
        if render is None or not render.get("output_path"):
            raise QAError(
                "The render stage reported a finished video but recorded no file.",
                code=ErrorCode.QA_FAILED,
                details={"project_id": context.project_id},
                recoverable=False,
            )

        path = Path(render["output_path"])
        timeline = TimelineRepository(context.database).load(context.project_id)

        context.report(0.1, "Inspecting the rendered file")
        expected = technical.Expected(
            duration_seconds=float(render.get("duration_seconds") or 0.0),
            width=_width_of(render),
            height=int(render.get("resolution") or 0),
            fps=float(render.get("fps") or 0.0),
        )
        findings = technical.inspect(
            path,
            expected,
            runner=context.ffmpeg,
            config=context.config.qa,
            sources=self._sources(context, timeline),
        )

        context.report(0.7, "Checking the edit")
        findings.extend(self._content(context, timeline, findings))

        report = build_report(findings, context.config.qa)
        stored = QaRepository(context.database).replace_for_render(
            context.project_id, render.get("id"), report.findings
        )

        if report.blocks_export:
            # Loud, and stored: this is the difference between a video someone
            # publishes and one they do not.
            logger.error(
                "QA blocks export",
                extra={
                    "project_id": context.project_id,
                    "render_id": render.get("id"),
                    "failures": [str(item) for item in report.failures],
                },
            )
        elif report.warnings:
            logger.warning(
                "QA passed with warnings",
                extra={"warnings": [str(item) for item in report.warnings]},
            )

        quality, uncertainties = self._doctrine_summary(context, report)
        context.report(1.0, _message(report))
        return {
            **report.summary(),
            "render_id": render.get("id"),
            "output_path": str(path),
            "checks_stored": stored,
            "explanation": report.explain(),
            # The doctrine's closing contract (docs/DIRECTION.md §34): one
            # score a person can read at a glance, and the honest list of
            # what the pipeline was unsure about instead of silence.
            "quality_score": quality,
            "uncertainties": uncertainties,
        }

    #: Notes that record an action the pipeline took on purpose. They stay
    #: in their stage's result for §80; they are not uncertainty.
    _ACTION_NOTE_PREFIXES = ("variety: ", "flatness: ", "arc: ")

    def _doctrine_summary(self, context: WorkerContext, report) -> tuple[int, list[str]]:
        """A 0-100 score and the uncertainty list, both derived, both stated.

        The score is arithmetic over what QA already measured -- failures,
        warnings, and every §95 note an upstream stage attached -- so two
        videos with the same defects always score the same. The uncertainty
        list is those notes verbatim: the §95 design already made every
        degradation announce itself; this collects the announcements where
        the doctrine asks for them.
        """
        notes: list[str] = []
        repository = JobRepository(context.database)
        for stage in (JobStage.STORY, JobStage.EDL, JobStage.RENDER, JobStage.CRITIQUE):
            job = repository.find(context.project_id, stage, None)
            if job is None or not job.result:
                continue
            for note in job.result.get("notes") or []:
                text = str(note)
                # The run-breaker's notes describe fixes made, not doubts
                # held -- §80 transparency, already on the story record.
                # Scoring them as uncertainty punished the machine for
                # repairing: measured live, a better edit scored 24 where
                # its broken predecessor scored 52.
                if text.startswith(self._ACTION_NOTE_PREFIXES):
                    continue
                if text and text not in notes:
                    notes.append(text)
            if stage is JobStage.CRITIQUE and job.result.get("skipped"):
                notes.append(f"critique skipped: {job.result.get('reason')}")

        score = 100
        score -= 25 * len(report.failures)
        score -= 6 * len(report.warnings)
        score -= 2 * len(notes)
        quality = max(0, min(100, score))

        uncertainties = [
            *(f"[warning] {item}" for item in report.warnings),
            *notes,
        ]
        return quality, uncertainties

    def _render_skipped(self, context: WorkerContext) -> str | None:
        """Why the render produced nothing, when it produced nothing.

        Read from the RENDER stage's own result rather than inferred from an
        absent file: "skipped because the timeline was empty" and "the file was
        deleted" look identical on disk and mean opposite things.
        """
        job = JobRepository(context.database).find(
            context.project_id, JobStage.RENDER, None
        )
        if job is None:
            return None
        result = job.result or {}
        if result.get("skipped"):
            return str(result.get("reason") or "the render stage was skipped")
        return None

    def _sources(self, context: WorkerContext, timeline) -> list[technical.SourceSpan]:
        """Where each second of the render came from (§42, §80).

        The frozen-frames check needs it to tell a stalled render from a menu
        someone left in, and only the video track can answer that. A clip whose
        recording has since been removed is skipped rather than guessed at: the
        check then treats its stretch as unexplained, which is the safe way to
        be wrong.
        """
        media = MediaRepository(context.database)
        paths: dict[str, Path] = {}
        spans: list[technical.SourceSpan] = []
        for clip in timeline.video_clips():
            if clip.media_id not in paths:
                item = media.get(clip.media_id)
                if item is None:
                    continue
                paths[clip.media_id] = Path(item.source_path)
            spans.append(
                technical.SourceSpan(
                    path=paths[clip.media_id],
                    timeline_start=clip.timeline_start,
                    timeline_end=clip.timeline_end,
                    source_in=clip.source_in,
                    speed=clip.speed,
                )
            )
        return spans

    def _content(
        self, context: WorkerContext, timeline, technical_findings
    ) -> list:
        """Run the §77 checks from analysis the pipeline already has."""
        database = context.database
        media = MediaRepository(database).list_for_project(context.project_id)

        observations: list[tuple[str, float, tuple[str, ...]]] = []
        silences: list[tuple[str, float, float]] = []
        vision = VisionRepository(database)
        audio_events = AudioEventRepository(database)
        for item in media:
            observations.extend(
                (item.id, stored.timestamp, stored.labels)
                for stored in vision.list_for_media(item.id)
            )
            silences.extend(
                (item.id, event.start_seconds, event.end_seconds)
                for event in audio_events.list_for_media(item.id)
                if event.event_type.value == "silence"
            )

        inputs = content.ContentInputs(
            observations=observations,
            silences=silences,
            pacing_warnings=self._pacing_warnings(context),
            black_runs=_black_runs(technical_findings),
            profile=self._profile(context),
        )
        return content.inspect(
            timeline,
            TimelineRepository(database).list_captions(context.project_id),
            inputs,
            config=context.config.qa,
            caption_config=context.config.captions,
        )

    def _pacing_warnings(self, context: WorkerContext) -> tuple[str, ...]:
        """What the narrative stage already said about its own pacing (§38)."""
        job = JobRepository(context.database).find(
            context.project_id, JobStage.STORY, None
        )
        if job is None:
            return ()
        pacing = (job.result or {}).get("pacing") or {}
        warnings = pacing.get("warnings") or []
        return tuple(str(item) for item in warnings)

    def _profile(self, context: WorkerContext):
        """The game profile, for its HUD regions (§22, §77)."""
        job = JobRepository(context.database).find(context.project_id, JobStage.GAME_EVENTS)
        name = (job.result or {}).get("game_profile") if job else None
        if not name:
            return None
        try:
            return load_profile(str(name), context.profiles_dir).profile
        except GamingEditorError as error:
            # A malformed profile is a problem for the gaming stage to report,
            # not a reason for QA to fail: the rest of the checks still apply.
            logger.warning(
                "Could not load the game profile for QA",
                extra={"profile": name, "error": str(error)},
            )
            return None


def _width_of(render: dict[str, Any]) -> int:
    """The render's width, derived from its stored height and 16:9.

    The renders table keeps ``resolution`` (a height) because that is how §75's
    preset is expressed. Deriving the width here beats adding a column that
    would only ever hold height × 16/9.
    """
    height = int(render.get("resolution") or 0)
    width = round(height * 16 / 9)
    return width + (width % 2)


def _black_runs(findings) -> tuple[tuple[float, float], ...]:
    """Black stretches the technical pass measured, for the content check.

    Passed along rather than measured again: the decode already happened, and
    the two checks ask different questions of the same numbers.
    """
    for finding in findings:
        if finding.check == "black_frames":
            runs = finding.measured.get("runs")
            longest = finding.measured.get("longest_seconds")
            if runs and longest:
                # Only the aggregate survives the report, which is enough for a
                # warning: the technical finding carries the detail.
                return ((0.0, float(longest)),) * min(int(runs), 1)
    return ()


def _message(report: QaReport) -> str:
    if report.blocks_export:
        return f"{len(report.failures)} technical failure(s); export blocked"
    if report.warnings:
        return f"passed with {len(report.warnings)} warning(s)"
    return "all checks passed"


__all__ = ["QaWorker"]
