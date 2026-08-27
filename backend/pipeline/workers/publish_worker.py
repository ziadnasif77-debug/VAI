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
from typing import Any

from backend.core.errors import ErrorCode
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import JobStage
from backend.core.models.publishing import PublishRequest, VideoMetadata
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.renders import RenderRepository
from backend.pipeline.workers.base import WorkerContext
from backend.publishing import PublisherRegistry, build_registry
from backend.publishing.base import PublishError

logger = get_logger("pipeline.workers.publish", LogChannel.RENDERING)


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
        # The render is resolved before the request is built: "publish the
        # latest" becomes a concrete id here, and that id -- not an empty
        # string -- is what the history must name.
        render = self._render(context, payload)
        request = self._request(context, payload)
        self._respect_qa(context)

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
            # The metadata as sent, so a later edit to the project does not
            # rewrite the history of what was actually published.
            "metadata_snapshot": request.metadata.model_dump(mode="json"),
        }

    # -- assembling the request ------------------------------------------

    def _request(self, context: WorkerContext, payload: dict[str, Any]) -> PublishRequest:
        return PublishRequest(
            project_id=context.project_id,
            render_id=str(payload.get("render_id") or ""),
            target=payload.get("target") or "local_file",
            metadata=VideoMetadata.model_validate(payload.get("metadata") or {}),
            destination=payload.get("destination"),
        )

    def _render(self, context: WorkerContext, payload: dict[str, Any]) -> dict[str, Any]:
        """The file being published: the named render, or the latest one."""
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
