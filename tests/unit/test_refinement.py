"""The last pass over the cut points (backend/narrative/refinement.py).

Every rule here was paid for by a measured defect on a finished video:

* 8 of 26 cut points landed mid-sentence — one clip cut the player off
  mid-word, and the next clip resumed inside the same sentence.
* Two clips continued the same footage and were joined anyway, which put a
  30 ms audio fade in the middle of continuous speech.
* The first fix snapped one clip's end *into the next clip's span* — the same
  two seconds of footage twice — and the EDL's exclusivity guard trimmed it,
  leaving the plan and the timeline disagreeing about the same clip.

The shapes below are those defects, reduced.
"""

from __future__ import annotations

import pytest

from ai.providers.base import TranscriptSegment, TranscriptWord
from backend.core.models.enums import MomentType
from backend.moments.formation import Moment
from backend.narrative.refinement import SpeechIndex, refine

pytestmark = pytest.mark.unit


def _word(start: float, end: float) -> TranscriptWord:
    return TranscriptWord(word="x", start=start, end=end)


def _speech(*runs: tuple[float, float]) -> dict[str, list[TranscriptSegment]]:
    """Continuous word runs; the gaps between runs are the pauses."""
    words: list[TranscriptWord] = []
    for start, end in runs:
        position = start
        while position + 0.4 <= end:
            words.append(_word(position, position + 0.4))
            position += 0.45
    return {
        "m": [TranscriptSegment(start=runs[0][0], end=runs[-1][1], text="كلام", words=tuple(words))]
    }


def _moment(
    context_start: float,
    context_end: float,
    *,
    start: float | None = None,
    end: float | None = None,
) -> Moment:
    return Moment(
        media_id="m",
        moment_type=MomentType.SURPRISE,
        start_seconds=context_start + 2.0 if start is None else start,
        end_seconds=context_end - 2.0 if end is None else end,
        events=(),
        context_start=context_start,
        context_end=context_end,
    )


class TestSnappingToPauses:
    #: Speech to 52.45, a pause, speech again from 53.2. The pause midpoint
    #: is the professional cut point.
    SPEECH = _speech((37.2, 52.5), (53.2, 70.0))

    def test_a_mid_word_cut_moves_to_the_pause(self) -> None:
        result = refine([_moment(30.0, 54.4, end=50.0)], ["body"], self.SPEECH)

        index = SpeechIndex(self.SPEECH["m"], 0.35)
        assert result.snapped_cuts == 1
        assert not index.inside_speech(result.moments[0].context_end)

    def test_a_cut_already_in_silence_is_left_alone(self) -> None:
        # 20.0 is before any speech: there is nothing to fix, and a refinement
        # that moves a good cut is making work, not removing it.
        result = refine([_moment(20.0, 52.83, end=50.0)], ["body"], self.SPEECH)

        assert result.snapped_cuts == 0
        assert result.moments[0].context_start == 20.0

    def test_the_core_span_is_never_given_back(self) -> None:
        # The nearest pause is behind the last event. A mid-word cut is a
        # smaller failure than a missing kill, so the cut stays where it is.
        result = refine([_moment(30.0, 54.4, end=54.0)], ["body"], self.SPEECH)

        assert result.moments[0].context_end == 54.4

    def test_no_words_at_all_changes_nothing(self) -> None:
        result = refine([_moment(10.0, 20.0)], ["body"], {"m": []})

        assert result.snapped_cuts == 0


class TestWhereInsideThePause:
    """Leake et al. (SIGGRAPH 2017): ~90% of a retained gap belongs before the
    incoming line. The midpoint left both clips hanging in half a silence each;
    the split resumes speech almost as soon as the new shot lands.
    """

    def _index(self, *words: tuple[float, float], **kwargs) -> SpeechIndex:
        segment = TranscriptSegment(
            start=words[0][0],
            end=words[-1][1],
            text="كلام",
            words=tuple(_word(start, end) for start, end in words),
        )
        return SpeechIndex([segment], 0.35, **kwargs)

    def test_a_long_gap_splits_ninety_ten(self) -> None:
        # Gap of 3.0s between word runs: the cut belongs 0.3s after the
        # outgoing words, leaving 2.7s of lead-in for the incoming clip.
        index = self._index((5.0, 10.0), (13.0, 15.0))

        assert index.pauses[1] == pytest.approx(10.3)

    def test_the_tail_never_shrinks_below_the_consonant_floor(self) -> None:
        # Gap of 1.0s: ten percent is 0.1s, inside the window Whisper's
        # word-end timestamps clip trailing S/P sounds -- the floor governs.
        index = self._index((5.0, 10.0), (11.0, 15.0))

        assert index.pauses[1] == pytest.approx(10.2)

    def test_a_gap_too_short_for_both_clearances_yields_no_candidate(self) -> None:
        # 0.36s cannot hold the 0.2s clearance on both sides. Any position in
        # it sits against a word's breath -- the old midpoint fallback put a
        # candidate 0.11s from a word edge whenever min_pause was tuned below
        # twice the pad, which inside_speech itself would call mid-speech.
        index = self._index((5.0, 9.30), (9.66, 15.0))

        assert len(index.pauses) == 2, "only the before-first and after-last edges"

    def test_a_snapped_cut_lands_early_in_the_pause(self) -> None:
        # End-to-end through refine(): a mid-word out-point moves to the
        # split position, not the midpoint the old rule chose.
        speech = _speech((37.2, 52.5), (54.45, 70.0))
        result = refine([_moment(30.0, 54.6, end=52.0)], ["body"], speech)

        index = SpeechIndex(speech["m"], 0.35)
        moved = result.moments[0].context_end
        assert result.snapped_cuts == 1
        assert not index.inside_speech(moved)
        assert moved == pytest.approx(52.65), "left words end 52.45; tail is the 0.2 floor"


class TestTheFootageIsBounded:
    SPEECH = _speech((0.0, 17.0))

    def test_a_snap_never_leaves_the_recording(self) -> None:
        # The transcript happily timestamps words past the end of the file it
        # heard; a "pause" after the last of them points at footage that does
        # not exist, and the EDL would clamp it — plan and timeline would then
        # disagree about the same clip.
        result = refine(
            [_moment(0.0, 15.0, end=13.0)],
            ["body"],
            self.SPEECH,
            duration_by_media={"m": 15.0},
        )

        assert result.moments[0].context_end <= 15.0

    def test_a_snap_never_enters_another_clips_span(self) -> None:
        # The defect that broke the EDL contract: the body clip's end snapped
        # forward into the hook's span, showing the same footage twice.
        hook = _moment(15.0, 40.0, start=17.0, end=38.0)
        body = _moment(0.0, 15.0, start=2.0, end=13.0)

        result = refine([hook, body], ["hook", "body"], self.SPEECH)

        refined_body = result.moments[1]
        assert refined_body.context_end <= 15.0 + 0.05

    def test_a_start_never_backs_into_the_previous_clip(self) -> None:
        # A tempting pause at ~9.15 sits *inside the previous clip's span*.
        # Without the claims floor it is the nearest pause and would be
        # chosen, replaying the previous clip's tail. With it, the boundary
        # stays where it was: no pause exists in the unclaimed footage.
        speech = _speech((0.0, 9.3), (9.7, 17.0))
        earlier = _moment(0.0, 10.0, start=2.0, end=8.0)
        later = _moment(10.4, 30.0, start=14.0, end=28.0)

        result = refine([earlier, later], ["body", "body"], speech)

        assert len(result.moments) == 2, "0.4s of unused footage is not adjacency"
        assert result.moments[1].context_start == 10.4


class TestMergingContinuousFootage:
    def test_adjacent_clips_become_one(self) -> None:
        # Every join carries a 30 ms audio fade (§72); in continuous speech
        # that is a dip mid-sentence for no visual gain at all.
        first = _moment(0.0, 10.0)
        second = _moment(10.0, 25.0)

        result = refine([first, second], ["climax", "body"], {"m": []})

        assert result.merged_clips == 1
        assert len(result.moments) == 1
        assert result.moments[0].context_start == 0.0
        assert result.moments[0].context_end == 25.0

    def test_the_merged_clip_keeps_the_arrived_beat(self) -> None:
        result = refine([_moment(0.0, 10.0), _moment(10.0, 25.0)], ["climax", "body"], {"m": []})

        assert result.beats == ("climax",)

    def test_a_forward_overlap_is_one_situation(self) -> None:
        # A real plan arrived with consecutive clips overlapping by 3.5s --
        # the same scene twice, which the EDL would trim silently mid-word.
        first = _moment(100.0, 140.0, start=105.0, end=138.0)
        second = _moment(136.5, 170.0, start=140.0, end=168.0)

        result = refine([first, second], ["body", "body"], {"m": []})

        assert result.merged_clips == 1
        assert result.moments[0].context_start == 100.0
        assert result.moments[0].context_end == 170.0

    def test_a_hook_jumping_backwards_is_never_merged(self) -> None:
        # The regression this rule shipped with: the hook plays 15-40, then
        # the body starts at 0. "Starts before the previous ended" is true of
        # that deliberate jump too, and the first cut of the overlap rule
        # merged the hook and the whole body into one clip.
        hook = _moment(15.0, 40.0, start=17.0, end=38.0)
        body = _moment(0.0, 15.0, start=2.0, end=13.0)

        result = refine([hook, body], ["hook", "body"], {"m": []})

        assert result.merged_clips == 0
        assert len(result.moments) == 2

    def test_clips_with_footage_between_them_stay_apart(self) -> None:
        result = refine([_moment(0.0, 10.0), _moment(30.0, 45.0)], ["body", "body"], {"m": []})

        assert result.merged_clips == 0
        assert len(result.moments) == 2

    def test_different_recordings_never_merge(self) -> None:
        first = _moment(0.0, 10.0)
        second = Moment(
            media_id="other",
            moment_type=MomentType.SURPRISE,
            start_seconds=12.0,
            end_seconds=20.0,
            events=(),
            context_start=10.0,
            context_end=22.0,
        )

        result = refine([first, second], ["body", "body"], {"m": [], "other": []})

        assert result.merged_clips == 0


class TestDisabled:
    def test_disabled_means_untouched(self) -> None:
        moments = [_moment(0.0, 10.0), _moment(10.0, 25.0)]

        result = refine(moments, ["body", "body"], {"m": []}, enabled=False)

        assert list(result.moments) == moments
        assert result.snapped_cuts == 0 and result.merged_clips == 0
