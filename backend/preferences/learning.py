"""Reading preferences out of what the person already did (Phase F).

No new table, no new writer, no migration. Everything this needs was recorded
by the interaction layer the first time, because §4 asked for an auditable
intent log and §78 asked for the human to have the last word:

* ``editing_intent_updates`` -- every instruction, its delta and the words
  that produced it, per project;
* ``moments.user_state`` -- which moments were rejected by hand;
* ``projects`` -- when, so the recent past can outweigh the distant one.

The reading is deliberately conservative, and each rule below cost something to
get wrong somewhere else in this pipeline:

**Several projects, not several instructions.** Saying "faster" three times in
one project is one opinion repeated, usually because the first two did not take.
Saying it once in each of three projects is a preference.

**The last thing they said in that project.** A project where someone went
faster, then slower, then settled on fast, contributes *fast* -- the value they
stopped at, not the ones they passed through.

**Recent projects only.** Preferences change, and a window is the honest way to
let them: nothing is unlearned by a rule, it simply falls out of view once
enough newer projects disagree.

**Nothing is inferred from silence.** A project with no instructions says
nothing about anything. It is not evidence that the defaults were right; it is
much more often evidence that nobody looked.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from backend.core.logging import LogChannel, get_logger
from backend.database.connection import Database
from backend.database.repositories.projects import ProjectRepository
from backend.interaction.models import IntentDelta, IntentUpdate
from backend.interaction.store import IntentStore
from backend.preferences.models import Preference, Preferences

logger = get_logger("preferences.learning", LogChannel.PIPELINE)

#: Dimensions a preference may cover. Scalars only, and only the ones a person
#: actually repeats. Deliberately not here:
#:
#: ``target_duration_seconds`` -- a length is chosen per video, and a
#: twenty-minute default learned from three twenty-minute projects would fight
#: the import screen's own field every time.
#: ``mode`` -- same reason, and it is a project setting with its own control.
#: ``chronological`` -- already a shipped default, settled the hard way.
LEARNABLE: tuple[str, ...] = (
    "pacing",
    "dead_time_policy",
    "context_preservation",
    "effects",
    "captions",
    "music",
    "variety",
    "style",
)

#: List-valued dimensions, learned from what was added rather than the final
#: list: "no fail clips" in three projects is a preference, while the list it
#: produced in each is an artefact of what else was said there.
LEARNABLE_LISTS: tuple[str, ...] = (
    "avoid_moment_types",
    "priority_moment_types",
)

#: How many separate projects must agree before something is a preference.
#: Two is a coincidence often enough to be worth one more.
MIN_PROJECTS: int = 3

#: How far back to look. Enough to see a habit, short enough that a habit can
#: be replaced by a newer one without anybody having to say so.
RECENT_PROJECTS: int = 12

#: Instructions the parser understood poorly are still recorded, with the
#: confidence it had. Below this they are evidence of a sentence, not of a
#: preference.
MIN_CONFIDENCE: float = 0.5


def learn(
    database: Database,
    *,
    exclude_project: str | None = None,
    min_projects: int = MIN_PROJECTS,
    recent_projects: int = RECENT_PROJECTS,
) -> Preferences:
    """Read the intent logs of recent projects and find what repeats.

    Args:
        exclude_project: the project being edited now. Its own instructions are
            already applied on top and must not also count as evidence -- a
            preference that included the present would strengthen itself every
            time the intent was resolved.
    """
    projects = [
        project
        for project in ProjectRepository(database).list(limit=recent_projects + 1)
        if project.id != exclude_project
    ][:recent_projects]
    if len(projects) < min_projects:
        return Preferences(considered=len(projects))

    store = IntentStore(database)
    settled: dict[str, list[tuple[str, Any, str]]] = defaultdict(list)
    for project in projects:
        updates = [
            update for update in store.updates(project.id) if update.confidence >= MIN_CONFIDENCE
        ]
        for dimension, value, said in _settled_in(updates):
            settled[dimension].append((project.id, value, said))

    learned = []
    for dimension, entries in sorted(settled.items()):
        counts = Counter(_key(value) for _, value, _ in entries)
        best, times = counts.most_common(1)[0]
        if times < min_projects:
            continue
        matching = [entry for entry in entries if _key(entry[1]) == best]
        learned.append(
            Preference(
                dimension=dimension,
                value=matching[0][1],
                projects=times,
                seen_in=tuple(project for project, _, _ in matching),
                examples=tuple(dict.fromkeys(said for _, _, said in matching if said))[:3],
            )
        )

    preferences = Preferences(learned=tuple(learned), considered=len(projects))
    if not preferences.is_empty:
        logger.info("Learned editing preferences", extra=preferences.summary())
    return preferences


def as_delta(preferences: Preferences) -> IntentDelta:
    """The preferences as one delta, ready to apply over a preset.

    Scalars set the dimension. Lists **add** rather than replace, for the same
    reason `ListDelta` exists at all: a learned "avoid fails" must not silently
    discard an avoid the preset shipped with.
    """
    changes: dict[str, Any] = {}
    for preference in preferences.learned:
        if preference.dimension in LEARNABLE_LISTS:
            changes[preference.dimension] = {"add": list(preference.value)}
        else:
            changes[preference.dimension] = preference.value
    return IntentDelta.model_validate(changes) if changes else IntentDelta()


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _settled_in(updates: Sequence[IntentUpdate]) -> list[tuple[str, Any, str]]:
    """What each dimension ended up as in one project, and what was said for it.

    The *last* value, not every value. Someone who went faster, then slower,
    then fast again wanted fast; counting all three would have this project
    vote twice for a setting it rejected.
    """
    ordered = sorted(updates, key=lambda update: update.sequence)
    final: dict[str, tuple[Any, str]] = {}
    for update in ordered:
        payload = update.delta.model_dump(mode="json", exclude_none=True)
        said = (update.raw_text or "").strip()
        for dimension in LEARNABLE:
            if payload.get(dimension) is not None:
                final[dimension] = (payload[dimension], said)
        for dimension in LEARNABLE_LISTS:
            added = (payload.get(dimension) or {}).get("add") or []
            if added:
                # Added, not the resulting list: what the person asked for in
                # this project is the evidence, and the list it produced also
                # holds whatever the preset and earlier instructions put there.
                previous = list(final.get(dimension, ([], ""))[0])
                final[dimension] = (
                    [*previous, *(item for item in added if item not in previous)],
                    said,
                )
    return [(dimension, value, said) for dimension, (value, said) in final.items()]


def _key(value: Any) -> Any:
    """A hashable form, so lists can be counted like anything else."""
    return tuple(sorted(value)) if isinstance(value, list) else value


__all__ = ["LEARNABLE", "LEARNABLE_LISTS", "MIN_PROJECTS", "as_delta", "learn"]
