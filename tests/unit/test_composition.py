"""Phase 9: the composition description Remotion receives (SPEC §64, §66).

No Node here. The description is the whole contract with the renderer, and it
is ordinary data — which means the part most likely to be wrong, the conversion
from seconds to frames, is testable without launching a browser.

The tests that matter most are the boundary ones: that only overlay-engine
effects cross (D-008), and that an empty description is *recognisably* empty,
because that is what lets a caption-free video skip Chromium entirely.
"""

from __future__ import annotations

import json

import pytest

from backend.config.loader import load_config
from backend.core.models.enums import (
    EffectCategory,
    EffectEngine,
    EffectType,
    MomentType,
    TrackKind,
)
from backend.effects.models import EffectInstance
from backend.rendering.composition import (
    COMPOSITION_VERSION,
    build_composition,
    ceil_frames,
    resolution_for,
    seconds_to_frames,
)
from backend.timeline.captions import Caption
from backend.timeline.models import Timeline, TimelineClip, Track

pytestmark = pytest.mark.unit

MEDIA = "media-aaaaaaaaaaaa"
PROJECT = "proj-aaaaaaaaaaaa"


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture
def timeline() -> Timeline:
    clips = tuple(
        TimelineClip(
            id=f"clip-{index:012d}",
            media_id=MEDIA,
            clip_index=index,
            source_in=index * 100.0,
            source_out=index * 100.0 + 20.0,
            timeline_start=index * 20.0,
            timeline_end=index * 20.0 + 20.0,
            moment_type=MomentType.EPIC,
        )
        for index in range(3)
    )
    return Timeline(project_id=PROJECT).with_track(
        Track(kind=TrackKind.VIDEO, clips=clips)
    )


def _caption(index: int, start: float, end: float, text: str = "no way") -> Caption:
    words = text.split()
    step = (end - start) / max(len(words), 1)
    return Caption(
        id=f"cap-{index:012d}",
        index=index,
        timeline_start=start,
        timeline_end=end,
        text=text,
        language="en",
        clip_id=f"clip-{0:012d}",
        words=tuple(
            (word, start + position * step, start + (position + 1) * step)
            for position, word in enumerate(words)
        ),
    )


def _effect(engine: EffectEngine, effect: EffectType, start: float = 2.0) -> EffectInstance:
    return EffectInstance(
        effect=effect,
        engine=engine,
        category=EffectCategory.TEXT if engine is EffectEngine.REMOTION else EffectCategory.CAMERA,
        start_seconds=start,
        duration_seconds=1.5,
        clip_id="clip-000000000000",
        reason="a test",
    )


def _build(timeline, config, **kwargs):
    return build_composition(
        timeline,
        caption_config=config.captions,
        width=1920,
        height=1080,
        fps=60,
        **kwargs,
    )


class TestFrameConversion:
    """Rounding happens once, here, where it can be checked."""

    def test_seconds_become_frames_by_nearest(self) -> None:
        assert seconds_to_frames(1.234, 60) == 74
        assert seconds_to_frames(1.999, 30) == 60
        assert seconds_to_frames(0.0, 60) == 0

    def test_a_negative_time_clamps_to_zero(self) -> None:
        assert seconds_to_frames(-5.0, 60) == 0

    def test_ceil_frames_contains_the_duration(self) -> None:
        # Nearest is right for a position; a duration must not come up short.
        assert ceil_frames(1.234, 60) == 75
        assert ceil_frames(1.0, 60) == 60

    def test_a_caption_lands_on_the_frame_its_transcript_says(
        self, timeline, config
    ) -> None:
        composition = _build(timeline, config, captions=[_caption(0, 1.5, 3.0)])

        assert composition.captions[0]["from"] == 90
        assert composition.captions[0]["durationInFrames"] == 90

    def test_a_zero_length_element_still_occupies_a_frame(
        self, timeline, config
    ) -> None:
        # Nothing renders for zero frames; a caption that rounds to nothing
        # would vanish rather than flash.
        composition = _build(timeline, config, captions=[_caption(0, 1.0, 1.001)])

        assert composition.captions[0]["durationInFrames"] >= 1

    def test_fps_must_be_positive(self, timeline, config) -> None:
        with pytest.raises(ValueError, match="fps"):
            build_composition(
                timeline, caption_config=config.captions, width=1920, height=1080, fps=0
            )


class TestOnlyOverlayWork:
    """D-008: what crosses is drawn on top; nothing alters the footage."""

    def test_ffmpeg_effects_never_reach_the_renderer(self, timeline, config) -> None:
        composition = _build(
            timeline,
            config,
            effects=[
                _effect(EffectEngine.FFMPEG, EffectType.ZOOM),
                _effect(EffectEngine.REMOTION, EffectType.TEXT_POP),
            ],
        )

        assert len(composition.effects) == 1
        assert composition.effects[0]["type"] == "text_pop"

    def test_an_overlay_effect_is_placed_on_the_programme_timeline(
        self, timeline, config
    ) -> None:
        # Effects are stored relative to their clip; the overlay is drawn on the
        # finished video, so the clip's position is added back.
        effect = _effect(EffectEngine.REMOTION, EffectType.TEXT_POP, start=2.0)
        effect = effect.model_copy(update={"clip_id": "clip-000000000001"})
        composition = _build(timeline, config, effects=[effect])

        # Clip 1 starts at 20 s, so 2 s into it is 22 s of the programme.
        assert composition.effects[0]["from"] == seconds_to_frames(22.0, 60)

    def test_the_reason_travels_with_the_effect(self, timeline, config) -> None:
        # §80: a mystery graphic in the finished video must be traceable.
        composition = _build(
            timeline, config, effects=[_effect(EffectEngine.REMOTION, EffectType.IMPACT)]
        )

        assert composition.effects[0]["reason"] == "a test"


class TestEmptiness:
    """The basis for skipping the Chromium pass entirely."""

    def test_no_captions_and_no_effects_is_empty(self, timeline, config) -> None:
        assert _build(timeline, config).is_empty

    def test_ffmpeg_only_effects_leave_it_empty(self, timeline, config) -> None:
        # A video whose only decoration is a zoom needs no overlay at all.
        composition = _build(
            timeline, config, effects=[_effect(EffectEngine.FFMPEG, EffectType.ZOOM)]
        )

        assert composition.is_empty

    def test_one_caption_is_not_empty(self, timeline, config) -> None:
        assert not _build(timeline, config, captions=[_caption(0, 1.0, 2.0)]).is_empty


class TestDrawnSpans:
    """A twenty-minute video with four minutes of captions costs four minutes."""

    def test_spans_cover_the_elements(self, timeline, config) -> None:
        composition = _build(
            timeline, config, captions=[_caption(0, 1.0, 2.0), _caption(1, 40.0, 41.0)]
        )

        assert len(composition.spans) == 2
        assert composition.drawn_frames < composition.duration_in_frames

    def test_overlapping_elements_merge_into_one_span(self, timeline, config) -> None:
        composition = _build(
            timeline, config, captions=[_caption(0, 1.0, 3.0), _caption(1, 2.0, 4.0)]
        )

        assert len(composition.spans) == 1

    def test_neighbouring_elements_merge_through_the_padding(
        self, timeline, config
    ) -> None:
        # The padding exists so a fade is not clipped; it also means two
        # captions a fifth of a second apart are one span, not two.
        composition = _build(
            timeline, config, captions=[_caption(0, 1.0, 2.0), _caption(1, 2.1, 3.0)]
        )

        assert len(composition.spans) == 1

    def test_no_span_runs_past_the_video(self, timeline, config) -> None:
        composition = _build(
            timeline, config, captions=[_caption(0, 58.0, 60.0)]
        )

        for span in composition.spans:
            assert span.end_frame <= composition.duration_in_frames


class TestTheDescription:
    def test_it_is_json_serialisable_and_versioned(self, timeline, config) -> None:
        composition = _build(timeline, config, captions=[_caption(0, 1.0, 2.0)])
        payload = json.loads(json.dumps(composition.as_dict()))

        assert payload["version"] == COMPOSITION_VERSION
        assert payload["fps"] == 60
        assert payload["captions"][0]["text"] == "no way"

    def test_the_same_edit_produces_the_same_bytes(self, timeline, config) -> None:
        # §48: a render is cached against its inputs, so the description must
        # not vary between runs over unchanged data.
        first = json.dumps(_build(timeline, config, captions=[_caption(0, 1.0, 2.0)]).as_dict(),
                           sort_keys=True)
        second = json.dumps(_build(timeline, config, captions=[_caption(0, 1.0, 2.0)]).as_dict(),
                            sort_keys=True)

        assert first == second

    def test_the_style_is_resolved_into_pixels(self, timeline, config) -> None:
        # font_size_ratio is a fraction of frame height so it reads the same at
        # any resolution; the browser never has to know the rule.
        composition = _build(timeline, config)

        expected = round(1080 * config.captions.appearance.font_size_ratio)
        assert composition.style["fontSizePx"] == expected

    def test_captions_arrive_pre_wrapped(self, timeline, config) -> None:
        # The renderer measures its own text; the sidecar files do not. Wrapping
        # once means the two cannot disagree.
        long_text = " ".join(["word"] * 30)
        composition = _build(timeline, config, captions=[_caption(0, 1.0, 4.0, long_text)])

        lines = composition.captions[0]["lines"]
        assert 1 <= len(lines) <= config.captions.layout.max_lines

    def test_word_timings_survive_into_frames(self, timeline, config) -> None:
        composition = _build(timeline, config, captions=[_caption(0, 1.0, 3.0, "one two three")])
        words = composition.captions[0]["words"]

        assert len(words) == 3
        for word in words:
            assert word["to"] > word["from"]

    def test_the_duration_matches_the_edit(self, timeline, config) -> None:
        composition = _build(timeline, config)

        assert composition.duration_in_frames == seconds_to_frames(timeline.duration, 60)


class TestResolution:
    def test_common_heights_map_to_even_widths(self) -> None:
        assert resolution_for(1080, "16:9") == (1920, 1080)
        assert resolution_for(720, "16:9") == (1280, 720)
        assert resolution_for(1080, "1:1") == (1080, 1080)

    def test_an_unknown_aspect_ratio_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="aspect ratio"):
            resolution_for(1080, "21:9")
