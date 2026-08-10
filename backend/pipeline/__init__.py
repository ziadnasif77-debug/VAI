"""Pipeline orchestration (SPEC §46, §47).

Stage workers claim jobs from the job manager, run one stage, and report back.
Keeping state in the database rather than in a process is what makes resume
after a crash possible: nothing about where the pipeline had got to lives in
memory, so a new process simply continues.

``PipelineRunner`` is the loop; ``workers/`` holds one implementation per stage.
Stages without a worker are not errors -- the runner stops at the first one and
reports it, which is what keeps a pipeline under construction usable.
"""

from backend.pipeline.runner import PipelineRunner, RunOutcome
from backend.pipeline.workers import StageWorker, WorkerContext, default_workers

__all__ = [
    "PipelineRunner",
    "RunOutcome",
    "StageWorker",
    "WorkerContext",
    "default_workers",
]
