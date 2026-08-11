"""Phase 13 acceptance, against the real language model.

The suite proves the wiring with a scripted model, which is the right trade for
a test that has to run in twenty minutes and mean the same thing twice. It
cannot prove the other half: that a real qwen2.5, given these prompts, actually
answers in the shape the code expects.

So this script runs the same criterion against Ollama. It builds a throwaway
project with plausible analysis rows, types sentences the rule parser reports
zero confidence on, and reports what changed.

Run it with Ollama up and the configured model pulled:

    python scripts/verify_phase13.py

Nothing here is imported by the application. It writes to a temporary directory
under the data root and deletes it on the way out.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.llm import create_llm_provider
from backend.config.loader import load_config
from backend.config.paths import build_paths
from backend.core.ids import new_id
from backend.core.models.enums import VideoMode
from backend.core.models.media import Media
from backend.core.models.project import ProjectCreate
from backend.database.connection import Database
from backend.database.migrator import migrate
from backend.database.repositories.media import MediaRepository
from backend.interaction.llm_fallback import LlmInterpreter
from backend.interaction.parser import classify, parse_command, parse_instruction
from backend.interaction.service import InteractionService
from backend.services.job_manager import JobManager
from backend.services.project_manager import ProjectManager
from tests.support.analysed_project import (
    complete_analysis,
    insert_clip,
    insert_moment,
)

#: Sentences a person might actually type, none of which the rules can read.
MESSAGES = [
    "give it the feel of a wildlife documentary",
    "I want it calmer, and stop plastering text over everything",
    "make it punchier, more like a highlight reel",
    "delete the part right after the opener",
    "get rid of whichever clip is the weakest",
    "did I sound frustrated at any point?",
    "make it 30 seconds",
    "grade it teal and orange",
]

MOMENT_TYPES = ["funny", "clutch", "epic", "fail", "victory"]


def build_project(database: Database, config, paths) -> str:
    projects = ProjectManager(database, paths, config)
    project = projects.create(
        ProjectCreate(
            name="Phase 13 verification",
            target_duration_seconds=1200,
            mode=VideoMode.BEST_MOMENTS,
            game="valorant",
        )
    )
    now = datetime.now(timezone.utc)
    media = Media(
        id=new_id("media"),
        project_id=project.id,
        source_path="D:/Gaming 2026/session.mp4",
        filename="session.mp4",
        container=".mp4",
        size_bytes=4096,
        checksum="a" * 64,
        created_at=now,
        updated_at=now,
    )
    MediaRepository(database).create(media)
    complete_analysis(JobManager(database, config), project.id, media.id)

    for index in range(6):
        moment_id = insert_moment(
            database,
            project.id,
            media.id,
            moment_type=MOMENT_TYPES[index % len(MOMENT_TYPES)],
            start=index * 400.0 + 90.0,
            score=0.92 - index * 0.06,
            explanation=[f"{MOMENT_TYPES[index % len(MOMENT_TYPES)]} moment, 3 detectors agreed"],
        )
        insert_clip(
            database,
            project.id,
            media.id,
            index=index,
            timeline_start=index * 200.0,
            duration=200.0,
            moment_id=moment_id,
        )
    return project.id


def main() -> int:
    config = load_config()
    provider = create_llm_provider(config)
    print(f"model: {config.models.llm.model} at {config.models.llm.endpoint}")
    if not provider.is_available():
        print("FAILED: the model is not available. Start Ollama and pull the model.")
        return 1

    # Everything stays on D:, including scratch (the machine's C: is full).
    scratch = Path(__file__).resolve().parents[1] / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="phase13-", dir=scratch))
    try:
        paths = build_paths(config, data_root=root).create()
        database = Database(paths.database_path, config.application.database)
        migrate(database)
        project_id = build_project(database, config, paths)
        interaction = InteractionService(
            database, config, interpreter=LlmInterpreter(config, provider)
        )

        for message in MESSAGES:
            parsed_instruction = parse_instruction(message).confidence
            parsed_command = parse_command(message)
            unreadable = parsed_instruction == 0.0 and parsed_command is None
            before = interaction.current_intent(project_id)
            duration_before = interaction._knowledge.edit_duration_seconds(project_id)

            print()
            print(f"> {message}")
            print(f"  rules: {classify(message).value}, unreadable={unreadable}")
            result = interaction.handle(project_id, message)
            after = interaction.current_intent(project_id)
            duration_after = interaction._knowledge.edit_duration_seconds(project_id)

            changed = [
                f"{field}: {getattr(before, field)} -> {getattr(after, field)}"
                for field in before.model_fields
                if getattr(before, field) != getattr(after, field)
            ]
            print(f"  {result.message}")
            if changed:
                print(f"  intent: {', '.join(changed)}")
            if abs(duration_after - duration_before) > 0.5:
                print(f"  edit: {duration_before / 60:.1f} min -> {duration_after / 60:.1f} min")
            if result.answer and result.answer.evidence:
                print(f"  cited: {[item.id for item in result.answer.evidence]}")

        database.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
        provider.unload()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
