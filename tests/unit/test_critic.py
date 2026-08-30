"""The Critic (Phase E).

The first component in this pipeline that reads the pipeline's own output. What
it is checked on here is not whether its taste is good -- that is a model's
problem and a person's judgement -- but whether a bad answer can reach a video:

* a note about a clip that does not exist is thrown away, not repaired;
* a trim longer than the clip is capped, not obeyed;
* §42's operations are the only vocabulary, so nothing can be asked for that
  the timeline would not already do; and
* §39 keeps its veto, so the review can improve a video but never shorten it
  out of the length that was asked for.
"""

from __future__ import annotations

import pytest

from ai.llm.fake_provider import FakeLLMProvider
from backend.config.loader import load_config
from backend.core.models.enums import GameEventType, MomentType, TrackKind
from backend.critic import gather, review
from backend.critic.models import Action, Critique, CritiqueRejection, Note
from backend.critic.revision import apply
from backend.critic.service import _SCHEMA, MAX_CLIPS_SHOWN, MAX_TRIM_FRACTION
from backend.timeline.models import Timeline, TimelineClip, Track

pytestmark = pytest.mark.unit

PROMPT_ID = "critique.edit_review"
POLICY = load_config().duration_policy
MEDIA = "media-0000000000"


def _clip(index: int, *, seconds: float = 60.0, source_in: float = 0.0, **extra) -> TimelineClip:
    start = index * seconds
    return TimelineClip(
        id=f"clip-{index:012d}",
        media_id=extra.pop("media_id", MEDIA),
        track=TrackKind.VIDEO,
        clip_index=index,
        source_in=source_in,
        source_out=source_in + seconds,
        timeline_start=start,
        timeline_end=start + seconds,
        moment_type=MomentType.SKILL,
        role=extra.pop("role", "body"),
        **extra,
    )


def _timeline(count: int = 4, *, seconds: float = 60.0) -> Timeline:
    clips = tuple(_clip(index, seconds=seconds, source_in=index * 200.0) for index in range(count))
    return Timeline(
        project_id="proj-000000000000",
        tracks=(Track(kind=TrackKind.VIDEO, clips=clips),),
    )


def _provider(payload: dict | None = None, **kwargs) -> FakeLLMProvider:
    return FakeLLMProvider(responses={PROMPT_ID: payload} if payload else {}, **kwargs)


def _evidence(timeline: Timeline | None = None, *, target: float = 240.0, **kwargs):
    return gather(timeline or _timeline(), target_seconds=target, **kwargs)


def _ask(payload=None, *, timeline=None, target=240.0, **kwargs):
    return review(_evidence(timeline, target=target), provider=_provider(payload, **kwargs))


# -- what the Critic is shown ------------------------------------------------


class TestTheEvidence:
    def test_every_enabled_clip_becomes_a_numbered_row(self) -> None:
        evidence = _evidence(_timeline(3))

        assert [clip.index for clip in evidence.clips] == [0, 1, 2]
        assert evidence.total_seconds == pytest.approx(180.0)
        assert "0. [" in evidence.render()

    def test_a_disabled_clip_is_not_part_of_the_edit(self) -> None:
        timeline = _timeline(3)
        clips = list(timeline.tracks[0].clips)
        clips[1] = clips[1].model_copy(update={"enabled": False})
        timeline = timeline.model_copy(
            update={"tracks": (timeline.tracks[0].model_copy(update={"clips": tuple(clips)}),)}
        )

        evidence = gather(timeline, target_seconds=240.0)
        # Renumbered, because the Critic's indices address what the viewer
        # sees. A note about "clip 1" must not land on footage nobody watches.
        assert [clip.index for clip in evidence.clips] == [0, 1]
        assert [clip.clip.clip_index for clip in evidence.clips] == [0, 2]

    def test_observations_are_read_from_the_clips_own_recording(self) -> None:
        class _Stored:
            def __init__(self, timestamp: float, description: str, labels: tuple[str, ...]):
                self.timestamp = timestamp
                self.description = description
                self.labels = labels

        evidence = gather(
            _timeline(2),
            target_seconds=240.0,
            observations={
                MEDIA: [_Stored(10.0, "a menu screen", ("menu",))],
                "other-media": [_Stored(10.0, "a boss fight", ("combat",))],
            },
        )

        # Clip 0 spans source 0-60 of MEDIA, so it gets the menu and nothing
        # from the recording it was not cut from.
        assert evidence.clips[0].labels == ("menu",)
        assert "a menu screen" in evidence.clips[0].descriptions
        assert evidence.clips[1].labels == ()

    def test_an_event_nobody_could_name_is_not_shown(self) -> None:
        class _Event:
            # Shaped like the real thing: an episode reads spans, and a stub
            # missing `end_seconds` fails loudly here rather than quietly in a
            # video.
            def __init__(self, start: float, kind: GameEventType):
                self.start_seconds = start
                self.end_seconds = start + 2.0
                self.event_type = kind
                self.confidence = 0.8

        evidence = gather(
            _timeline(1),
            target_seconds=240.0,
            events={
                MEDIA: [
                    _Event(5.0, GameEventType.UNKNOWN_EVENT),
                    _Event(10.0, GameEventType.DEFEAT),
                ]
            },
        )

        # Showing `unknown_event` invites a story about something nobody
        # identified, which is the failure this whole design is against.
        assert evidence.clips[0].events == ("defeat",)


# -- and what it is allowed to say -------------------------------------------


class TestTheReview:
    def test_the_model_is_shown_the_edit_it_must_review(self) -> None:
        provider = _provider({"verdict": "fine", "notes": []})
        review(_evidence(), provider=provider, intent_text="make it punchy")

        prompt_id, prompt = provider.calls[-1]
        assert prompt_id == PROMPT_ID
        assert "0. [" in prompt
        assert "make it punchy" in prompt

    def test_a_long_edit_is_capped_rather_than_shown_in_full(self) -> None:
        provider = _provider({"verdict": "", "notes": []})
        review(_evidence(_timeline(MAX_CLIPS_SHOWN + 5, seconds=5.0)), provider=provider)

        _, prompt = provider.calls[-1]
        assert f"{MAX_CLIPS_SHOWN - 1}. [" in prompt
        assert f"{MAX_CLIPS_SHOWN}. [" not in prompt

    def test_a_clip_that_does_not_exist_is_a_rejection_not_a_guess(self) -> None:
        outcome = _ask({"verdict": "", "notes": [{"clip": 9, "action": "drop"}]})

        assert isinstance(outcome, CritiqueRejection)
        assert outcome.detail == {"clip": 9, "available": 4}

    def test_two_notes_on_one_clip_are_rejected(self) -> None:
        outcome = _ask(
            {
                "verdict": "",
                "notes": [
                    {"clip": 1, "action": "trim_start", "seconds": 2},
                    {"clip": 1, "action": "drop"},
                ],
            }
        )
        assert isinstance(outcome, CritiqueRejection)

    def test_an_over_long_trim_is_capped_not_obeyed(self) -> None:
        # 90 seconds off a 60-second clip is not a trim, it is a disagreement
        # about whether the clip belongs.
        outcome = _ask(
            {"verdict": "", "notes": [{"clip": 0, "action": "trim_start", "seconds": 90}]}
        )

        assert isinstance(outcome, Critique)
        assert outcome.notes[0].seconds == pytest.approx(60.0 * MAX_TRIM_FRACTION)
        assert "capped" in outcome.notes[0].reason

    @pytest.mark.parametrize(
        "entry",
        [
            {"clip": 0, "action": "explode"},
            {"clip": 0, "action": "trim_end", "seconds": 0},
            # Measured on a real edit: 0.23 s, 0.48 s and 0.59 s alongside two
            # real trims. A required field being filled, not a judgement -- and
            # §29 and §41 move cut points at that scale for reasons made of
            # evidence, so it would be undone anyway.
            {"clip": 0, "action": "trim_start", "seconds": 0.25},
        ],
    )
    def test_a_note_that_cannot_be_acted_on_becomes_no_change(self, entry: dict) -> None:
        # One unusable note does not throw away the other nineteen. The clip
        # exists and was reviewed; the honest reduction is "no change".
        outcome = _ask({"verdict": "", "notes": [entry]})

        assert isinstance(outcome, Critique)
        assert outcome.notes[0].action is Action.KEEP
        assert outcome.actionable == ()

    def test_a_model_that_will_not_answer_is_a_rejection_not_an_exception(self) -> None:
        outcome = _ask({"verdict": "", "notes": []}, fail_times=5)
        assert isinstance(outcome, CritiqueRejection)
        assert "did not answer" in outcome.reason

    def test_no_provider_and_no_edit_both_come_back_as_reasons(self) -> None:
        assert isinstance(review(_evidence(), provider=None), CritiqueRejection)
        assert isinstance(
            review(gather(_timeline(0), target_seconds=1.0), provider=_provider()),
            CritiqueRejection,
        )


class TestTheGrammarItIsGiven:
    """The schema is what Ollama decodes with, so its bounds are behaviour.

    A string with no `maxLength` is an invitation to fill the output budget,
    and when the budget runs out mid-string the JSON is truncated and
    unparseable. Measured against the real 7B on a real edit: two runs in three
    lost all three attempts that way, each taking 59 s to generate a verdict
    that never closed its quote. With every string bounded: four runs in four,
    in 7-13 s.
    """

    def test_every_string_the_model_writes_is_bounded(self) -> None:
        note = _SCHEMA["properties"]["notes"]["items"]["properties"]
        assert _SCHEMA["properties"]["verdict"]["maxLength"] > 0
        assert note["reason"]["maxLength"] > 0
        assert _SCHEMA["properties"]["notes"]["maxItems"] == MAX_CLIPS_SHOWN

    def test_the_bounds_match_the_type_that_receives_them(self) -> None:
        # A grammar that allows more than the model accepts would produce
        # answers the validator silently truncates, which is two rules for one
        # limit and the slower one to notice.
        assert _SCHEMA["properties"]["verdict"]["maxLength"] == (
            Critique.model_fields["verdict"].metadata[0].max_length
        )
        assert (
            _SCHEMA["properties"]["notes"]["items"]["properties"]["reason"]["maxLength"]
            == Note.model_fields["reason"].metadata[0].max_length
        )

    def test_the_model_is_never_offered_an_action_that_does_nothing(self) -> None:
        # v1 offered `keep` and got eleven of them, each with a reason
        # describing a trim. A clip that is fine is one the review omits.
        offered = _SCHEMA["properties"]["notes"]["items"]["properties"]["action"]["enum"]
        assert Action.KEEP.value not in offered
        assert set(offered) == {a.value for a in Action if a.changes_the_edit}


# -- and what actually happens to the edit -----------------------------------


class TestTheRevision:
    def _apply(self, critique: Critique, *, timeline=None, target=240.0, **kwargs):
        timeline = timeline or _timeline()
        evidence = gather(timeline, target_seconds=target)
        return apply(timeline, critique, evidence, policy=POLICY, target_seconds=target, **kwargs)

    def test_a_trim_moves_the_clips_in_point_and_nothing_else(self) -> None:
        revision = self._apply(
            Critique(notes=(Note(clip=1, action=Action.TRIM_START, seconds=4.0),))
        )

        clips = revision.timeline.video_clips()
        assert clips[1].source_in == pytest.approx(200.0 + 4.0)
        assert clips[1].timeline_end - clips[1].timeline_start == pytest.approx(56.0)
        # The rest of the edit is untouched, and re-laid end to end.
        assert clips[0].source_in == pytest.approx(0.0)
        assert revision.seconds_removed == pytest.approx(4.0)
        assert any("clip 1" in note for note in revision.applied)

    def test_a_drop_disables_the_clip_rather_than_deleting_it(self) -> None:
        # §78: "remove that bit" has to be undoable. Three minutes asked for
        # against four of clips, so there is room for the drop.
        revision = self._apply(Critique(notes=(Note(clip=0, action=Action.DROP),)), target=180.0)

        clips = revision.timeline.video_clips(enabled_only=False)
        assert len(clips) == 4
        assert clips[0].enabled is False

    def test_the_duration_floor_refuses_the_change_that_would_break_it(self) -> None:
        # Four minutes of clips against a four-minute request: there is no
        # room, and §39 outranks the review.
        revision = self._apply(Critique(notes=(Note(clip=0, action=Action.DROP),)), target=240.0)

        assert revision.applied == ()
        assert any("under" in note for note in revision.refused)
        assert revision.timeline.video_clips()[0].enabled is True

    def test_small_changes_are_applied_before_large_ones(self) -> None:
        # A drop applied first would eat the whole budget and refuse three
        # trims behind it, each of which would have improved a clip.
        critique = Critique(
            notes=(
                Note(clip=0, action=Action.DROP),
                Note(clip=1, action=Action.TRIM_START, seconds=3.0),
                Note(clip=2, action=Action.TRIM_END, seconds=3.0),
            )
        )
        revision = self._apply(critique, target=225.0)

        assert len(revision.applied) == 2
        assert all("trimmed" in note for note in revision.applied)
        assert any("dropped" in note for note in revision.refused)

    def test_drops_can_be_switched_off_without_losing_the_trims(self) -> None:
        critique = Critique(
            notes=(
                Note(clip=0, action=Action.DROP),
                Note(clip=1, action=Action.TRIM_END, seconds=2.0),
            )
        )
        revision = self._apply(critique, target=200.0, allow_drops=False)

        assert len(revision.applied) == 1
        assert "trimmed" in revision.applied[0]
        assert any("switched off" in note for note in revision.refused)

    def test_a_trim_the_footage_cannot_take_is_reported_not_swallowed(self) -> None:
        # §42 refuses a trim that would leave less than a usable clip. That is
        # a fact about the recording, and the person should hear it rather than
        # find a video that quietly ignored the note.
        revision = self._apply(
            Critique(notes=(Note(clip=0, action=Action.TRIM_END, seconds=59.9),)),
            target=180.0,
        )

        assert revision.applied == ()
        assert len(revision.refused) == 1
        assert "minimum" in revision.refused[0]

    def test_an_edit_already_too_short_can_still_be_cleaned_up(self) -> None:
        # Four minutes of clips against a twenty-minute request: §39's floor
        # was missed before the Critic was asked, and applying it literally
        # would mean a video that is both too short *and* still opens on a
        # loading screen.
        revision = self._apply(
            Critique(notes=(Note(clip=0, action=Action.TRIM_START, seconds=6.0),)),
            target=1200.0,
        )

        assert len(revision.applied) == 1
        assert revision.seconds_removed == pytest.approx(6.0)

    def test_but_it_may_not_be_shrunk(self) -> None:
        revision = self._apply(Critique(notes=(Note(clip=0, action=Action.DROP),)), target=1200.0)

        # A drop takes a quarter of a four-clip edit, which is past what a
        # review of an already-short video is allowed to remove.
        assert revision.applied == ()
        assert any("under" in note for note in revision.refused)

    def test_a_critique_that_changes_nothing_changes_nothing(self) -> None:
        timeline = _timeline()
        revision = self._apply(
            Critique(verdict="it plays well", notes=(Note(clip=0, action=Action.KEEP),)),
            timeline=timeline,
        )

        assert revision.changed is False
        assert revision.timeline is timeline
