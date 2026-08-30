"""V2-P4: effects as sentences rather than decorations.

``docs/DIRECTION.md`` has asked since day one for
SETUP -> BUILDUP -> TENSION -> PAYOFF -> REACTION and never got it. The
reason was not effort: an effect had no group, no role and no dependency,
and the planner was handed event *types* without event *times*, so there was
no beat in the data to build around.

The tests that matter here are the refusals. A composition that ships in
pieces is worse than one that does not ship at all -- half a build-up with no
payoff is noise wearing the shape of intent.
"""

from __future__ import annotations

import pytest

from backend.core.errors import ConfigurationError
from backend.core.models.enums import EffectType
from backend.emphasis import engine as emphasis
from backend.emphasis.grammar import load_library
from backend.emphasis.models import Anchor, Composition, CompositionMember

pytestmark = pytest.mark.unit


def _member(role, effect, offset, *, duration=0.5, depends_on=()):
    return CompositionMember(
        role=role,
        effect=effect,
        offset_seconds=offset,
        duration_seconds=duration,
        depends_on=tuple(depends_on),
    )


def _composition(**kwargs):
    defaults = {
        "id": "sentence",
        "members": (
            _member("buildup", EffectType.CINEMATIC_BARS, -1.5, duration=2.0),
            _member("payoff", EffectType.IMPACT, 0.0, depends_on=["buildup"]),
        ),
        "requires_level": (),
        "requires_kind": (),
        "min_strength": 0.0,
        "cooldown_seconds": 60.0,
        "cluster_cost": 2,
    }
    defaults.update(kwargs)
    return Composition(**defaults)


def _anchor(seconds=30.0, *, kind="boss_defeat", strength=0.9, level="climax"):
    return Anchor(
        id=f"a@{seconds}",
        media_id="media-aaaaaaaaaaaa",
        seconds=seconds,
        kind=kind,
        strength=strength,
        level=level,
    )


class TestASentenceShipsWholeOrNotAtAll:
    def test_every_member_is_placed_around_the_beat(self) -> None:
        spoken, _refused = emphasis.compose(
            [_anchor()], [_composition()], budget=10, min_gap_seconds=6.0
        )

        assert len(spoken) == 1
        placed = {member.role: seconds for member, seconds in spoken[0].placements}
        assert placed["buildup"] == pytest.approx(28.5), "the build-up runs BEFORE the beat"
        assert placed["payoff"] == pytest.approx(30.0)

    def test_the_members_arrive_in_time_order(self) -> None:
        spoken, _ = emphasis.compose(
            [_anchor()], [_composition()], budget=10, min_gap_seconds=6.0
        )

        times = [seconds for _member, seconds in spoken[0].placements]
        assert times == sorted(times)

    def test_a_sentence_that_runs_past_the_edit_is_not_spoken(self) -> None:
        # Its build-up would start before the video does.
        spoken, refused = emphasis.compose(
            [_anchor(seconds=0.5)],
            [_composition()],
            budget=10,
            min_gap_seconds=6.0,
            duration_seconds=600.0,
        )

        assert spoken == []
        assert any("runs past the edit" in reason for reason in refused)


class TestTheLibraryRefusesIncoherence:
    """Checked when the library loads, not when a video is made: a sentence
    that could never ship whole is a configuration error somebody should see
    once, not a hole in a finished video they have to infer."""

    def _config(self, config, members):
        from backend.config.schema import (
            CompositionConfig,
            CompositionMemberConfig,
            CompositionsConfig,
        )

        return config.model_copy(
            update={
                "compositions": CompositionsConfig(
                    enabled=True,
                    library={
                        "broken": CompositionConfig(
                            members=[CompositionMemberConfig(**m) for m in members]
                        )
                    },
                )
            }
        )

    def test_a_member_depending_on_a_role_that_is_absent(self, config) -> None:
        broken = self._config(
            config,
            [{"role": "payoff", "effect": "impact", "offset": 0.0, "depends_on": ["buildup"]}],
        )

        with pytest.raises(ConfigurationError, match="does not contain"):
            load_library(broken)

    def test_a_member_depending_on_something_that_has_not_happened_yet(self, config) -> None:
        # A reaction cannot depend on a payoff that comes after it.
        broken = self._config(
            config,
            [
                {"role": "reaction", "effect": "text_pop", "offset": -1.0, "depends_on": ["payoff"]},
                {"role": "payoff", "effect": "impact", "offset": 0.0},
            ],
        )

        with pytest.raises(ConfigurationError, match="has not happened yet"):
            load_library(broken)

    def test_a_member_depending_on_itself(self, config) -> None:
        broken = self._config(
            config,
            [{"role": "payoff", "effect": "impact", "offset": 0.0, "depends_on": ["payoff"]}],
        )

        with pytest.raises(ConfigurationError, match="depends on itself"):
            load_library(broken)

    def test_a_sentence_naming_an_effect_nothing_can_draw_is_skipped(self, config) -> None:
        # Three effects in the shipped library had no renderer when this was
        # written. A sentence containing one would ship as a hole.
        library = load_library(config, realisable=frozenset({"impact"}))

        assert library == []

    def test_the_shipped_library_is_coherent(self, config) -> None:
        library = load_library(config)

        assert library, "the shipped compositions load"
        for composition in library:
            roles = {member.role for member in composition.members}
            for member in composition.members:
                assert set(member.depends_on) <= roles


class TestRefusals:
    def test_a_weak_beat_gets_nothing(self) -> None:
        heavy = _composition(min_strength=0.8)

        spoken, _ = emphasis.compose(
            [_anchor(strength=0.2)], [heavy], budget=10, min_gap_seconds=6.0
        )

        assert spoken == []

    def test_a_level_the_sentence_does_not_belong_at(self) -> None:
        # A heavy payoff composition over a calm stretch is exactly the noise
        # the doctrine's decision filter exists to refuse.
        heavy = _composition(requires_level=("high", "climax"))

        spoken, _ = emphasis.compose(
            [_anchor(level="calm")], [heavy], budget=10, min_gap_seconds=6.0
        )

        assert spoken == []

    def test_the_budget_counts_a_sentence_as_one_gesture(self) -> None:
        # Charging per member would let the existing per-minute cap forbid
        # every composition there is.
        anchors = [_anchor(seconds=30.0), _anchor(seconds=200.0, strength=0.8)]

        spoken, refused = emphasis.compose(
            anchors, [_composition(cluster_cost=2)], budget=2, min_gap_seconds=6.0
        )

        assert len(spoken) == 1
        assert any("budget is spent" in reason for reason in refused)

    def test_the_same_sentence_twice_inside_its_cooldown(self) -> None:
        anchors = [_anchor(seconds=30.0), _anchor(seconds=45.0, strength=0.95)]

        spoken, refused = emphasis.compose(
            anchors, [_composition(cooldown_seconds=60.0)], budget=10, min_gap_seconds=6.0
        )

        assert len(spoken) == 1
        assert any("spoken" in reason and "away" in reason for reason in refused)

    def test_a_repeat_needs_a_clearly_stronger_beat(self) -> None:
        # Past the cooldown, the same sentence over a beat no stronger than
        # the last one is a template rather than an emphasis. Beats are
        # considered strongest-first, so the 0.92 one is spoken and the 0.90
        # one is judged against it.
        anchors = [_anchor(seconds=30.0, strength=0.90), _anchor(seconds=200.0, strength=0.92)]

        spoken, refused = emphasis.compose(
            anchors, [_composition(cooldown_seconds=30.0)], budget=10, min_gap_seconds=6.0
        )

        assert len(spoken) == 1
        assert spoken[0].anchor.strength == pytest.approx(0.92)
        assert any("no stronger" in reason for reason in refused)

    def test_two_sentences_do_not_speak_over_each_other(self) -> None:
        # Two different sentences, so neither cooldown applies -- what stops
        # the second is that the first is still speaking. Two sentences at
        # once are neither.
        anchors = [_anchor(seconds=30.0), _anchor(seconds=32.0, strength=0.8, kind="clutch")]
        library = [
            _composition(requires_kind=("boss_defeat",)),
            _composition(id="other", requires_kind=("clutch",), cooldown_seconds=0.0),
        ]

        spoken, refused = emphasis.compose(
            anchors, library, budget=10, min_gap_seconds=6.0
        )

        assert len(spoken) == 1
        assert any("speaking" in reason for reason in refused)


class TestAnchors:
    def test_a_beat_reported_twice_is_one_beat(self) -> None:
        found = emphasis.anchors_from(
            media_id="m",
            events=[(10.0, 11.0, "combat", 0.4), (10.4, 11.4, "boss_defeat", 0.9)],
        )

        assert len(found) == 1
        assert found[0].kind == "boss_defeat", "the stronger reading wins"

    def test_a_payoff_phase_is_a_beat_even_with_no_named_event(self) -> None:
        # Most events on real footage cannot be named at all; the moment's own
        # measured shape still says where it lands.
        found = emphasis.anchors_from(
            media_id="m",
            events=[],
            phases=[("moment-1", 40.0, 44.0, 0.8, "payoff")],
        )

        assert [anchor.kind for anchor in found] == ["payoff"]
        assert found[0].moment_id == "moment-1"

    def test_phases_that_are_not_payoffs_are_not_beats(self) -> None:
        found = emphasis.anchors_from(
            media_id="m",
            events=[],
            phases=[("m1", 10.0, 14.0, 0.9, "setup"), ("m1", 20.0, 22.0, 0.9, "reaction")],
        )

        assert found == []

    def test_the_unnameable_event_is_not_a_beat(self) -> None:
        assert "unknown_event" not in emphasis.strong_event_types()
        assert "boss_defeat" in emphasis.strong_event_types()


class TestTheRows:
    def test_every_row_names_its_sentence_and_its_part(self) -> None:
        spoken, _ = emphasis.compose(
            [_anchor()], [_composition()], budget=10, min_gap_seconds=6.0
        )

        rows = emphasis.as_effects(spoken, library_for=None)

        assert len(rows) == 2
        for row in rows:
            assert row["composition_id"] == "sentence"
            assert row["group_role"] in ("buildup", "payoff")
            assert row["anchor_seconds"] == pytest.approx(30.0)
            assert row["reason"], "a placement with no reason cannot be reviewed"
        assert [row["start_seconds"] for row in rows] == sorted(
            row["start_seconds"] for row in rows
        )
