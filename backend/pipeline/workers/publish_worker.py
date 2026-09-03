"""The PUBLISH stage — deliver a QA-passed render where it was asked to go.

Manual by constitution (§51): nothing queues this stage but an explicit user
request, and the request itself rides in the job payload — target, metadata,
destination — written by the API at the moment the person confirmed. The
worker adds no judgement of its own; its whole job is to honour two earlier
verdicts:

* **the QA verdict.** A render QA marked as blocking export is refused here
  with the same words (§76). Warnings pass — §78 already gave the human the
  last word, and the human just used it by pressing publish.
* **the registry's.** A target with no registered publisher fails with the
  typed error the registry has always promised, which is what "YouTube is not
  connected" looks like at this layer.

The outcome is stored on the job row (§81), the same way every stage stores
its result. There is no publications table yet, and this is why there does not
need to be one to ship: the job history *is* the publication history, queryable
per project, with the metadata snapshot inside the payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from backend.core.errors import ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage, PublishTarget
from backend.core.models.publishing import PublishRequest, VideoMetadata
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.renders import RenderRepository
from backend.pipeline.workers.base import WorkerContext
from backend.publishing import PublisherRegistry, build_registry
from backend.publishing.base import PublishError

logger = get_logger("pipeline.workers.publish", LogChannel.RENDERING)

#: Who may authorise a publication. A name here is a person's decision --
#: made in the moment, or made once for a schedule -- never the machine's.
AUTHORISATIONS: Final[frozenset[str]] = frozenset(
    {"human", "daily_policy", "project_auto_publish"}
)


class PublishWorker:
    """PUBLISH — hand the finished file to its destination (§50, §51)."""

    stage = JobStage.PUBLISH

    def __init__(self, publishers: PublisherRegistry | None = None) -> None:
        """
        Args:
            publishers: injected for tests. Production builds the registry
                from configuration, the same construction the API uses, so
                the two can never disagree about what is available.
        """
        self._publishers = publishers

    def run(self, context: WorkerContext) -> dict[str, Any]:
        payload = context.job.payload or {}
        if payload.get("auto"):
            # Always regenerated, never read back from the payload: auto
            # means the analysis writes the words at publish time. Found
            # live -- the worker persists its payload, so a requeued auto
            # job carried the metadata snapshot of its very first run, from
            # before any of the writing existed, and published a video
            # titled with the raw project name.
            payload = {**payload, "metadata": self._suggested(context).model_dump()}
        if payload.get("publish_at_utc"):
            # The daily policy's scheduled publication: the instant rides in
            # the payload (set once, at queueing) and lands in the metadata
            # the platform sees, whether the metadata was regenerated or not.
            metadata = dict(payload.get("metadata") or {})
            metadata["publish_at"] = payload["publish_at_utc"]
            payload = {**payload, "metadata": metadata}
        # The render is resolved before the request is built: "publish the
        # latest" becomes a concrete id here, and that id -- not an empty
        # string -- is what the history must name.
        payload = {
            # A request with no destination takes the configured default
            # rather than failing on a missing key.
            "target": context.config.publishing.default_target.value,
            **payload,
        }
        render = self._render(context, payload)
        request = self._request(context, payload)
        self._respect_qa(context)
        self._respect_authorisation(context, request, payload)

        registry = self._publishers or build_registry(context.config, context.data_root)
        publisher = registry.get(request.target)
        if not publisher.is_configured():
            raise PublishError(
                f"The {request.target.value} publisher is not connected yet.",
                code=ErrorCode.PUBLISH_TARGET_NOT_CONFIGURED,
                details={"target": request.target.value},
                recoverable=False,
            )

        context.report(0.1, f"Delivering to {request.target.value}")
        result = publisher.publish(request, Path(render["output_path"]))

        context.report(1.0, result.external_url or result.output_path or "Delivered")
        return {
            **result.model_dump(mode="json"),
            "render_id": request.render_id,
            # Which Short went out, when one did; the render id above is
            # empty in that case rather than borrowed from the long video.
            "short": request.short,
            # The metadata as sent, so a later edit to the project does not
            # rewrite the history of what was actually published.
            "metadata_snapshot": request.metadata.model_dump(mode="json"),
        }

    # -- assembling the request ------------------------------------------

    def _suggested(self, context: WorkerContext):
        """Metadata from the analysis, assembled the way the API route does."""
        from backend.core.models.enums import JobStage
        from backend.database.repositories.gaming import GameEventRepository
        from backend.database.repositories.jobs import JobRepository
        from backend.database.repositories.media import MediaRepository
        from backend.database.repositories.moments import MomentRepository
        from backend.database.repositories.projects import ProjectRepository
        from backend.database.repositories.transcript import TranscriptRepository
        from backend.metadata.generation import detect_transcript_language, suggest

        database = context.database
        project = ProjectRepository(database).require(context.project_id)
        moments = []
        events_by_media = {}
        for item in MediaRepository(database).list_for_project(context.project_id):
            moments.extend(MomentRepository(database).list_for_media(item.id))
            events_by_media[item.id] = GameEventRepository(database).list_for_media(item.id)
        story = JobRepository(database).find(context.project_id, JobStage.STORY, None)
        clips = story.result.get("clips") if story is not None and story.result else None
        defaults = context.config.publishing.defaults
        segments = TranscriptRepository(database).list_for_project(context.project_id)
        written = suggest(
            project,
            moments=moments,
            events_by_media=events_by_media,
            story_clips=clips if isinstance(clips, list) else (),
            transcript_language=detect_transcript_language(segments),
            min_chapter_seconds=defaults.min_chapter_seconds,
            title_language=defaults.title_language,
        )
        # The owner's standing visibility, not the model's cautious default:
        # auto-publish means "publish the way I always publish" -- hooked
        # thumbnail included, the same one the Suggest button would build.
        from backend.metadata.thumbnail import render_thumbnail

        creative = self._creative(context, project, moments, segments)
        if creative is not None:
            written = written.model_copy(update={"title": creative.title})
        thumbnail = render_thumbnail(
            database=database,
            config=context.config,
            assets_dir=context.paths.assets,
            moments=moments,
            language=detect_transcript_language(segments),
            # Titles come from the model; the thumbnail's two lines stay with
            # the curated tables. Measured 2026-08-28: the same sampling that
            # writes a sound Arabic title writes gibberish hook lines, and a
            # thumbnail wears its words larger than anything else.
            hook_text=None,
        )
        return written.model_copy(
            update={
                "visibility": context.config.publishing.youtube.default_visibility,
                **({"thumbnail_path": thumbnail} if thumbnail else {}),
            }
        )

    def _creative(self, context: WorkerContext, project, moments, segments):
        """The model's own words for this video, or ``None`` and templates hold."""
        defaults = context.config.publishing.defaults
        if not defaults.creative_text:
            return None
        try:
            from ai.llm import create_llm_provider
            from backend.metadata.creative import gather_and_write

            provider = create_llm_provider(context.config)
            return gather_and_write(
                provider,
                database=context.database,
                project=project,
                moments=moments,
                segments=segments,
                arabic=defaults.title_language != "en",
            )
        except Exception:
            logger.exception("Creative text unavailable; templates carry on")
            return None

    def _request(self, context: WorkerContext, payload: dict[str, Any]) -> PublishRequest:
        return PublishRequest(
            project_id=context.project_id,
            render_id=str(payload.get("render_id") or ""),
            target=payload.get("target") or "local_file",
            metadata=VideoMetadata.model_validate(payload.get("metadata") or {}),
            destination=payload.get("destination"),
            short=str(payload.get("short") or "") or None,
        )

    def _render(self, context: WorkerContext, payload: dict[str, Any]) -> dict[str, Any]:
        """The file being published: the named Short, the named render, or the latest render."""
        if payload.get("short"):
            return self._short(context, payload)
        renders = RenderRepository(context.database)
        wanted = str(payload.get("render_id") or "")
        record = None
        if wanted:
            record = next(
                (
                    item
                    for item in renders.list_for_project(context.project_id)
                    if str(item.get("id")) == wanted
                ),
                None,
            )
        else:
            record = renders.latest(context.project_id)
        if not record or not record.get("output_path"):
            raise PublishError(
                "There is no finished render to publish. Render the project first.",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"project_id": context.project_id, "render_id": wanted or None},
                recoverable=False,
            )
        # The payload carries the id forward so the result names what went out.
        payload["render_id"] = str(record.get("id"))
        return record

    def _short(self, context: WorkerContext, payload: dict[str, Any]) -> dict[str, Any]:
        """A Short, by the filename the SHORTS stage reported.

        Shorts are not render rows: the SHORTS stage cuts them from the
        source and lists them in its result, beside the renders on disk. The
        first real upload found the button could not reach them and the test
        Short went up through the publisher stack by hand. This resolves a
        name against that list and nothing else -- not a path from the
        request, not a glob over the directory -- so the only files that can
        leave are the ones a stage of this project produced and wrote down.
        """
        wanted = str(payload.get("short") or "")
        job = JobRepository(context.database).find(context.project_id, JobStage.SHORTS)
        produced = [
            item
            for item in ((job.result or {}).get("shorts") or [])
            if isinstance(item, dict) and item.get("output_path")
        ]
        names = [Path(str(item["output_path"])).name for item in produced]
        match = next(
            (item for item, name in zip(produced, names, strict=True) if name == wanted), None
        )
        if match is None:
            raise PublishError(
                f"This project produced no Short named {wanted!r}."
                + (f" It produced: {', '.join(names)}." if names else " Generate Shorts first."),
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"project_id": context.project_id, "short": wanted, "produced": names},
                recoverable=False,
            )
        path = Path(str(match["output_path"]))
        if not path.is_file():
            raise PublishError(
                f"The Short {wanted!r} is no longer at {path}.",
                code=ErrorCode.MEDIA_NOT_FOUND,
                details={"project_id": context.project_id, "short": wanted, "path": str(path)},
                recoverable=False,
            )
        # A Short has no render id, and the history must not invent one.
        payload["render_id"] = ""
        return {"id": "", "output_path": str(path), "short": wanted, **match}

    def _respect_authorisation(
        self, context: WorkerContext, request, payload: dict[str, Any]
    ) -> None:
        """§51: nothing reaches a channel that nobody authorised.

        ``publishing.youtube.require_explicit_confirmation`` shipped as ``true``
        and was read by no code at all, so the setting promised a gate that did
        not exist. It exists now, and it asks the honest question -- not "did a
        person click this second", which would forbid the owner's own daily
        policy, but **who authorised this, and can it be named**.

        Three authorisations are real, and every publish carries one:

        * ``human`` -- the export screen, where somebody pressed publish;
        * ``daily_policy`` -- the owner's standing schedule, consent given once
          in writing for a recurring publication;
        * ``project_auto_publish`` -- the project's own flag, set by hand.

        A payload with none of them is a publication nobody can account for,
        and that is exactly what this refuses.
        """
        if request.target is not PublishTarget.YOUTUBE:
            return
        if not context.config.publishing.youtube.require_explicit_confirmation:
            return
        authorisation = str(payload.get("authorised_by") or "").strip()
        if authorisation in AUTHORISATIONS:
            logger.info(
                "Publication authorised",
                extra={"project_id": context.project_id, "authorised_by": authorisation},
            )
            return
        raise PublishError(
            "This publication names no authorisation. Publish from the export "
            "screen, or let the daily policy queue it.",
            code=ErrorCode.PUBLISH_FAILED,
            details={
                "blocked_by": "authorisation",
                "authorised_by": authorisation or None,
                "accepted": sorted(AUTHORISATIONS),
            },
            recoverable=False,
        )

    def _respect_qa(self, context: WorkerContext) -> None:
        """§76: a QA failure stops the file leaving. Warnings do not."""
        qa = next(
            (
                job
                for job in JobRepository(context.database).list_for_project(context.project_id)
                if job.stage is JobStage.QA and job.result
            ),
            None,
        )
        if qa is not None and (qa.result or {}).get("blocks_export"):
            raise PublishError(
                "QA found problems that block publishing. Fix them or re-render "
                "before delivering this file.",
                code=ErrorCode.PUBLISH_FAILED,
                details={"blocked_by": "qa"},
                recoverable=False,
            )


__all__ = ["PublishWorker"]
