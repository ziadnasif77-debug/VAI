"""P0.3 — AuthorizedSpan: the contract, the closed granter set, the validator.

The brief's tests 1, 2, 3, 6, 9 and the owner's tenth (no granter may
authorise into an exclusion) live here, named after the rule each guards.
Tests 4, 5, 7 and 8 live where the actor they guard lives.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from backend.core.models.enums import TrackKind
from backend.timeline import authorization as auth
from backend.timeline import validation
from backend.timeline.models import Timeline, TimelineClip, Track

pytestmark = pytest.mark.unit

MEDIA = "media-aaaaaaaaaaaa"


def _grant(start: float, end: float, granter=auth.Granter.CONTEXT_EXPANSION, reason="context"):
    return auth.AuthorizedSpan(MEDIA, start, end, granter, reason)


def _timeline(source_in: float, source_out: float, spans) -> Timeline:
    clip = TimelineClip(
        id="clip-000000000001",
        media_id=MEDIA,
        clip_index=0,
        source_in=source_in,
        source_out=source_out,
        timeline_start=0.0,
        timeline_end=source_out - source_in,
        metadata={"authorized": [span.to_dict() for span in spans]} if spans is not None else {},
    )
    return Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
        Track(kind=TrackKind.VIDEO, clips=(clip,))
    )


class TestTheContract:
    def test_p0_3_a_clip_inside_its_authorized_span_passes(self) -> None:
        report = validation.validate(
            _timeline(100.0, 130.0, [_grant(95.0, 140.0)]), require_authorization=True
        )
        assert report.is_valid, [str(f) for f in report.findings]

    def test_p0_3_narrowing_an_authorized_span_passes(self) -> None:
        # The grant says 95-140; the clip shows 110-120. Narrowing needs no one.
        report = validation.validate(
            _timeline(110.0, 120.0, [_grant(95.0, 140.0)]), require_authorization=True
        )
        assert report.is_valid

    def test_p0_3_widening_without_a_new_grant_fails_hard(self) -> None:
        report = validation.validate(_timeline(90.0, 130.0, [_grant(95.0, 140.0)]))
        assert not report.is_valid
        (finding,) = report.errors
        assert finding.code == "unauthorized_span"
        assert "granted by context_expansion" in finding.message
        with pytest.raises(validation.ValidationError):
            validation.require_valid(_timeline(90.0, 130.0, [_grant(95.0, 140.0)]))

    def test_p0_3_a_new_grant_by_a_listed_granter_widens(self) -> None:
        # Widening is a NEW span, newest governs; the old one is untouched.
        first = _grant(95.0, 140.0)
        second = auth.AuthorizedSpan(
            MEDIA, 80.0, 140.0, auth.Granter.DURATION_OPTIMIZER, "+15 s before, towards target"
        )
        report = validation.validate(
            _timeline(85.0, 130.0, [first, second]), require_authorization=True
        )
        assert report.is_valid
        assert first.start == 95.0, "the original span is immutable"

    def test_p0_3_a_clip_with_no_span_fails_where_authorization_is_required(self) -> None:
        required = validation.validate(_timeline(100.0, 130.0, []), require_authorization=True)
        assert [f.code for f in required.errors] == ["unauthorized_span"]
        # History stays readable: an older stored timeline is not an error
        # unless the caller asks for authorization.
        assert validation.validate(_timeline(100.0, 130.0, None)).is_valid


class TestTheGranterSetIsClosed:
    @pytest.mark.parametrize("impostor", ["style", "critic", "jump_cut", "context_expansion", ""])
    def test_p0_3_an_unlisted_granter_is_refused(self, impostor) -> None:
        # A string -- even one spelling a member's value -- is not a member.
        with pytest.raises(auth.AuthorizationError, match="not a listed granter"):
            auth.AuthorizedSpan(MEDIA, 0.0, 10.0, impostor, "typed in")  # type: ignore[arg-type]
        with pytest.raises(auth.AuthorizationError, match="not a listed granter"):
            auth.issue(MEDIA, 0.0, 10.0, impostor, "typed in")  # type: ignore[arg-type]

    def test_p0_3_an_unlisted_granter_in_stored_metadata_is_refused_too(self) -> None:
        clip = TimelineClip(
            id="clip-000000000001",
            media_id=MEDIA,
            clip_index=0,
            source_in=0.0,
            source_out=10.0,
            timeline_start=0.0,
            timeline_end=10.0,
            metadata={
                "authorized": [
                    {"media_id": MEDIA, "start": 0, "end": 10, "granted_by": "style", "reason": "x"}
                ]
            },
        )
        timeline = Timeline(project_id="proj-aaaaaaaaaaaa").with_track(
            Track(kind=TrackKind.VIDEO, clips=(clip,))
        )
        report = validation.validate(timeline)
        assert [f.code for f in report.errors] == ["unauthorized_granter"]

    def test_p0_3_every_listed_granter_is_derived_from_the_plan_or_the_spec(self) -> None:
        assert {g.value for g in auth.Granter} == {
            "moment_core",
            "context_expansion",
            "duration_optimizer",
            "refinement",
            "screen_guard_rescue",
            "human",
            "jl_cut",
        }

    def test_p0_3_a_grant_must_say_why(self) -> None:
        with pytest.raises(auth.AuthorizationError, match="must say why"):
            auth.AuthorizedSpan(MEDIA, 0.0, 10.0, auth.Granter.HUMAN, "  ")

    def test_p0_3_jl_cut_is_audio_only_and_nothing_else_is(self) -> None:
        with pytest.raises(auth.AuthorizationError, match="audio only"):
            auth.AuthorizedSpan(MEDIA, 0.0, 1.0, auth.Granter.JL_CUT, "lead")
        lead = auth.AuthorizedSpan(MEDIA, 0.0, 1.0, auth.Granter.JL_CUT, "lead", audio_only=True)
        assert lead.audio_only
        with pytest.raises(auth.AuthorizationError, match="cannot be audio-only"):
            auth.AuthorizedSpan(MEDIA, 0.0, 1.0, auth.Granter.HUMAN, "h", audio_only=True)
        # An audio-only span never governs the picture.
        assert auth.newest_for([lead], MEDIA) is None
        assert auth.newest_for([lead], MEDIA, audio=True) is lead


class TestNoGranterCanAuthorizeIntoAnExclusion:
    EXCLUSIONS: ClassVar[list[tuple[float, float]]] = [(520.0, 531.0)]

    @pytest.mark.parametrize("granter", [g for g in auth.Granter if g is not auth.Granter.JL_CUT])
    def test_p0_3_no_granter_can_authorize_into_an_exclusion(self, granter) -> None:
        # Asked for 515-540 across a menu at 520-531: the grant is the largest
        # gameplay piece, 531-540, whoever asked -- the human included.
        span = auth.issue(MEDIA, 515.0, 540.0, granter, "wanted it all", exclusions=self.EXCLUSIONS)
        assert (span.start, span.end) == (531.0, 540.0)
        assert "cut back to gameplay" in span.reason
        # And nothing at all inside the menu is refused outright.
        with pytest.raises(auth.AuthorizationError, match="no granter may authorise"):
            auth.issue(MEDIA, 522.0, 528.0, granter, "the menu itself", exclusions=self.EXCLUSIONS)

    def test_p0_3_an_audio_lead_cannot_enter_an_exclusion_either(self) -> None:
        with pytest.raises(auth.AuthorizationError, match="no granter may authorise"):
            auth.issue(
                MEDIA, 530.5, 531.0, auth.Granter.JL_CUT, "lead",
                exclusions=self.EXCLUSIONS, audio_only=True,
            )

    def test_p0_3_a_clean_request_is_granted_whole(self) -> None:
        span = auth.issue(MEDIA, 600.0, 630.0, auth.Granter.HUMAN, "ok", exclusions=self.EXCLUSIONS)
        assert (span.start, span.end, span.reason) == (600.0, 630.0, "ok")


class TestTheCheckIsLoadBearing:
    def test_p0_3_removing_the_check_lets_a_widened_clip_through(self, monkeypatch) -> None:
        # Mutation test: the widened clip fails only because the check runs.
        widened = _timeline(90.0, 130.0, [_grant(95.0, 140.0)])
        assert not validation.validate(widened).is_valid
        monkeypatch.setattr(validation, "_check_authorization", lambda timeline, *, require: [])
        assert validation.validate(widened).is_valid, "with the check gone, nothing else catches it"
        monkeypatch.undo()
        assert not validation.validate(widened).is_valid


class TestSerialization:
    def test_p0_3_a_span_survives_the_metadata_round_trip(self) -> None:
        span = auth.AuthorizedSpan(MEDIA, 1.2345, 9.8765, auth.Granter.REFINEMENT, "snap +0.4 s")
        again = auth.AuthorizedSpan.from_dict(span.to_dict())
        assert again.granted_by is auth.Granter.REFINEMENT
        assert (again.start, again.end) == (1.234, 9.877)
        assert auth.spans_from_metadata({"authorized": [span.to_dict()]}) == (again,)
        assert auth.spans_from_metadata({}) == ()
