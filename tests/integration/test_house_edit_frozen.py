"""The house edit does not move (V2-P12 regression contract).

The one test the professional-editing work is allowed to break only on
purpose. Every upgrade from here on adds a way for footage to be read more
carefully, and every one of them arrives holding an argument for why the
result is better. This test does not accept arguments. It holds the exact edit
the system made before any of it, for every project on this machine, and
compares:

* which moments were selected, in order, with their spans;
* which counterfactual profile won;
* the hook, and the shot the video ends on;
* the timeline after clamping, de-overlapping and transitions -- the boundaries
  a viewer would actually see, not the ones the plan asked for;
* the eight judge axes, to three decimals.

**Exactly, not approximately.** A change that moves one clip boundary by
40 milliseconds is a change to the finished video, and the whole point of
freezing this is that nobody gets to decide after the fact that a difference
was too small to matter.

Only the **default style** is frozen. The other five exist to differ, and
freezing them would make every deliberate improvement to a style read as a
regression. What they are held to instead is that they remain distinguishable
from the house edit, which `scripts/baseline.py` measures.

This reads the machine's own database and skips where there is none, which is
what makes it a real gate here and dormant on a clean checkout. It is not a
substitute for the unit tests around each stage; it is the thing that catches
what those miss, which is a hundred small correct-looking changes adding up to
a different video.

Regenerating the golden file is a deliberate act:

    python scripts/baseline.py --freeze

Do it only when the house edit was *meant* to change, and say in the commit
message what changed and why.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.core.models.enums import VideoMode
from backend.database.connection import Database
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.editorial import reading as editorial_reading
from backend.editorial import strategy as editorial_strategy
from backend.editorial.doctrine import resolve
from backend.narrative import judge as judging
from backend.narrative.plans import propose
from backend.timeline.builder import build_timeline, clips_from_plan

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "house_edit.json"

#: The style the golden file was frozen under. Named rather than assumed: a
#: change to `style.default` would otherwise silently compare two different
#: tastes and report the difference as a regression in the optimiser.
HOUSE = "best_moments"


@pytest.fixture(scope="module")
def frozen() -> dict:
    if not GOLDEN.exists():  # pragma: no cover - the file is committed
        pytest.skip(f"no frozen house edit at {GOLDEN}")
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def live():
    """The machine's own database, or a skip.

    Deliberately not a `tmp_path`. A synthetic project would freeze synthetic
    footage, and the edits worth protecting are the ones made from the sessions
    actually recorded here.
    """
    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    if not Path(paths.database_path).exists():
        pytest.skip("no database on this machine: nothing to hold to a baseline")
    database = Database(paths.database_path, config.application.database)
    try:
        yield config, database
    finally:
        database.close()


def _edit(config, database, project_id: str, target: float):
    """The house edit for one project, by the story stage's own path.

    `propose` then `judge` then `best`, because that is what the stage does
    when counterfactuals are enabled -- which they are. The Director is not
    offered: it is a model call whose answer varies between runs, and a
    baseline that moves on its own protects nothing.
    """
    moments = MomentRepository(database).list_for_project(project_id)
    durations = {
        media.id: float(media.metadata.duration_seconds)
        for media in MediaRepository(database).list_for_project(project_id)
        if getattr(getattr(media, "metadata", None), "duration_seconds", None)
    }
    policy = resolve(config, HOUSE)
    # V2-P0: the shaping the story stage now does before anything selects. For
    # the house style with no brief this resolves to the neutral strategy and
    # `apply` hands back this exact list, so the contract measures that the
    # short circuit really is one rather than trusting that it is.
    strategy = editorial_strategy.resolve(policy)
    shaped = editorial_strategy.apply(
        moments, strategy, _reading(config, database, project_id, moments, durations)
    )
    assert shaped is moments, (
        f"{project_id}: the house strategy reshaped the footage. It is supposed "
        f"to be neutral, and a neutral strategy returns the caller's own list."
    )
    proposed = propose(
        shaped,
        mode=VideoMode.STORY,
        target_seconds=target,
        config=config.narrative,
        policy=config.duration_policy,
        chronological=True,
        media_durations=durations,
        selection=policy.selection,
    )
    scored = [
        (profile, plan, judging.judge(plan, reader=None, config=config, style=policy))
        for profile, plan in proposed
    ]
    winner = judging.best(scored)
    assert winner is not None, f"{project_id}: no profile could assemble an edit"
    profile, plan, score = winner
    built = build_timeline(
        clips_from_plan(plan),
        project_id=project_id,
        policy=config.output.duration_policy(),
        target_seconds=target,
        media_durations=durations,
        transitions=config.narrative.transitions,
    )
    return profile, plan, score, built.timeline


def _reading(config, database, project_id, moments, durations):
    """The editorial reading, or None. Never fatal, like every other caller."""
    try:
        return editorial_reading.read(
            database,
            config,
            moments=moments,
            media_ids=sorted(durations),
            durations=durations,
        )
    except Exception:
        return None


def _fingerprint(plan) -> list[list]:
    return [
        [
            moment.moment_type.value,
            round(float(moment.context_start), 3),
            round(float(moment.context_duration), 3),
        ]
        for moment in plan.moments
    ]


def _boundaries(timeline) -> list[list]:
    return [
        [clip.media_id[-6:], round(clip.source_in, 3), round(clip.source_out, 3)]
        for clip in timeline.video_clips()
    ]


def test_the_golden_file_covers_something(frozen) -> None:
    """A contract over nothing passes for the wrong reason."""
    assert frozen["projects"], "the frozen house edit is empty"


@pytest.mark.slow
def test_house_edit_is_unchanged(frozen, live) -> None:
    """Every frozen project still produces exactly the edit it produced."""
    config, database = live
    target = float(frozen["target_seconds"])

    checked = 0
    differences: list[str] = []
    for project_id, expected in frozen["projects"].items():
        moments = MomentRepository(database).list_for_project(project_id)
        if not moments:
            # The project was deleted or re-analysed away. Not a regression --
            # but not a pass either, so it is reported at the end.
            differences.append(f"{project_id}: has no moments on this machine any more")
            continue
        checked += 1
        profile, plan, score, timeline = _edit(config, database, project_id, target)
        where = f"{project_id[:16]} {expected.get('clips')} clips"

        if profile.id != expected["profile"]:
            differences.append(
                f"{where}: a different profile won -- "
                f"{expected['profile']} became {profile.id}"
            )
        if _fingerprint(plan) != expected["fingerprint"]:
            differences.append(
                f"{where}: the selection changed -- "
                f"{len(expected['fingerprint'])} clips became {len(plan.moments)}"
            )
        hook = getattr(plan.hook, "moment", None)
        if hook is not None and hook.moment_type.value != expected["hook_type"]:
            differences.append(
                f"{where}: the hook changed -- "
                f"{expected['hook_type']} became {hook.moment_type.value}"
            )
        if _boundaries(timeline) != expected["timeline"]["boundaries"]:
            differences.append(f"{where}: the timeline's clip boundaries changed")
        if round(float(timeline.duration), 2) != expected["timeline"]["seconds"]:
            differences.append(
                f"{where}: the finished length changed -- "
                f"{expected['timeline']['seconds']}s became "
                f"{round(float(timeline.duration), 2)}s"
            )
        axes = {name: value for name, value in score.as_dict().items() if name != "why"}
        if axes != expected["axes"]:
            moved = [
                f"{name} {expected['axes'][name]}->{value}"
                for name, value in axes.items()
                if expected["axes"].get(name) != value
            ]
            differences.append(f"{where}: the judge moved -- {', '.join(moved)}")

    assert checked, "no frozen project could be measured on this machine"
    assert not differences, (
        "The house edit changed. If that was intended, regenerate the golden "
        "file with `python scripts/baseline.py --freeze` and say why in the "
        "commit message.\n\n  " + "\n  ".join(differences)
    )
