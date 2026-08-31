"""Five styles must not be one style (V2-P0).

The complement of the frozen contract. That test says the house edit may not
move; this one says the other five may not stop moving -- and the two together
are what "the same footage becomes genuinely different edits" means as
something a machine can check.

The baseline measured the state before this phase and it was not good:

* on the nine projects where the optimiser genuinely chooses, **15 of 45**
  style-edits were byte-identical to the house edit;
* on the eight where the footage is shorter than the target, **40 of 40**
  were -- every style, every project, one video. Selection was the only lever
  a style had, and when everything fits there is nothing to select.

Both numbers are asserted here as ceilings rather than as exact values. A test
that pinned them exactly would fail on any deliberate improvement, which is the
mistake the frozen contract deliberately makes for the house style and must not
make for the other five.

Like the frozen contract, this reads the machine's own database and skips where
there is none. What it cannot see is stated rather than hidden: the style's
**pacing** doctrine is consumed by the EDL stage, after the point this measures,
so every number here understates how different these edits actually are.
"""

from __future__ import annotations

import statistics
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

HOUSE = "best_moments"
STYLES = ("gaming_fast", "cinematic", "funny", "competitive", "minimal")
TARGET = 20 * 60.0

#: How many style-edits may still be identical to the house edit.
#:
#: Not zero. A style is allowed to agree with the house on a particular
#: session -- two tastes reaching the same answer about the same footage is a
#: real thing, and forbidding it would force every style to differ for the sake
#: of differing. What is forbidden is agreeing *everywhere*, which is what
#: "these are not styles" looks like in numbers.
MAX_IDENTICAL_SHARE = 0.30


@pytest.fixture(scope="module")
def live():
    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    if not Path(paths.database_path).exists():
        pytest.skip("no database on this machine: nothing to differentiate")
    database = Database(paths.database_path, config.application.database)
    try:
        yield config, database
    finally:
        database.close()


def _edits(config, database) -> dict[str, dict[str, dict]]:
    """Every style's edit for every project with moments, by the stage's path."""
    rows = database.fetch_all(
        "SELECT p.id AS id, COUNT(m.id) AS moments FROM projects p "
        "LEFT JOIN moments m ON m.project_id = p.id "
        "GROUP BY p.id HAVING moments > 0",
        (),
    )
    found: dict[str, dict[str, dict]] = {}
    for row in rows:
        project_id = str(row["id"])
        moments = MomentRepository(database).list_for_project(project_id)
        durations = {
            media.id: float(media.metadata.duration_seconds)
            for media in MediaRepository(database).list_for_project(project_id)
            if getattr(getattr(media, "metadata", None), "duration_seconds", None)
        }
        try:
            reading = editorial_reading.read(
                database,
                config,
                moments=moments,
                media_ids=sorted(durations),
                durations=durations,
            )
        except Exception:
            reading = None

        per_style: dict[str, dict] = {}
        for style in (HOUSE, *STYLES):
            policy = resolve(config, style)
            strategy = editorial_strategy.resolve(policy)
            shaped = editorial_strategy.apply(moments, strategy, reading)
            proposed = propose(
                shaped,
                mode=VideoMode.STORY,
                target_seconds=TARGET,
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
            if winner is None:
                continue
            _profile, plan, _score = winner
            per_style[style] = {
                "fingerprint": [
                    (
                        moment.moment_type.value,
                        round(float(moment.context_start), 3),
                        round(float(moment.context_duration), 3),
                    )
                    for moment in plan.moments
                ],
                "median_shot": statistics.median(
                    [float(m.context_duration) for m in plan.moments] or [0.0]
                ),
                "dead_time": statistics.fmean(
                    [float(m.dead_time_score) for m in plan.moments] or [0.0]
                ),
            }
        if HOUSE in per_style:
            found[project_id] = per_style
    return found


@pytest.fixture(scope="module")
def edits(live) -> dict[str, dict[str, dict]]:
    config, database = live
    measured = _edits(config, database)
    if not measured:
        pytest.skip("no project on this machine has moments to plan from")
    return measured


@pytest.mark.slow
def test_a_style_is_not_the_house_edit_under_a_different_name(edits) -> None:
    """The number the baseline put at 55 of 85 before this phase existed."""
    identical: dict[str, int] = {style: 0 for style in STYLES}
    total = 0
    for per_style in edits.values():
        house = per_style[HOUSE]["fingerprint"]
        for style in STYLES:
            if style not in per_style:
                continue
            total += 1
            if per_style[style]["fingerprint"] == house:
                identical[style] += 1

    assert total, "no style-edit could be measured"
    share = sum(identical.values()) / total
    assert share <= MAX_IDENTICAL_SHARE, (
        f"{sum(identical.values())} of {total} style-edits ({share:.0%}) are "
        f"byte-identical to the house edit; the ceiling is "
        f"{MAX_IDENTICAL_SHARE:.0%}.\n  " + "\n  ".join(
            f"{style}: identical in {count}" for style, count in identical.items() if count
        )
    )


@pytest.mark.slow
def test_every_style_differs_somewhere(edits) -> None:
    """A style that agrees with the house on every project is not a style.

    Weaker than the ceiling above and aimed at a different failure: a single
    style quietly reverting to neutral while the others carry the average.
    """
    silent = []
    for style in STYLES:
        differs = sum(
            1
            for per_style in edits.values()
            if style in per_style
            and per_style[style]["fingerprint"] != per_style[HOUSE]["fingerprint"]
        )
        if differs == 0:
            silent.append(style)
    assert not silent, (
        "these styles produced the house edit on every single project, which "
        f"means their doctrine reaches nothing: {', '.join(silent)}"
    )


@pytest.mark.slow
def test_short_footage_no_longer_collapses_every_style_into_one(edits) -> None:
    """The finding this phase was built around.

    When a session holds less footage than the target the optimiser keeps every
    moment, so a style whose only lever is selection has no lever at all. The
    baseline measured 40 of 40 identical. What breaks the tie is the shot layer:
    a style may now say how much run-up a shot keeps and where its edges land,
    and that works whether or not anything was left to choose.
    """
    starved = {
        project_id: per_style
        for project_id, per_style in edits.items()
        if sum(f[2] for f in per_style[HOUSE]["fingerprint"]) < 0.9 * TARGET
    }
    if not starved:
        pytest.skip("every project on this machine has more footage than the target")

    collapsed = [
        project_id
        for project_id, per_style in starved.items()
        if all(
            per_style.get(style, {}).get("fingerprint") == per_style[HOUSE]["fingerprint"]
            for style in STYLES
            if style in per_style
        )
    ]
    assert len(collapsed) < len(starved), (
        f"all {len(starved)} short-footage projects still produce one identical "
        "video across every style"
    )


@pytest.mark.slow
def test_the_redefined_dead_time_reaches_a_real_edit(edits) -> None:
    """`dead_time_score` was 0.0 on all 435 stored moments, structurally.

    Not a threshold that needed adjusting: `_gaps_between` searches the
    stretches no moment's context occupies and `dead_time_ratio` then measures
    their overlap with a moment's context window, which is empty by
    construction. The editorial reading answers a different question -- does
    this stretch add context, anticipation, progression, payoff or reaction --
    and a style has to ask for it before the optimiser sees it.
    """
    priced = [
        per_style[style]["dead_time"]
        for per_style in edits.values()
        for style in STYLES
        if style in per_style and per_style[style]["dead_time"] > 0.0
    ]
    house = [per_style[HOUSE]["dead_time"] for per_style in edits.values()]

    assert priced, (
        "no style-edit anywhere carries a non-zero dead-time score, so the "
        "redefinition reaches no optimiser and changes no video"
    )
    assert not any(house), (
        "the house edit is now priced for dead time. That is a real change to "
        "every video this machine has made and it must be a decision, not a "
        "side effect -- see docs/BASELINE.md"
    )
