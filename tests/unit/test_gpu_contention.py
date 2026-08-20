"""Whose model is on the card (§54, shared-infrastructure 5.4).

§54's "one heavy model at a time" is honoured between this project's own
stages, and says nothing at all about the rest of the machine. Inspecting the
machine showed what that omission costs.

The shared Ollama store on this machine holds five models and serves two
programs. Two of the five are VAI's. `qwen2.5-coder:7b` belongs to an OpenHands
install that reaches the *same daemon* from Docker through
`host.docker.internal:11434` — and that is the tag that was once found resident
with an expiry in the year 2318, holding 4.7 GB while a render waited twenty
minutes to discover it.

So: report, never release. Another program's model may be mid-request, and
taking its memory is precisely the discourtesy this project asks not to
receive.
"""

from __future__ import annotations

import pytest

from backend.core import gpu

pytestmark = pytest.mark.unit


@pytest.fixture
def card(monkeypatch):
    """Put a known set of models on a known card."""

    def place(resident, free_mb=1200):
        monkeypatch.setattr(
            gpu, "resident_models", lambda: [gpu.ResidentModel(n, mb) for n, mb in resident]
        )
        monkeypatch.setattr(gpu, "free_vram_mb", lambda *a, **k: free_mb)

    return place


class TestWhoseModelIsIt:
    def test_the_configured_tags_are_ours(self, config) -> None:
        ours = gpu.our_models(config)

        assert config.models.llm.model in ours
        assert config.models.vision.model in ours

    def test_a_model_nobody_configured_is_somebody_elses(self, config, card) -> None:
        # The real pairing on this machine, to the megabyte.
        card([(config.models.llm.model, 4528), ("qwen2.5-coder:7b", 4700)])

        foreign = gpu.foreign_models(config)

        assert [model.name for model in foreign] == ["qwen2.5-coder:7b"]

    def test_an_empty_card_belongs_to_nobody(self, config, card) -> None:
        card([], free_mb=7300)

        assert gpu.foreign_models(config) == []

    def test_our_own_models_are_never_called_foreign(self, config, card) -> None:
        card([(config.models.vision.model, 5202)])

        assert gpu.foreign_models(config) == []


class TestWhatItSays:
    def test_it_names_the_holder_and_the_megabytes(self, config, card) -> None:
        card([("qwen2.5-coder:7b", 4700)], free_mb=509)

        held = gpu.contention(config)

        assert held["foreign_vram_mb"] == 4700
        assert "qwen2.5-coder:7b" in held["message"]
        # And says plainly that it is not going to do anything about it.
        assert "will not unload" in held["message"]

    def test_an_uncontended_card_falls_back_to_the_pressure_reading(self, config, card) -> None:
        card([], free_mb=7300)

        assert "7300 MB" in gpu.contention(config)["message"]

    def test_it_adds_up_several_holders(self, config, card) -> None:
        card([("qwen2.5-coder:7b", 4700), ("nomic-embed-text", 300)])

        held = gpu.contention(config)

        assert held["foreign_vram_mb"] == 5000
        assert len(held["foreign_models"]) == 2

    def test_a_card_that_cannot_be_read_still_answers(self, config, monkeypatch) -> None:
        # A machine without nvidia-smi is a machine this still has to run on.
        monkeypatch.setattr(gpu, "resident_models", list)
        monkeypatch.setattr(gpu, "free_vram_mb", lambda *a, **k: None)

        assert gpu.contention(config)["message"]


def test_nothing_here_releases_anything(config, card, monkeypatch) -> None:
    """The property the whole design turns on.

    A shared daemon means an unload is a decision about another program's
    work, and this project makes exactly one: it does not.
    """
    released: list[str] = []
    monkeypatch.setattr(gpu, "release_models", lambda names: released.extend(names))
    card([("qwen2.5-coder:7b", 4700)])

    gpu.foreign_models(config)
    gpu.contention(config)

    assert released == []
