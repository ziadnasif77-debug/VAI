"""Preferences (Phase F): learning what the person keeps asking for.

The lesson this generalises is written down next to `chronological` in
`EditingIntent`, and it was learned the expensive way:

    a default the user has to re-defeat per project is not a default

That one was settled by changing the shipped value, which works exactly once
and only for a preference the author happens to share. Everything else someone
re-types every project is still re-typed every project.

So the tests here are about the difference between a habit and a coincidence,
and about the one ordering question that matters: a preference beats the
preset, and an instruction about *this* project beats a preference.
"""

from __future__ import annotations

import pytest

from backend.core.models.enums import VideoMode
from backend.core.models.project import ProjectCreate
from backend.interaction.models import Pacing
from backend.interaction.service import InteractionService
from backend.preferences import MIN_PROJECTS, as_delta, learn

pytestmark = pytest.mark.unit


@pytest.fixture
def service(database, config) -> InteractionService:
    return InteractionService(database, config)


def _project(project_manager, name: str):
    return project_manager.create(
        ProjectCreate(name=name, target_duration_seconds=900, mode=VideoMode.STORY)
    )


def _asked(service, project_manager, text: str, *, projects: int, first: int = 0):
    """Say the same thing once in each of ``projects`` separate projects."""
    made = []
    for index in range(projects):
        project = _project(project_manager, f"p{first + index}")
        service.apply_instruction(project.id, text)
        made.append(project)
    return made


class TestWhatCounts:
    def test_the_same_request_in_enough_projects_becomes_a_preference(
        self, service, project_manager, database
    ) -> None:
        _asked(service, project_manager, "make it slower", projects=MIN_PROJECTS)

        preferences = learn(database)
        assert not preferences.is_empty
        learned = preferences.by_dimension()["pacing"]
        assert learned.value == Pacing.MEDIUM.value
        assert learned.projects == MIN_PROJECTS
        # The evidence, not just the conclusion (§80).
        assert len(learned.seen_in) == MIN_PROJECTS
        assert "make it slower" in learned.examples

    def test_one_project_saying_it_repeatedly_is_not_a_preference(
        self, service, project_manager, database
    ) -> None:
        # Usually because the first two did not take.
        project = _project(project_manager, "one")
        for _ in range(6):
            service.apply_instruction(project.id, "make it slower")

        assert learn(database).is_empty

    def test_too_few_projects_to_have_a_habit_learns_nothing(
        self, service, project_manager, database
    ) -> None:
        _asked(service, project_manager, "make it slower", projects=MIN_PROJECTS - 1)

        preferences = learn(database)
        assert preferences.is_empty
        # And says how little it had to go on, rather than implying agreement.
        assert preferences.considered == MIN_PROJECTS - 1

    def test_the_value_they_settled_on_is_the_one_that_counts(
        self, service, project_manager, database
    ) -> None:
        # Faster, then slower, then faster again: they wanted faster. Counting
        # every step would have each project vote for a setting it rejected.
        for index in range(MIN_PROJECTS):
            project = _project(project_manager, f"settled{index}")
            service.apply_instruction(project.id, "make it slower")
            service.apply_instruction(project.id, "make it faster")
            service.apply_instruction(project.id, "make it slower")

        assert learn(database).by_dimension()["pacing"].value == Pacing.MEDIUM.value

    def test_projects_that_disagree_learn_nothing(self, service, project_manager, database) -> None:
        _asked(service, project_manager, "make it slower", projects=2)
        _asked(service, project_manager, "make it faster", projects=2, first=90)

        assert "pacing" not in learn(database).by_dimension()

    def test_the_project_being_edited_is_not_its_own_evidence(
        self, service, project_manager, database
    ) -> None:
        made = _asked(service, project_manager, "make it slower", projects=MIN_PROJECTS)

        # With one of them excluded there is one fewer project agreeing, which
        # is the whole point: a preference that counted the present would
        # strengthen itself every time the intent was resolved.
        assert learn(database, exclude_project=made[-1].id).is_empty

    def test_a_silent_project_says_nothing_about_anything(
        self, service, project_manager, database
    ) -> None:
        _asked(service, project_manager, "make it slower", projects=MIN_PROJECTS)
        for index in range(4):
            _project(project_manager, f"untouched{index}")

        # Silence is much more often nobody looking than agreement.
        assert learn(database).by_dimension()["pacing"].projects == MIN_PROJECTS


class TestWhatItChanges:
    def test_a_preference_becomes_the_starting_point_for_a_new_project(
        self, service, project_manager, database, config
    ) -> None:
        _asked(service, project_manager, "make it slower", projects=MIN_PROJECTS)

        fresh = _project(project_manager, "fresh")
        # The shipped preset is `fast`; the preference is what moved it.
        assert service.current_intent(fresh.id).pacing is Pacing.MEDIUM

    def test_an_instruction_about_this_project_beats_a_preference(
        self, service, project_manager, database
    ) -> None:
        # "Keep it slow this time" gets it slow this time, and unlearns nothing.
        _asked(service, project_manager, "make it slower", projects=MIN_PROJECTS)

        fresh = _project(project_manager, "fresh")
        service.apply_instruction(fresh.id, "make it faster")

        assert service.current_intent(fresh.id).pacing is Pacing.FAST
        # And the preference is untouched by having been overruled once.
        assert learn(database, exclude_project=fresh.id).by_dimension()["pacing"].value == (
            Pacing.MEDIUM.value
        )

    def test_switching_it_off_starts_every_project_the_way_the_first_one_did(
        self, project_manager, database, config
    ) -> None:
        without = config.model_copy(
            update={
                "interaction": config.interaction.model_copy(update={"learn_preferences": False})
            }
        )
        service = InteractionService(database, without)
        _asked(service, project_manager, "make it slower", projects=MIN_PROJECTS)

        fresh = _project(project_manager, "fresh")
        # Straight back to the preset, which ships `fast`.
        assert service.current_intent(fresh.id).pacing is Pacing.FAST

    def test_a_learned_list_adds_rather_than_replaces(
        self, service, project_manager, database
    ) -> None:
        _asked(service, project_manager, "no fails", projects=MIN_PROJECTS)
        preferences = learn(database)

        assert preferences.by_dimension()["avoid_moment_types"].value == ["fail"]
        delta = as_delta(preferences)
        # A ListDelta, so a preset that already avoids something keeps it.
        assert delta.avoid_moment_types is not None
        assert delta.avoid_moment_types.set is None
        assert delta.avoid_moment_types.add


class TestItCanBeExplained:
    def test_every_preference_can_say_why_it_exists(
        self, service, project_manager, database
    ) -> None:
        _asked(service, project_manager, "make it slower", projects=MIN_PROJECTS)

        sentences = learn(database).sentences()
        assert sentences
        assert all(str(MIN_PROJECTS) in sentence for sentence in sentences)

    def test_nothing_learned_is_still_a_reportable_state(self, database) -> None:
        preferences = learn(database)
        assert preferences.is_empty
        assert preferences.summary() == {"considered": 0, "learned": []}
