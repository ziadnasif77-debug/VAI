"""Create a project over an evaluation cut and analyse it through MOMENTS.

    python scripts/analyse_cut.py "Eval GTA 40-50" gta_v D:/VAI/.tmp/eval/gta_40_50.mkv

The missing middle of the golden-set workflow (SPEC 117-118): annotate.py
builds the contact sheets a person labels from, evaluate.py scores a project
against the finished labels, and this is the step between -- turn the labelled
window into a project the pipeline has analysed, without rendering anything.
Stops after MOMENTS because that is everything the evaluator reads; a render
would cost twenty NVENC minutes to prove nothing about detection.

Prints the project id first so the evaluator can be pointed at it, then one
line per stage. Cut the window with stream copy so the frames are the
recording's own:

    ffmpeg -ss 2400 -i recording.mkv -t 600 -map 0 -c copy cut.mkv

and remember evaluate.py needs --offset with the same seconds the cut began
at. Label before analysing and the window stays out-of-sample.
"""

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.core.models.enums import JobStage
from backend.core.models.media import MediaImport
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.pipeline.runner import PipelineRunner
from backend.pipeline.workers import default_workers
from backend.services.job_manager import JobManager
from backend.services.media_ingestion import MediaIngestionService
from backend.services.project_manager import ProjectManager

name, game, cut = sys.argv[1], sys.argv[2], sys.argv[3]

config = load_config()
paths = build_paths(config, root=find_repository_root())
db = Database(paths.database_path, config.application.database)

projects = ProjectManager(db, paths, config)
project = projects.create(
    ProjectCreate(name=name, target_duration_seconds=900, game=game)
)
print(f"PROJECT {project.id}", flush=True)

media = MediaIngestionService(db, paths, config, JobManager(db, config))
media.import_media(project.id, MediaImport(path=cut))

order = [
    JobStage.IMPORT, JobStage.PROBE, JobStage.PROXY, JobStage.FRAMES,
    JobStage.SCENES, JobStage.AUDIO, JobStage.TRANSCRIPT, JobStage.VISION,
    JobStage.OCR, JobStage.AUDIO_EVENTS, JobStage.GAME_EVENTS, JobStage.MOMENTS,
]
keep = set(order)
workers = {stage: worker for stage, worker in default_workers().items() if stage in keep}
runner = PipelineRunner(db, paths, config, workers=workers)

t0 = time.perf_counter()
for outcome in runner.run_project(project.id):
    r = outcome.job.result or {}
    brief = {k: r[k] for k in (
        "frames_planned", "analysed_regions", "observations", "events",
        "named_events", "unknown_event_ratio", "moments", "segments",
    ) if k in r}
    print(
        f"{outcome.job.stage.value:12s} {'ok' if outcome.succeeded else 'FAILED'} "
        f"{(time.perf_counter() - t0)/60:6.1f} min  {brief}",
        flush=True,
    )
    if not outcome.succeeded:
        print(f"  error: {outcome.job.error_message}", flush=True)
        break

db.close()
print("DONE", flush=True)
