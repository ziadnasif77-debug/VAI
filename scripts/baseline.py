r"""Measure the edit this system makes, before changing how it makes it.

The one thing that stops "we added intelligence, so it is better" from being
the whole evaluation. Every number here is taken from the plan the pipeline
actually builds -- the same call the story stage makes -- for every style, over
every project that has moments, and written to a file that a later run can be
diffed against.

    python scripts/baseline.py                     # measure now, save it
    python scripts/baseline.py --against FILE      # measure now, diff that
    python scripts/baseline.py --project ID        # one project

Two things it is careful about.

**It renders nothing and analyses nothing.** Selection and planning read stored
moments and take milliseconds, which is what makes it worth running after every
change rather than once. A metric that needed a render would be measured twice
and then never again.

**It separates "this style chose different clips" from "this style has a
different character".** Two edits can share ten clips out of ninety and still
feel identical; two others can differ by three and be plainly different videos.
The identity block is the second question, and it is the one that says whether
a style is a style.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import logging

logging.disable(logging.WARNING)  # the pipeline's narration is not the report;
# every pacing warning it would have printed is counted below instead.

from backend.config.loader import load_config
from backend.config.paths import build_paths, find_repository_root
from backend.core.models.enums import VideoMode
from backend.database.connection import Database
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.editorial import bookends as editorial_bookends
from backend.editorial import reading as editorial_reading
from backend.editorial import sequence as editorial_sequence
from backend.editorial import strategy as editorial_strategy
from backend.editorial.doctrine import resolve
from backend.narrative import judge as judging
from backend.narrative.plans import propose
from backend.timeline.builder import build_timeline, clips_from_plan

#: The five the owner names. `best_moments` is first because everything else is
#: measured as a difference from it.
STYLES: tuple[str, ...] = (
    "best_moments",
    "gaming_fast",
    "cinematic",
    "funny",
    "competitive",
    "minimal",
)

HOUSE: str = "best_moments"

TARGET_SECONDS: float = 20 * 60.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=None, help="one project instead of all")
    parser.add_argument("--against", default=None, help="a saved baseline to diff")
    parser.add_argument("--out", default=None, help="where to write this run")
    parser.add_argument(
        "--target", type=float, default=TARGET_SECONDS, help="target seconds"
    )
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="rewrite the committed house edit that tests/integration/"
        "test_house_edit_frozen.py holds every change to. A deliberate act: "
        "do it only when the default style was meant to change.",
    )
    arguments = parser.parse_args()

    config = load_config()
    paths = build_paths(config, root=find_repository_root())
    database = Database(paths.database_path, config.application.database)

    projects = _projects(database, arguments.project)
    if not projects:
        print("No project has stored moments to plan from.")
        database.close()
        return 1

    measured: dict[str, dict] = {}
    for project_id, name, count in projects:
        print(f"\n{project_id}  {name[:40]}  ({count} moments)")
        measured[project_id] = _measure(
            database, config, project_id, arguments.target
        )
        _print(measured[project_id])

    run = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "target_seconds": arguments.target,
        "projects": measured,
    }
    out = Path(arguments.out or _default_path(paths))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nwritten to  {out}")

    if arguments.freeze:
        _freeze(run)

    if arguments.against:
        _diff(json.loads(Path(arguments.against).read_text(encoding="utf-8")), run)

    database.close()
    return 0


# -- measuring ---------------------------------------------------------------


def _measure(database, config, project_id: str, target: float) -> dict:
    """Every style's edit for one project, as numbers.

    The path is the story stage's own, not an approximation of it. With
    counterfactuals enabled -- which is the shipped setting -- that stage does
    not build one plan: it builds one per profile, judges all three, and
    renders the winner. Measuring `build_plan` alone would measure a call the
    pipeline does not make, and would miss the thing a style most visibly
    changes, which is *which profile wins*.

    The Director is not offered. It is a model call, its answer varies between
    runs, and a baseline whose numbers move on their own is not a baseline.
    What that costs is stated in the report rather than hidden: this measures
    the deterministic edit underneath, and the Director's contribution is
    ordering, which the chronology constitution already bounds.
    """
    moments = MomentRepository(database).list_for_project(project_id)
    durations = _durations(database, project_id)
    # V2-P0: the same reading the story stage makes, once per project rather
    # than once per style -- it depends on the footage, not on the taste.
    reading = _reading(database, config, project_id, moments, durations)
    by_style: dict[str, dict] = {}
    for style in STYLES:
        policy = resolve(config, style)
        strategy = editorial_strategy.resolve(policy)
        try:
            shaped = editorial_strategy.apply(moments, strategy, reading, durations)
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
            proposed = [
                (profile, _bookended(plan, strategy, reading))
                for profile, plan in proposed
            ]
            scored = [
                (profile, plan, judging.judge(plan, reader=None, config=config, style=policy, editorial=reading))
                for profile, plan in proposed
            ]
            winner = judging.best(scored)
        except Exception as error:  # a style that cannot plan is a finding
            by_style[style] = {"failed": f"{type(error).__name__}: {error}"[:200]}
            continue
        if winner is None:
            by_style[style] = {"failed": "no profile could assemble an edit"}
            continue
        profile, plan, score = winner
        numbers = _numbers(plan, score)
        numbers["timeline"] = _timeline(
            plan, config, project_id, durations, target
        )
        numbers["profile"] = profile.id
        numbers["considered"] = {
            other.id: round(float(each.total), 4) for other, _plan, each in scored
        }
        numbers["style_digest"] = policy.digest
        numbers["strategy"] = strategy.as_dict()
        numbers["reshaped"] = shaped is not moments
        numbers["cut_quality"] = _cut_quality(plan, reading)
        numbers["sequence"] = editorial_sequence.read(plan.moments, reading).as_dict()
        numbers["paced"] = _paced(plan, config, policy, database, durations)
        numbers["selection"] = {
            "entertainment": policy.selection.entertainment,
            "narrative": policy.selection.narrative,
            "variety": policy.selection.variety,
            "repetition_penalty": policy.selection.repetition_penalty,
            "dead_time_penalty": policy.selection.dead_time_penalty,
            "neutral": policy.selection.is_neutral,
        }
        by_style[style] = numbers
    by_style["_identity"] = _identity(by_style)
    return by_style


def _numbers(plan, score) -> dict:
    """One plan, measured. Nothing here needs a render."""
    chosen = list(plan.moments)
    lengths = [float(m.context_duration) for m in chosen] or [0.0]
    scores = [float(m.score) for m in chosen] or [0.0]
    axes = score.as_dict()
    axes.pop("why", None)

    hook = getattr(plan.hook, "moment", None)
    last = chosen[-1] if chosen else None

    return {
        # -- the shape of the edit ---------------------------------------
        "clips": len(chosen),
        "total_seconds": round(sum(lengths), 1),
        "deviation": round(float(plan.optimisation.deviation), 1),
        "median_shot": round(statistics.median(lengths), 2),
        "mean_shot": round(statistics.fmean(lengths), 2),
        "shot_variance": round(statistics.pstdev(lengths), 2),
        "shortest_shot": round(min(lengths), 2),
        "longest_shot": round(max(lengths), 2),
        # -- what the selection tolerated --------------------------------
        "dead_time_ratio": _mean(m.dead_time_score for m in chosen),
        "repetition_ratio": _mean(m.repetition_score for m in chosen),
        # -- the two ends ------------------------------------------------
        "hook_strength": round(float(getattr(hook, "score", 0.0) or 0.0), 3),
        "hook_type": _type_of(hook),
        "hook_reason": str(getattr(plan.hook, "reason", ""))[:80],
        "ending_strength": round(float(last.score) if last else 0.0, 3),
        "ending_type": _type_of(last),
        # -- how it paces, in the pacing stage's own words ----------------
        "clips_per_minute": round(float(plan.pacing.clips_per_minute), 3),
        "longest_same_type_run": int(plan.pacing.longest_same_type_run),
        "longest_flat_seconds": round(float(plan.pacing.longest_flat_seconds), 1),
        "rises": bool(plan.pacing.rises),
        "pacing_warnings": list(plan.pacing.warnings),
        # -- what the effects stage will have to work with ----------------
        "effect_opportunities": sum(1 for m in chosen for e in m.events if _named(e)),
        # -- what the judge already knows --------------------------------
        "judge_total": round(float(score.total), 4),
        "axes": axes,
        # -- style identity: a style, or merely a different ten clips? ----
        "identity": {
            "intensity": _mean(scores),
            "pacing": round(statistics.median(lengths), 2),
            "context_ratio": _mean(
                (m.context_duration / max(m.end_seconds - m.start_seconds, 1e-6))
                for m in chosen
            ),
            "variety": round(
                len({m.moment_type for m in chosen}) / max(len(chosen), 1), 4
            ),
            "reaction_usage": _mean(
                float(m.score_breakdown.get("reaction", 0.0)) for m in chosen
            ),
            "dead_time_tolerance": _mean(m.dead_time_score for m in chosen),
            "repetition_tolerance": _mean(m.repetition_score for m in chosen),
            "structure": float(axes.get("structure", 0.0)),
        },
        # -- what was chosen, so a later run can diff membership ----------
        "fingerprint": [
            [_type_of(m), round(float(m.context_start), 3), round(float(m.context_duration), 3)]
            for m in chosen
        ],
    }


def _identity(by_style: dict) -> dict:
    """Whether these are styles, or one style asked six different ways.

    The question the twelve shape metrics cannot answer on their own. Two edits
    can share every clip but one and still be different videos; two others can
    differ in a quarter of their clips and be indistinguishable to watch. So
    this reports both halves separately -- how much of the *selection* moved,
    and how far the *character* moved -- and calls a style undifferentiated
    only when neither did.

    The distance is normalised per dimension, because these are not the same
    units: a median shot length lives in seconds and a variety ratio in 0..1,
    and summing them raw would make pacing the only dimension that counts.
    """
    house = by_style.get(HOUSE) or {}
    reference = house.get("identity") or {}
    if not reference:
        return {}

    #: What a full unit of difference is on each dimension. Chosen from what
    #: these projects actually span, so a score of 1.0 means "as different as
    #: this footage allows", not "as different as a float can be".
    spans = {
        "intensity": 0.30,
        "pacing": 30.0,
        "context_ratio": 1.00,
        "variety": 0.40,
        "reaction_usage": 0.30,
        "dead_time_tolerance": 0.30,
        "repetition_tolerance": 0.30,
        "structure": 0.40,
    }

    report: dict[str, dict] = {}
    for style, numbers in by_style.items():
        if style == HOUSE or "identity" not in numbers:
            continue
        mine = numbers["identity"]
        per_axis = {
            axis: round(
                min(1.0, abs(float(mine.get(axis, 0.0)) - float(reference.get(axis, 0.0))) / span),
                4,
            )
            for axis, span in spans.items()
        }
        moved = _moved(house.get("fingerprint"), numbers.get("fingerprint"))
        character = round(sum(per_axis.values()) / len(per_axis), 4)
        report[style] = {
            "selection_moved": moved,
            "selection_share": round(moved / max(len(house.get("fingerprint") or []), 1), 4),
            "character_distance": character,
            "per_axis": per_axis,
            # A style whose edit is byte-identical to the house edit is not a
            # style yet, whatever its judge score says: the judge is scoring
            # the same video under a different taste.
            "undifferentiated": moved == 0 and character < 0.02,
        }
    return report


def _moved(house, other) -> int:
    if not house or not other:
        return 0
    return len({tuple(item) for item in house} ^ {tuple(item) for item in other})


def _timeline(plan, config, project_id: str, durations, target: float) -> dict:
    """The plan laid out, because a plan is not yet an edit.

    The story stage chooses spans; the EDL stage clamps them to the duration
    band, resolves overlaps, trims anything reading past the end of its
    recording, and assigns transitions. All of that changes clip boundaries,
    and a regression contract that stopped at the plan would call two different
    videos identical.

    Deterministic and file-free: `build_timeline` reads spans and durations,
    never the footage. The screen guard is not applied here -- it reads the
    vision store and would make this depend on analysis state -- and its
    absence is stated rather than hidden, because it can drop clips.
    """
    try:
        built = build_timeline(
            clips_from_plan(plan),
            project_id=project_id,
            policy=config.output.duration_policy(),
            target_seconds=target,
            media_durations=durations,
            transitions=config.narrative.transitions,
        )
    except Exception as error:
        return {"failed": f"{type(error).__name__}: {error}"[:200]}

    timeline = built.timeline
    clips = timeline.video_clips()
    return {
        "clips": len(clips),
        "seconds": round(float(timeline.duration), 2),
        "clamped": len(built.clamped),
        "notes": list(built.notes)[:6],
        "transitions": sorted(
            {str(getattr(clip.transition_in, "value", clip.transition_in)) for clip in clips}
        ),
        "boundaries": [
            [clip.media_id[-6:], round(clip.source_in, 3), round(clip.source_out, 3)]
            for clip in clips
        ],
        "roles": [clip.role for clip in clips],
    }


def _mean(values) -> float:
    collected = [float(value) for value in values]
    return round(statistics.fmean(collected), 4) if collected else 0.0


def _type_of(moment) -> str:
    return str(getattr(getattr(moment, "moment_type", None), "value", "") or "-")


def _named(event) -> bool:
    return str(getattr(getattr(event, "event_type", None), "value", "")) != "unknown_event"


# -- reading and reporting ---------------------------------------------------


def _projects(database, only: str | None) -> list[tuple[str, str, int]]:
    rows = database.fetch_all(
        "SELECT p.id AS id, p.name AS name, COUNT(m.id) AS moments "
        "FROM projects p LEFT JOIN moments m ON m.project_id = p.id "
        "GROUP BY p.id HAVING moments > 0 ORDER BY moments DESC",
        (),
    )
    found = [(row["id"], row["name"], int(row["moments"])) for row in rows]
    return [row for row in found if row[0] == only] if only else found


#: How close a cut has to land to a seam the footage already has before it
#: counts as landing *on* it. Half a second: a cut within half a second of a
#: scene change reads as that scene change, and one further away reads as a cut.
SEAM_TOLERANCE: float = 0.5


def _paced(plan, config, style, database, durations) -> dict:
    """What the EDL stage's pacing engine would make of these shots.

    The harness stops at the timeline, and until now that hid the layer where
    a style has the most direct say over rhythm: `backend.editorial.pacing_engine`
    re-reads every shot's length at the second it starts on, and it is style-
    aware -- `band_scale`, `stutter_relief`, `stillness_relief` and
    `on_the_beat_seconds` are all taste. Every rhythm number this harness
    reported before was therefore measured *before the thing that sets rhythm*.

    Deterministic and file-free, like everything else here: the engine reads
    the semantic lanes and the event onsets, both of which are stored or
    rebuilt from stored rows.
    """
    from backend.editorial import pacing_engine
    from backend.semantic.timeline import load_timeline

    if not config.editorial.pacing.dynamic or plan.is_empty:
        return {"measured": False}

    readers: dict = {}
    for media_id, length in (durations or {}).items():
        try:
            readers[media_id] = load_timeline(
                database, media_id, duration_seconds=float(length), config=config
            )
        except Exception:
            continue
    if not readers:
        return {"measured": False}

    lengths: list[float] = []
    rules: dict[str, int] = {}
    previous = 0.0
    for moment in plan.moments:
        pacing_context = pacing_engine.context_at(
            moment.context_start,
            readers.get(moment.media_id),
            role=str(moment.metadata.get("role", "body")),
            previous_length=previous,
            events=(),
        )
        if pacing_context is None:
            continue
        shot = pacing_engine.shot_length(pacing_context, config=config, style=style)
        # The engine returns a *cap*, and the EDL stage applies it as one --
        # `dynamic_cap` caps a clip rather than setting it. Measuring the raw
        # cap would report a rhythm no viewer sees on any clip the plan
        # already made shorter than it.
        lengths.append(min(float(moment.context_duration), float(shot.seconds)))
        for rule in shot.rules:
            key = rule.split(" ")[0][:24]
            rules[key] = rules.get(key, 0) + 1
        previous = lengths[-1]

    if len(lengths) < 2:
        return {"measured": False}
    changed = sum(
        1
        for a, b in zip(lengths, lengths[1:])
        if abs(b / max(a, 1e-6) - 1.0) >= 0.20
    )
    return {
        "measured": True,
        "shots": len(lengths),
        "median": round(statistics.median(lengths), 2),
        "spread": round(statistics.pstdev(lengths), 2),
        "rhythm": round(changed / (len(lengths) - 1), 4),
        "rules": dict(sorted(rules.items(), key=lambda kv: -kv[1])[:6]),
    }


def _bookended(plan, strategy, reading):
    """Where this candidate begins and ends -- the story stage's own call."""
    if strategy.bookends.is_neutral or plan.is_empty:
        return plan
    decided = editorial_bookends.read(plan.moments, strategy.bookends, reading)
    return editorial_bookends.apply_to_plan(plan, decided)


def _cut_quality(plan, reading) -> dict:
    """How many of this edit's cuts fall on a boundary the footage already had.

    The metric `CutPolicy` exists to move, measured on the finished selection
    rather than on the policy's intention. A cut on a seam the analysis already
    found is invisible; a cut on the second a moment happens to end at is a cut.

    Absent rather than zero when there is no reading: a project whose stores
    will not load has no seams to be measured against, and reporting 0% would
    make an unmeasured edit and a badly cut one indistinguishable.
    """
    if reading is None:
        return {"measured": False}
    on_seam = cuts = 0
    for moment in plan.moments:
        shot = reading.shot(moment)
        if shot is None:
            continue
        for edge, candidates in (
            (float(moment.context_start), shot.cuts.into),
            (float(moment.context_end), shot.cuts.out_of),
        ):
            cuts += 1
            if any(abs(edge - float(point)) <= SEAM_TOLERANCE for point in candidates):
                on_seam += 1
    if not cuts:
        return {"measured": False}
    return {
        "measured": True,
        "cuts": cuts,
        "on_seam": on_seam,
        "share": round(on_seam / cuts, 4),
    }


def _reading(database, config, project_id, moments, durations):
    """The editorial reading, or None when it cannot be made.

    Never fatal: a project whose stores will not load is measured without the
    reading rather than skipped, and the strategy then shapes nothing -- which
    is the same §95 rule the Director and the Critic follow.
    """
    try:
        return editorial_reading.read(
            database,
            config,
            moments=moments,
            media_ids=sorted(durations) or _media_ids(database, project_id),
            durations=durations,
        )
    except Exception as error:
        print(f"  (no editorial reading: {type(error).__name__}: {str(error)[:90]})")
        return None


def _media_ids(database, project_id: str) -> list[str]:
    return [media.id for media in MediaRepository(database).list_for_project(project_id)]


def _durations(database, project_id: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for media in MediaRepository(database).list_for_project(project_id):
        seconds = getattr(getattr(media, "metadata", None), "duration_seconds", None)
        if seconds:
            found[media.id] = float(seconds)
    return found


def _print(by_style: dict) -> None:
    house = by_style.get("best_moments", {})
    header = (
        f"  {'style':<14}{'clips':>6}{'median':>8}{'var':>7}{'dead':>7}"
        f"{'repeat':>8}{'judge':>8}{'won':>5}{'vs house':>11}"
    )
    print(header)
    for style, numbers in by_style.items():
        if style == "_identity":
            continue
        if "failed" in numbers:
            print(f"  {style:<14}{numbers['failed'][:60]}")
            continue
        differ = _differ(house.get("fingerprint"), numbers.get("fingerprint"))
        print(
            f"  {style:<14}{numbers['clips']:>6}{numbers['median_shot']:>8.2f}"
            f"{numbers['shot_variance']:>7.2f}{numbers['dead_time_ratio']:>7.3f}"
            f"{numbers['repetition_ratio']:>8.3f}{numbers['judge_total']:>8.3f}"
            f"{numbers.get('profile', '-'):>5}{differ:>11}"
        )
    identity = by_style.get("_identity") or {}
    flat = [style for style, found in identity.items() if found.get("undifferentiated")]
    if flat:
        print(f"    identical to the house edit: {', '.join(flat)}")
    else:
        distances = ", ".join(
            f"{style} {found['character_distance']:.2f}"
            for style, found in identity.items()
        )
        if distances:
            print(f"    character distance from house: {distances}")


def _differ(house, other) -> str:
    if not house or not other:
        return "-"
    a = {tuple(item) for item in house}
    b = {tuple(item) for item in other}
    return f"{len(a ^ b)} moved"


def _diff(before: dict, after: dict) -> None:
    """What changed, per project per style, for the numbers that matter."""
    watched = (
        "profile",
        "clips",
        "median_shot",
        "shot_variance",
        "dead_time_ratio",
        "repetition_ratio",
        "hook_strength",
        "ending_strength",
        "judge_total",
    )
    print("\n=== against the saved baseline ===")
    # A shape check, because the alternative is a lie that reads like a
    # regression. `--freeze` writes the *house* edit flat -- one entry per
    # project, with no style level -- while this function expects a full run
    # keyed by style. Handed the frozen file, every `old_styles.get(style)`
    # misses, every metric prints a change from nothing, and all seventeen
    # projects report "[selection changed]" when nothing has changed at all.
    # That happened, and the output was convincing for a minute.
    sample = next(iter((before.get("projects") or {}).values()), None)
    if isinstance(sample, dict) and "axes" in sample and "profile" in sample:
        print(
            "  this is a frozen house edit, not a baseline: it holds one "
            "entry per project rather than one per style, so there is "
            "nothing here to diff style by style."
        )
        print(
            "  the house edit is checked by tests/integration/"
            "test_house_edit_frozen.py; for a style diff, compare against a "
            "file written by a plain `python scripts/baseline.py` run."
        )
        return

    moved = 0
    for project_id, styles in after["projects"].items():
        old_styles = (before.get("projects") or {}).get(project_id)
        if not old_styles:
            print(f"  {project_id}: not in the baseline")
            continue
        for style, numbers in styles.items():
            if style == "_identity":
                continue
            old = old_styles.get(style) or {}
            changes = [
                f"{key} {old.get(key)}->{numbers.get(key)}"
                for key in watched
                if key in numbers and old.get(key) != numbers.get(key)
            ]
            same_choice = old.get("fingerprint") == numbers.get("fingerprint")
            if changes or not same_choice:
                moved += 1
                mark = "" if same_choice else "  [selection changed]"
                print(f"  {project_id[:16]} {style:<14}{mark}")
                for change in changes:
                    print(f"      {change}")
    if not moved:
        print("  nothing moved: every style plans exactly as it did.")


#: What the regression test reads. One file, in the repository, so a change to
#: the house edit is visible in a diff rather than in a number nobody kept.
FROZEN = Path(__file__).resolve().parents[1] / "tests" / "golden" / "house_edit.json"


def _freeze(run: dict) -> None:
    """Rewrite the frozen house edit.

    Only the default style, and only the parts a viewer would notice: what was
    selected in what order, which profile won, the two ends, the timeline after
    clamping, and the judge's eight axes. The other five styles are left out on
    purpose -- they exist to differ, and freezing them would turn every
    deliberate improvement to a style into a failing test.
    """
    frozen = {
        "target_seconds": run["target_seconds"],
        "note": (
            "The house edit, frozen. Regenerate with scripts/baseline.py "
            "--freeze only when a change to the house style is intended and "
            "reviewed."
        ),
        "projects": {},
    }
    for project_id, styles in run["projects"].items():
        house = styles.get(HOUSE)
        if not house or "failed" in house:
            continue
        timeline = house.get("timeline") or {}
        if "failed" in timeline:
            continue
        frozen["projects"][project_id] = {
            "profile": house["profile"],
            "clips": house["clips"],
            "total_seconds": house["total_seconds"],
            "median_shot": house["median_shot"],
            "hook_type": house["hook_type"],
            "hook_strength": house["hook_strength"],
            "ending_type": house["ending_type"],
            "judge_total": house["judge_total"],
            "axes": house["axes"],
            "fingerprint": house["fingerprint"],
            "timeline": {
                key: timeline[key]
                for key in ("clips", "seconds", "clamped", "transitions", "boundaries", "roles")
            },
        }
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    FROZEN.write_text(json.dumps(frozen, indent=2, sort_keys=True), encoding="utf-8")
    print(f"froze the house edit for {len(frozen['projects'])} project(s) -> {FROZEN}")


def _default_path(paths) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Path(paths.data_root) / ".cache" / "baseline" / f"{stamp}.json"


if __name__ == "__main__":
    raise SystemExit(main())
