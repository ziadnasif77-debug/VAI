"""What a style asks of its captions (V2-P2.5).

Captions were the one editorial layer with no style doctrine: the word did not
appear in `config/style.yaml`, every style rendered byte-identical text, and a
`Caption.style` column round-tripped the empty object on all 312 captions ever
produced without reaching the renderer.

The tests that matter here are the ones that would fail if the doctrine were a
document nobody reads. So each one ends at a value the renderer actually draws
with, and the neutrality tests assert *identity* rather than equality -- a copy
that happens to agree with the house is not the same thing as the house.
"""

from __future__ import annotations

import pytest

from backend.config.schema import StyleCaptionsConfig
from backend.rendering.composition import _style as caption_style_payload
from backend.style import bible

pytestmark = pytest.mark.unit


class TestNeutrality:
    def test_a_style_with_no_opinion_returns_the_callers_own_object(
        self, config
    ) -> None:
        # `is`, not `==`. This is what makes "the house renders what it always
        # rendered" a consequence of the code rather than a promise about it:
        # there is no copy that could drift, because there is no copy.
        neutral = StyleCaptionsConfig()
        assert neutral.applied_to(config.captions) is config.captions

    @pytest.mark.parametrize("name", ["default", "cinematic", "minimal", "gaming_fast"])
    def test_every_style_that_declares_nothing_gets_the_house_captions(
        self, config, name: str
    ) -> None:
        style = bible.resolve(config, name)
        assert style.captions.applied_to(config.captions) is config.captions

    def test_a_style_that_will_not_resolve_falls_back_to_the_house(
        self, config
    ) -> None:
        # A caption colour is not worth failing a render over, and the fallback
        # is the same object rather than a reconstruction of it.

        class Broken:
            def fetch_all(self, *_args, **_kwargs):
                raise RuntimeError("no database")

            def fetch_one(self, *_args, **_kwargs):
                raise RuntimeError("no database")

        assert (
            bible.captions_for(Broken(), config, "proj-missing") is config.captions
        )


class TestTheDoctrineIsRead:
    def test_funny_lands_its_punchline_bigger_than_the_house(self, config) -> None:
        funny = bible.resolve(config, "funny").captions.applied_to(config.captions)
        assert funny is not config.captions
        assert funny.appearance.font_size_ratio > config.captions.appearance.font_size_ratio

    def test_funny_lights_the_word_in_its_own_colour(self, config) -> None:
        funny = bible.resolve(config, "funny").captions.applied_to(config.captions)
        assert funny.appearance.highlight_color != config.captions.appearance.highlight_color

    def test_funny_keeps_the_house_motion_by_saying_nothing_about_it(
        self, config
    ) -> None:
        # The house already fades a caption in and lights the spoken word.
        # Funny wants both, so it declares neither -- and inherits them.
        declared = bible.resolve(config, "funny").captions
        assert declared.animated is None
        assert declared.word_highlighting is None
        funny = declared.applied_to(config.captions)
        assert funny.style == config.captions.style
        assert funny.word_highlighting == config.captions.word_highlighting

    def test_competitive_stops_the_captions_performing(self, config) -> None:
        # Clarity over decoration, in the layer drawn on top of the play.
        comp = bible.resolve(config, "competitive").captions.applied_to(config.captions)
        assert comp is not config.captions
        assert comp.style == "standard"
        assert comp.word_highlighting is False

    def test_competitive_changes_nothing_it_did_not_declare(self, config) -> None:
        comp = bible.resolve(config, "competitive").captions.applied_to(config.captions)
        assert comp.appearance.font_size_ratio == config.captions.appearance.font_size_ratio
        assert comp.appearance.highlight_color == config.captions.appearance.highlight_color
        assert comp.min_confidence == config.captions.min_confidence
        assert comp.layout == config.captions.layout
        assert comp.timing == config.captions.timing


class TestItReachesSomethingDrawn:
    """Every declared field must end at a value a renderer draws with.

    `captions.emphasis` is the counter-example this guards against: it is
    passed to the renderer and declared in its TypeScript schema, and no
    component reads it. A doctrine field that behaved like that would be
    decoration, so each of these ends at a key `Caption.tsx` consumes.
    """

    def _payload(self, config, name: str) -> dict:
        resolved = bible.resolve(config, name).captions.applied_to(config.captions)
        return caption_style_payload(resolved, width=1920, height=1080)

    def test_the_house_payload_is_the_baseline(self, config) -> None:
        house = caption_style_payload(config.captions, width=1920, height=1080)
        assert house["animated"] is True
        assert house["wordHighlighting"] is True

    def test_funnys_font_size_survives_into_pixels(self, config) -> None:
        house = caption_style_payload(config.captions, width=1920, height=1080)
        assert self._payload(config, "funny")["fontSizePx"] > house["fontSizePx"]

    def test_funnys_highlight_colour_survives(self, config) -> None:
        house = caption_style_payload(config.captions, width=1920, height=1080)
        assert self._payload(config, "funny")["highlightColor"] != house["highlightColor"]

    def test_competitive_arrives_with_the_motion_off(self, config) -> None:
        payload = self._payload(config, "competitive")
        # `animated` gates the fade and the rise; `wordHighlighting` gates the
        # travelling colour. Both are read by Caption.tsx.
        assert payload["animated"] is False
        assert payload["wordHighlighting"] is False

    @pytest.mark.parametrize("name", ["default", "cinematic", "minimal", "gaming_fast"])
    def test_a_style_with_no_doctrine_draws_exactly_the_house(
        self, config, name: str
    ) -> None:
        house = caption_style_payload(config.captions, width=1920, height=1080)
        assert self._payload(config, name) == house


class TestTheDeadFieldIsGone:
    def test_a_caption_carries_no_style_of_its_own(self) -> None:
        # It was written empty, stored empty, loaded empty and dropped before
        # the renderer. Per-style appearance is a taste, and belongs with the
        # other tastes rather than on a per-row column.
        from backend.timeline.captions import Caption

        assert not hasattr(Caption("c", 0, 0.0, 1.0, "text"), "style")
        assert "style" not in Caption("c", 0, 0.0, 1.0, "text").as_row()
