"""The Style Bible (V2-P8), and that it reaches the decisions.

A style selected an effects profile from V1's Phase 8 onward and changed
nothing else: not a cut length, not an audio decision, not what the judge
valued, not what counted as a defect. The tests that matter here are the ones
that would fail if the bible were a document nobody reads -- which is the only
interesting way for this phase to be wrong.
"""

from __future__ import annotations

import pytest

from backend.config.schema import StyleConfig, StyleEntry, StyleLimit
from backend.style import bible

pytestmark = pytest.mark.unit


class TestOneNamespace:
    def test_every_style_that_decorates_can_also_cut(self, config) -> None:
        # There is no second namespace on purpose: intent.style has selected an
        # effects profile since V1, and P8 gave that name a body rather than
        # inventing a parallel vocabulary for the same idea.
        assert set(config.style.bible) == set(config.effects.style_profiles)

    def test_the_default_names_a_style_that_exists(self, config) -> None:
        assert config.style.default in config.style.bible


class TestResolution:
    def test_a_named_style_resolves_to_its_own_body(self, config) -> None:
        assert bible.resolve(config, "cinematic").name == "cinematic"

    def test_an_unnamed_style_gets_the_house_body(self, config) -> None:
        for asked in ("", "default", None):
            assert bible.resolve(config, asked).name == config.style.default

    def test_a_typo_cuts_the_video_and_records_what_happened(self, config) -> None:
        # Refusing to build the edit over a misspelled style would be a worse
        # failure than cutting it in the house style -- but the record has to
        # say which name was asked for, or the stamp is a fiction.
        style = bible.resolve(config, "cinematick")

        assert style.name == config.style.default
        assert style.asked == "cinematick"


class TestTheFence:
    def test_a_value_outside_its_declared_range_refuses_to_load(self) -> None:
        with pytest.raises(ValueError, match="outside its declared range"):
            StyleConfig(
                default="wild",
                limits={"pacing.band_scale": StyleLimit(min=0.5, max=2.0)},
                bible={"wild": StyleEntry(version=1, pacing={"band_scale": 9.0})},
            )

    def test_the_message_names_the_key_and_the_style(self) -> None:
        with pytest.raises(ValueError) as raised:
            StyleConfig(
                default="wild",
                limits={"critique.hook_seconds": StyleLimit(min=5.0, max=30.0)},
                bible={"wild": StyleEntry(version=1, critique={"hook_seconds": 90.0})},
            )
        assert "wild" in str(raised.value) and "critique.hook_seconds" in str(raised.value)

    def test_a_default_with_no_body_refuses_to_load(self) -> None:
        with pytest.raises(ValueError, match="not in the bible"):
            StyleConfig(default="ghost", bible={"real": StyleEntry(version=1)})

    def test_every_tunable_has_a_declared_range(self, config) -> None:
        """A fence with a gap in it is not a fence.

        P10 is allowed to move these numbers and nothing else. A tunable with
        no limit would be a number it could move to anywhere at all.
        """
        from backend.config.schema import _style_values

        entry = config.style.bible[config.style.default]
        unfenced = sorted(set(_style_values(entry)) - set(config.style.limits))

        assert not unfenced, f"tunables with no declared range: {unfenced}"


class TestTheDigest:
    def test_the_same_taste_hashes_the_same(self, config) -> None:
        assert bible.resolve(config, "cinematic").digest == (
            bible.resolve(config, "cinematic").digest
        )

    def test_two_tastes_do_not(self, config) -> None:
        assert (
            bible.resolve(config, "cinematic").digest
            != bible.resolve(config, "best_moments").digest
        )

    def test_a_reworded_description_is_not_a_change_of_taste(self) -> None:
        plain = StyleEntry(version=1, description="one")
        reworded = StyleEntry(version=1, description="something else entirely")

        assert bible.digest_of("x", plain) == bible.digest_of("x", reworded)

    def test_a_changed_number_is(self) -> None:
        before = StyleEntry(version=1, pacing={"band_scale": 1.0})
        after = StyleEntry(version=1, pacing={"band_scale": 1.1})

        assert bible.digest_of("x", before) != bible.digest_of("x", after)


class TestTheStyleReachesTheCut:
    """The interesting failure: a bible nobody reads."""

    def _context(self, level="normal"):
        from backend.editorial.pacing_engine import PacingContext

        return PacingContext(position=100.0, level=level, tension=0.0)

    def test_a_patient_style_holds_a_shot_longer(self, config) -> None:
        from backend.editorial.pacing_engine import shot_length

        context = self._context()
        house = shot_length(context, config, bible.resolve(config, "best_moments"))
        slow = shot_length(context, config, bible.resolve(config, "cinematic"))

        assert slow.seconds > house.seconds

    def test_the_shot_says_which_style_lengthened_it(self, config) -> None:
        from backend.editorial.pacing_engine import shot_length

        rules = shot_length(
            self._context(), config, bible.resolve(config, "cinematic")
        ).rules

        assert any("cinematic" in rule for rule in rules)

    def test_without_a_style_nothing_moved(self, config) -> None:
        # Adopting the bible must change no video until someone edits it: the
        # schema defaults are the constants that were in Python before it.
        from backend.editorial.pacing_engine import shot_length

        context = self._context()
        before = shot_length(context, config)
        after = shot_length(context, config, bible.resolve(config, "best_moments"))

        assert after.seconds == pytest.approx(before.seconds)


class TestTheStyleReachesTheCriticism:
    def test_a_patient_style_is_not_fatigued_as_quickly(self, config) -> None:
        from backend.critic2.watch import _fatigue

        class _Halves:
            """One level for thirty seconds, then another for thirty."""

            duration_s = 60.0

            def level_for(self, at, *_args) -> str:
                return "normal" if at < 30.0 else "high"

            def value_at(self, *_args) -> float:
                return 0.0

        clips = [_Clip(index) for index in range(2)]
        house = _fatigue(
            clips, _Halves(), (), 60.0, bible.resolve(config, "best_moments").critique
        )
        patient = _fatigue(
            clips, _Halves(), (), 60.0, bible.resolve(config, "cinematic").critique
        )

        assert len(house) > len(patient)

    def test_a_style_that_decorates_less_calls_a_pile_sooner(self, config) -> None:
        from backend.critic2.watch import _effect_overuse

        placed = [_Effect(f"fx-{index}", 100.0 + index) for index in range(3)]
        house = _effect_overuse(
            placed, 200.0, bible.resolve(config, "best_moments").critique
        )
        patient = _effect_overuse(
            placed, 200.0, bible.resolve(config, "cinematic").critique
        )

        assert not house
        assert [item.code for item in patient] == ["effect_overuse"]


class TestTheStyleReachesTheJudge:
    def test_a_style_that_wants_no_effects_is_not_marked_down_for_having_none(
        self, config
    ) -> None:
        from backend.narrative.judge import _effect_density

        empty = [_Moment()]
        why: list[str] = []
        minimal = _effect_density(
            empty, None, why, bible.resolve(config, "minimal").judgement
        )
        house = _effect_density(
            empty, None, why, bible.resolve(config, "best_moments").judgement
        )

        assert minimal == pytest.approx(1.0)
        assert house < minimal


class TestWhatMadeThisEdit:
    def test_the_stamp_outlives_a_change_of_mind(
        self, database, project_manager, config
    ) -> None:
        """A brief edited after the render does not change the file on disk.

        The renderer, QA and the post-render critic all judge a video that
        already exists, and judging it by a style it was never cut with would
        report defects the edit was not trying to avoid.
        """
        from backend.core.models.project import ProjectCreate

        project = project_manager.create(
            ProjectCreate(name="Stamped", target_duration_seconds=600)
        )
        bible.stamp(database, project.id, bible.resolve(config, "cinematic"))

        assert bible.for_project(database, config, project.id).name == "cinematic"

    def test_without_a_stamp_the_brief_decides(
        self, database, project_manager, config
    ) -> None:
        from backend.core.models.project import ProjectCreate

        project = project_manager.create(
            ProjectCreate(name="Unstamped", target_duration_seconds=600)
        )

        assert bible.for_project(database, config, project.id).name in config.style.bible

    def test_the_stamp_keeps_the_whole_body(
        self, database, project_manager, config
    ) -> None:
        # config/style.yaml can be edited tomorrow; what this video was cut
        # with cannot be re-derived from a file that has since changed.
        import json

        from backend.core.models.project import ProjectCreate

        project = project_manager.create(
            ProjectCreate(name="Whole", target_duration_seconds=600)
        )
        bible.stamp(database, project.id, bible.resolve(config, "cinematic"))

        row = database.fetch_one(
            "SELECT resolved FROM edit_styles WHERE project_id = ?", (project.id,)
        )
        stored = json.loads(row["resolved"])

        assert stored["pacing"]["band_scale"] == pytest.approx(1.35)
        assert stored["critique"]["hook_seconds"] == pytest.approx(20.0)


class _Clip:
    def __init__(self, index: int) -> None:
        self.id = f"clip-{index}"
        self.timeline_start = index * 30.0
        self.duration = 30.0
        self.timeline_end = self.timeline_start + self.duration
        self.score = 0.5


class _Effect:
    def __init__(self, identifier: str, at: float) -> None:
        self.id = identifier
        self.timeline_start = at
        self.duration_seconds = 0.5
        self.composition_id = None
        self.strength = 1.0

    @property
    def composed(self) -> bool:
        return False


class _Moment:
    context_duration = 60.0
    events = ()
