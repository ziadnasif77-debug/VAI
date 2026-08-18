"""The Director (Phase C).

A model proposes the shape of the edit; code checks it against the evidence and
executes it. The tests here are almost entirely about the checking, because
that is the half that decides whether a hallucination reaches the video:

    The Director may not produce an event that is not in the evidence.

The other half is the rule the owner of this editor has now stated three times.
Chronological order is not a preference the model may argue with, so a
blueprint that reorders a chronological edit has its *choices* honoured and its
*order* thrown away -- and the plan notes say exactly that happened.
"""

from __future__ import annotations

import pytest

from ai.llm.fake_provider import FakeLLMProvider
from backend.config.loader import load_config
from backend.core.models.enums import GameEventType, MomentType, VideoMode
from backend.director import build_blueprint
from backend.director.models import Beat, Blueprint, BlueprintRejection
from backend.director.service import MAX_MOMENTS_SHOWN
from backend.gaming.correlation import GameEvent
from backend.moments.formation import Moment
from backend.narrative.story import build_plan

pytestmark = pytest.mark.unit

PROMPT_ID = "narrative.blueprint"

#: The product's own band (§6), not a permissive one invented for the test:
#: the duration floor is what decides whether the Director's drops are taken.
POLICY = load_config().duration_policy


def _moment(start: float, *, duration: float = 20.0, media_id: str = "m") -> Moment:
    event = GameEvent(
        event_type=GameEventType.KILL,
        start_seconds=start,
        end_seconds=start + 2.0,
        confidence=0.85,
        importance=0.7,
        sources=("ocr",),
    )
    return Moment(
        media_id=media_id,
        moment_type=MomentType.SKILL,
        start_seconds=start,
        end_seconds=start + duration,
        context_start=start,
        context_end=start + duration,
        score=0.6,
        score_breakdown={"entertainment": 0.6, "narrative": 0.6},
        events=(event,),
    )


def _provider(payload: dict | None = None, **kwargs) -> FakeLLMProvider:
    return FakeLLMProvider(responses={PROMPT_ID: payload} if payload else {}, **kwargs)


def _ask(moments, payload=None, **kwargs):
    return build_blueprint(
        moments,
        provider=_provider(payload, **kwargs),
        intent_text="a video about the base",
        target_seconds=600.0,
    )


# -- what the model is shown ------------------------------------------------


def test_the_model_is_shown_the_moments_it_must_choose_from() -> None:
    provider = _provider({"theme": "t", "beats": [{"moment": 0, "role": "hook"}]})
    build_blueprint(
        [_moment(0.0), _moment(120.0)],
        provider=provider,
        intent_text="make it funny",
        target_seconds=300.0,
    )

    prompt_id, prompt = provider.calls[-1]
    assert prompt_id == PROMPT_ID
    # Numbered lines, because a beat is an index into exactly this list.
    assert "0. [" in prompt
    assert "1. [" in prompt
    assert "make it funny" in prompt


def test_a_long_selection_is_capped_rather_than_shown_in_full() -> None:
    provider = _provider({"theme": "t", "beats": [{"moment": 0, "role": "hook"}]})
    moments = [_moment(index * 60.0) for index in range(MAX_MOMENTS_SHOWN + 15)]
    build_blueprint(moments, provider=provider, intent_text="", target_seconds=600.0)

    _, prompt = provider.calls[-1]
    assert f"{MAX_MOMENTS_SHOWN - 1}. [" in prompt
    assert f"{MAX_MOMENTS_SHOWN}. [" not in prompt


# -- the rule the design exists for -----------------------------------------


def test_a_moment_that_does_not_exist_is_a_rejection_not_a_guess() -> None:
    outcome = _ask(
        [_moment(0.0), _moment(120.0)],
        {"theme": "t", "beats": [{"moment": 7, "role": "climax"}]},
    )

    assert isinstance(outcome, BlueprintRejection)
    assert outcome.detail == {"moment": 7, "available": 2}
    # Not the nearest existing index. There is nothing to repair it into.
    assert "does not exist" in outcome.reason


def test_the_same_moment_twice_is_rejected() -> None:
    outcome = _ask(
        [_moment(0.0), _moment(120.0)],
        {
            "theme": "t",
            "beats": [
                {"moment": 0, "role": "hook"},
                {"moment": 0, "role": "climax"},
            ],
        },
    )
    assert isinstance(outcome, BlueprintRejection)


def test_an_empty_plan_says_so_rather_than_returning_an_empty_video() -> None:
    outcome = _ask([_moment(0.0)], {"theme": "nothing here", "beats": []})
    assert isinstance(outcome, BlueprintRejection)
    assert outcome.detail["theme"] == "nothing here"


@pytest.mark.parametrize("kwargs", [{"fail_times": 5}, {"invalid_times": 5}])
def test_a_model_that_will_not_answer_is_a_rejection_not_an_exception(kwargs: dict) -> None:
    outcome = _ask([_moment(0.0)], {"theme": "t", "beats": []}, **kwargs)
    assert isinstance(outcome, BlueprintRejection)
    assert "did not answer" in outcome.reason


def test_no_provider_and_no_moments_both_come_back_as_reasons() -> None:
    assert isinstance(
        build_blueprint([_moment(0.0)], provider=None, intent_text="", target_seconds=1.0),
        BlueprintRejection,
    )
    assert isinstance(
        build_blueprint([], provider=_provider(), intent_text="", target_seconds=1.0),
        BlueprintRejection,
    )


def test_a_usable_answer_becomes_a_blueprint() -> None:
    outcome = _ask(
        [_moment(0.0), _moment(120.0), _moment(240.0)],
        {
            "theme": "the base kept falling over",
            "beats": [
                {"moment": 2, "role": "hook", "reason": "the collapse"},
                {"moment": 0, "role": "setup", "reason": "how it started"},
            ],
            "avoid": ["two deaths in a row"],
        },
    )

    assert isinstance(outcome, Blueprint)
    assert [beat.moment for beat in outcome.beats] == [2, 0]
    assert [beat.role for beat in outcome.beats] == ["hook", "setup"]
    assert outcome.avoid == ("two deaths in a row",)


# -- and what build_plan does with one --------------------------------------


def _plan(moments, blueprint, *, chronological=True, target=None):
    """Build a story plan, recording the list the Director was handed."""
    seen: list[list[Moment]] = []

    def director(shown):
        seen.append(list(shown))
        return blueprint

    plan = build_plan(
        moments,
        mode=VideoMode.STORY,
        target_seconds=(
            target if target is not None else sum(moment.context_duration for moment in moments)
        ),
        config=load_config().narrative,
        policy=POLICY,
        chronological=chronological,
        director=director,
    )
    return plan, seen


def _arc(count: int) -> Blueprint:
    """A blueprint naming every shown moment, in reverse -- so index errors show."""
    roles = ["payoff", "escalation", "build", "setup", "hook"]
    return Blueprint(
        theme="one long fall",
        beats=tuple(
            Beat(moment=index, role=roles[position % len(roles)])
            for position, index in enumerate(reversed(range(count)))
        ),
    )


def test_the_beats_index_the_list_the_director_was_shown() -> None:
    moments = [_moment(index * 200.0) for index in range(5)]
    plan, seen = _plan(moments, _arc(5))

    # The list handed to the Director is the one the optimiser selected, so
    # "moment 4" means the fifth line of the prompt and nothing else.
    assert [moment.context_start for moment in seen[0]] == [0.0, 200.0, 400.0, 600.0, 800.0]
    # Reverse-numbered roles, chronological clips: the role on the last clip is
    # the one the Director gave moment 4, not the one it gave the last beat.
    assert list(plan.beats) == ["hook", "setup", "build", "escalation", "payoff"]


def test_chronology_survives_a_director_that_reorders() -> None:
    moments = [_moment(index * 200.0) for index in range(5)]
    plan, _ = _plan(moments, _arc(5), chronological=True)

    starts = [moment.context_start for moment in plan.moments]
    assert starts == sorted(starts)
    assert any("time chose the order" in note for note in plan.notes)


def test_without_chronology_the_directors_order_is_what_runs() -> None:
    moments = [_moment(index * 200.0) for index in range(5)]
    plan, _ = _plan(moments, _arc(5), chronological=False)

    assert [moment.context_start for moment in plan.moments] == [800.0, 600.0, 400.0, 200.0, 0.0]
    assert list(plan.beats) == ["payoff", "escalation", "build", "setup", "hook"]


def test_a_drop_is_handed_back_to_the_optimiser_not_taken_out_of_the_length() -> None:
    # Twelve minutes of footage for a ten-minute request, so there is a clip in
    # reserve: the Director drops one and the optimiser refills from the pool.
    moments = [_moment(index * 100.0, duration=60.0) for index in range(12)]
    blueprint = Blueprint(beats=tuple(Beat(moment=index, role="body") for index in range(9)))
    plan, seen = _plan(moments, blueprint, target=600.0)

    assert len(seen[0]) == 10  # what the optimiser chose, and the Director saw
    dropped = {moment.context_start for moment in seen[0][9:]}
    assert not dropped & {moment.context_start for moment in plan.moments}
    assert plan.total_seconds == pytest.approx(600.0)
    assert plan.within_target
    assert any("refilled" in note for note in plan.notes)


def test_a_drop_that_cannot_be_refilled_is_refused_and_the_roles_kept() -> None:
    # Exactly ten minutes of footage for a ten-minute request. The Director
    # asks for one clip; there is nothing to put back, so §39 wins.
    moments = [_moment(index * 100.0, duration=60.0) for index in range(10)]
    blueprint = Blueprint(beats=(Beat(moment=3, role="climax"),))
    plan, _ = _plan(moments, blueprint, target=600.0)

    assert len(plan.moments) == 10
    assert plan.beats[3] == "climax"
    assert any("were not taken" in note for note in plan.notes)
    assert plan.within_target


def test_no_blueprint_is_the_order_the_pipeline_always_had() -> None:
    moments = [_moment(index * 200.0) for index in range(5)]
    total = sum(moment.context_duration for moment in moments)
    with_director, _ = _plan(moments, None)
    without = build_plan(
        moments,
        mode=VideoMode.STORY,
        target_seconds=total,
        config=load_config().narrative,
        policy=POLICY,
        chronological=True,
    )

    assert [moment.context_start for moment in with_director.moments] == [
        moment.context_start for moment in without.moments
    ]
    assert list(with_director.beats) == list(without.beats)
