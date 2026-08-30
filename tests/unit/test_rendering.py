"""Phase 10: encoder arguments and the audio mix (SPEC §65, §72–§75).

Neither of these needs to encode anything. The encoder's flags are a list, the
mix is a filter-graph string and a gain envelope is an array — which is
deliberate, because a wrong flag costs twenty minutes to discover if the only
way to see it is to render.

The tests that matter most here are the ones about §72's priority and §74's
ducking, because those are decisions with an audible consequence and no
automatic check downstream: nothing in QA can tell you the music was too loud
under the speech.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from backend.config.loader import load_config
from backend.core.errors import RenderError
from backend.rendering import audio_mix
from backend.rendering.encoder import (
    EncoderChoice,
    EncodeTarget,
    audio_arguments,
    container_arguments,
    intermediate_arguments,
    video_arguments,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture
def target(config) -> EncodeTarget:
    return EncodeTarget.from_preset(config.youtube_preset, width=1920)


NVENC = EncoderChoice(name="h264_nvenc", hardware=True, reason="test")
X264 = EncoderChoice(name="libx264", hardware=False, reason="test")


def _value(arguments: list[str], flag: str) -> str | None:
    return arguments[arguments.index(flag) + 1] if flag in arguments else None


class TestEncoderArguments:
    def test_the_hardware_encoder_is_driven_by_bitrate(self, target, config) -> None:
        arguments = video_arguments(NVENC, target, config.render)

        assert _value(arguments, "-c:v") == "h264_nvenc"
        assert _value(arguments, "-b:v") == config.render.bitrate_for(1080, 60)
        assert "-crf" not in arguments, "NVENC's rate control is not CRF"

    def test_the_cpu_encoder_is_driven_by_quality(self, target, config) -> None:
        arguments = video_arguments(X264, target, config.render)

        assert _value(arguments, "-crf") == str(config.render.libx264.crf)
        assert _value(arguments, "-preset") == config.render.libx264.preset

    def test_both_carry_a_ceiling(self, target, config) -> None:
        # Quality-targeted encoding can spike far above what a platform accepts.
        for choice in (NVENC, X264):
            arguments = video_arguments(choice, target, config.render)
            assert _value(arguments, "-maxrate")
            assert _value(arguments, "-bufsize")

    def test_the_gop_is_expressed_in_frames_for_the_target_rate(
        self, config
    ) -> None:
        # A GOP in frames drifts the moment someone renders at 30 instead of 60.
        at_60 = video_arguments(
            X264, EncodeTarget(width=1920, height=1080, fps=60), config.render
        )
        at_30 = video_arguments(
            X264, EncodeTarget(width=1280, height=720, fps=30), config.render
        )

        assert int(_value(at_60, "-g")) == round(config.render.gop_seconds * 60)
        assert int(_value(at_30, "-g")) == round(config.render.gop_seconds * 30)

    def test_an_unknown_resolution_still_gets_a_bitrate(self, config) -> None:
        # An unfamiliar format is not the moment to encode at an accidental
        # default of zero.
        arguments = video_arguments(
            NVENC, EncodeTarget(width=2560, height=1440, fps=60), config.render
        )

        assert _value(arguments, "-b:v")

    def test_the_audio_flags_follow_the_preset(self, target, config) -> None:
        arguments = audio_arguments(target)

        assert _value(arguments, "-c:a") == config.youtube_preset.audio_codec
        assert _value(arguments, "-b:a") == config.youtube_preset.audio_bitrate
        assert _value(arguments, "-ar") == str(config.youtube_preset.audio_sample_rate)

    def test_faststart_is_requested_for_mp4(self, config) -> None:
        # What lets a browser start playing before the file has arrived.
        assert "+faststart" in container_arguments(config.render)

    def test_the_intermediate_is_visually_lossless(self, config) -> None:
        # Every artefact introduced in a segment survives the second encode.
        cpu = intermediate_arguments(X264, config.render)
        assert int(_value(cpu, "-crf")) <= 18

        gpu = intermediate_arguments(NVENC, config.render)
        assert int(_value(gpu, "-qp")) <= 20

    def test_an_unparsable_bitrate_is_a_typed_error(self, target, config) -> None:
        broken = config.render.model_copy(update={"bitrate": {"1080p60": "loads"}})

        with pytest.raises(RenderError, match="bitrate"):
            video_arguments(NVENC, target, broken)


class TestDuckingEnvelope:
    """§74, computed here so it is exact rather than guessed by a compressor."""

    def test_silence_outside_the_spans(self, config) -> None:
        spans = audio_mix.speech_spans([(4.0, 5.0)], config.audio)
        envelope = audio_mix.build_envelope(
            spans, duration_seconds=10.0, config=config.audio
        )

        assert envelope[0] == pytest.approx(1.0)
        assert envelope[-1] == pytest.approx(1.0)

    def test_the_configured_depth_is_reached(self, config) -> None:
        spans = audio_mix.speech_spans([(4.0, 6.0)], config.audio)
        envelope = audio_mix.build_envelope(
            spans, duration_seconds=10.0, config=config.audio
        )
        middle = envelope[int(5.0 * audio_mix.ENVELOPE_SAMPLE_RATE)]

        expected = 10 ** (config.audio.ducking.speech_duck_db / 20)
        assert middle == pytest.approx(expected, rel=1e-3)

    def test_the_ramp_is_gradual_rather_than_a_step(self, config) -> None:
        # A step clicks, and a click is more noticeable than the music.
        spans = audio_mix.speech_spans([(4.0, 6.0)], config.audio)
        envelope = audio_mix.build_envelope(
            spans, duration_seconds=10.0, config=config.audio
        )
        rate = audio_mix.ENVELOPE_SAMPLE_RATE
        attack = envelope[int(3.9 * rate) : int(4.0 * rate)]

        assert attack.max() > attack.min(), "the attack is a cliff"
        assert np.all(np.diff(attack) <= 1e-6), "the attack must fall monotonically"

    def test_the_music_gives_way_further_than_the_gameplay(self, config) -> None:
        # §72's whole point: Speech > Important Game Audio > Music.
        spoken = [(2.0, 4.0)]
        music = audio_mix.build_envelope(
            audio_mix.speech_spans(spoken, config.audio),
            duration_seconds=8.0,
            config=config.audio,
        )
        game = audio_mix.build_envelope(
            audio_mix.game_under_speech_spans(spoken, config.audio),
            duration_seconds=8.0,
            config=config.audio,
        )
        at = int(3.0 * audio_mix.ENVELOPE_SAMPLE_RATE)

        assert music[at] < game[at] < 1.0

    def test_overlapping_spans_duck_once_by_the_deeper_amount(self, config) -> None:
        # Not twice into inaudibility.
        spans = [
            *audio_mix.speech_spans([(2.0, 4.0)], config.audio),
            *audio_mix.event_spans([(2.5, 3.5)], config.audio),
        ]
        envelope = audio_mix.build_envelope(
            spans, duration_seconds=8.0, config=config.audio
        )
        deepest = 10 ** (config.audio.ducking.speech_duck_db / 20)

        assert envelope.min() == pytest.approx(deepest, rel=1e-3)

    def test_a_span_past_the_end_is_ignored(self, config) -> None:
        spans = audio_mix.speech_spans([(50.0, 60.0)], config.audio)
        envelope = audio_mix.build_envelope(
            spans, duration_seconds=10.0, config=config.audio
        )

        assert envelope.min() == pytest.approx(1.0)

    def test_no_spans_leaves_the_music_alone(self, config) -> None:
        envelope = audio_mix.build_envelope(
            [], duration_seconds=5.0, config=config.audio
        )

        assert np.all(envelope == 1.0)

    def test_near_spans_merge_so_the_music_does_not_flap(self, config) -> None:
        # Music dipping between two words of one sentence sounds broken.
        merged = audio_mix.merge_spans(
            audio_mix.speech_spans([(1.0, 2.0), (2.1, 3.0)], config.audio)
        )

        assert len(merged) == 1
        assert merged[0].end == pytest.approx(3.0)

    def test_distant_spans_stay_apart(self, config) -> None:
        merged = audio_mix.merge_spans(
            audio_mix.speech_spans([(1.0, 2.0), (30.0, 31.0)], config.audio)
        )

        assert len(merged) == 2

    def test_the_envelope_is_written_as_stereo_at_the_mix_rate(
        self, config, tmp_path: Path
    ) -> None:
        # amultiply requires the layouts to match what it multiplies.
        envelope = audio_mix.build_envelope(
            audio_mix.speech_spans([(1.0, 2.0)], config.audio),
            duration_seconds=3.0,
            config=config.audio,
        )
        path = audio_mix.write_envelope(envelope, tmp_path / "duck.wav")

        with wave.open(str(path)) as handle:
            assert handle.getnchannels() == 2
            assert handle.getframerate() == audio_mix.ENVELOPE_SAMPLE_RATE
            assert handle.getnframes() == len(envelope)


class TestMixGraph:
    def test_every_track_reaches_the_mixer(self, config, tmp_path: Path) -> None:
        plan = audio_mix.plan_mix(
            game=tmp_path / "game.wav",
            microphone=tmp_path / "mic.wav",
            music=[tmp_path / "bed.mp3"],
            music_envelope=tmp_path / "duck.wav",
            game_envelope=tmp_path / "duckgame.wav",
            config=config.audio,
            duration_seconds=60.0,
        )

        assert "amix=inputs=3" in plan.filter_complex
        assert plan.metadata["ducked_music"] is True
        assert plan.metadata["ducked_game"] is True

    def test_the_mixer_does_not_divide_the_level_by_the_track_count(
        self, config, tmp_path: Path
    ) -> None:
        # amix normalises by default, which would drop everything by 9 dB the
        # moment music was added.
        plan = audio_mix.plan_mix(
            game=tmp_path / "game.wav",
            microphone=None,
            music=[],
            music_envelope=None,
            game_envelope=None,
            config=config.audio,
            duration_seconds=60.0,
        )

        assert "normalize=0" in plan.filter_complex

    def test_music_loops_through_an_input_option_not_a_filter(
        self, config, tmp_path: Path
    ) -> None:
        # apad pads with silence; only -stream_loop repeats the bed.
        plan = audio_mix.plan_mix(
            game=tmp_path / "game.wav",
            microphone=None,
            music=[tmp_path / "bed.mp3"],
            music_envelope=None,
            game_envelope=None,
            config=config.audio,
            duration_seconds=600.0,
        )
        arguments = plan.input_arguments()

        assert "-stream_loop" in arguments
        assert arguments.index("-stream_loop") < arguments.index(str(tmp_path / "bed.mp3"))

    def test_music_is_trimmed_to_the_programme(self, config, tmp_path: Path) -> None:
        plan = audio_mix.plan_mix(
            game=tmp_path / "game.wav",
            microphone=None,
            music=[tmp_path / "bed.mp3"],
            music_envelope=None,
            game_envelope=None,
            config=config.audio,
            duration_seconds=123.5,
        )

        assert "atrim=0:123.500" in plan.filter_complex

    def test_the_absence_of_a_microphone_is_reported(self, config, tmp_path: Path) -> None:
        # Every recording this project has seen lacks one; silence about it
        # would be the wrong kind of quiet.
        plan = audio_mix.plan_mix(
            game=tmp_path / "game.wav",
            microphone=None,
            music=[],
            music_envelope=None,
            game_envelope=None,
            config=config.audio,
            duration_seconds=60.0,
        )

        assert any("microphone" in note for note in plan.notes)

    def test_the_loudness_target_is_applied(self, config, tmp_path: Path) -> None:
        plan = audio_mix.plan_mix(
            game=tmp_path / "game.wav",
            microphone=None,
            music=[],
            music_envelope=None,
            game_envelope=None,
            config=config.audio,
            duration_seconds=60.0,
        )

        assert f"loudnorm=I={config.audio.mix.normalization.target_lufs}" in plan.filter_complex
        assert "alimiter" in plan.filter_complex


class TestLocalMusicOnly:
    """§73: nothing is downloaded, ever."""

    def test_only_audio_files_in_the_directory_are_used(self, tmp_path: Path) -> None:
        (tmp_path / "b.mp3").write_bytes(b"")
        (tmp_path / "a.wav").write_bytes(b"")
        (tmp_path / "notes.txt").write_text("not music")

        found = audio_mix.find_music(tmp_path)

        assert [path.name for path in found] == ["a.wav", "b.mp3"]

    def test_a_missing_directory_is_simply_no_music(self, tmp_path: Path) -> None:
        assert audio_mix.find_music(tmp_path / "absent") == []


class TestOverlaySpeedKnobs:
    """The two dials behind an hour-long overlay pass (found on a real video).

    A 9:45 story rendered its caption layer for over an hour: Chromium
    screenshots every frame, and the config used 2 tabs on a 12-thread
    machine at the full 60 fps. Concurrency 0 now sizes to the machine, and
    the overlay renders at its own rate.
    """

    def test_zero_concurrency_sizes_to_the_machine(self, config) -> None:
        import os

        from backend.rendering.remotion import resolved_concurrency

        remotion = config.remotion.model_copy(update={"concurrency": 0})

        assert resolved_concurrency(remotion) == max(2, (os.cpu_count() or 4) - 2)

    def test_an_explicit_concurrency_is_respected(self, config) -> None:
        from backend.rendering.remotion import resolved_concurrency

        remotion = config.remotion.model_copy(update={"concurrency": 3})

        assert resolved_concurrency(remotion) == 3

    def test_the_shipped_config_renders_the_overlay_at_30(self, config) -> None:
        # 30 over 60 composites by timestamp (overlay=shortest=0); half the
        # frames is half the Chromium pass.
        assert config.remotion.overlay_fps == 30
        assert config.remotion.concurrency == 0


class TestAudioJoinFades:
    """Every cut boundary passes through zero (the click-at-the-join defect).

    atrim chops the waveform at an arbitrary sample value; the jump to the
    next clip's first sample is audible as a pop at every join. Found by
    auditing the renderer against a checklist of classic cutting-tool
    defects -- eleven joins in the first passing video, none faded.
    """

    def test_a_span_gets_a_fade_at_each_end(self) -> None:
        from backend.pipeline.workers.render_worker import _audio_span_filter

        chain = _audio_span_filter(0, 10.0, 40.0, out_label="a0")

        assert "afade=t=in:st=0:d=0.030" in chain
        assert "afade=t=out:st=29.970:d=0.030" in chain
        assert chain.startswith("[0:a:0]atrim=start=10.000000:end=40.000000")

    def test_a_tiny_span_gets_a_proportional_fade_not_none(self) -> None:
        from backend.pipeline.workers.render_worker import _audio_span_filter

        chain = _audio_span_filter(2, 5.0, 5.08, out_label="a2")  # 80ms clip

        assert "afade=t=in" in chain
        assert "d=0.020" in chain  # a quarter of the span, not the full 30ms

    def test_the_format_still_lands_after_the_fades(self) -> None:
        # aformat before afade: the fade must act on the mix's sample rate.
        from backend.pipeline.workers.render_worker import _audio_span_filter

        chain = _audio_span_filter(1, 0.0, 10.0, out_label="a1")

        assert chain.index("aformat") < chain.index("afade")


class TestWarpedClipAudio:
    """The audio half of a freeze/ramp: same shape as the picture, as strings.

    Tested as filter-graph strings for the same reason every argv builder in
    this layer is -- and because the one truth that matters (the durations
    coming out of FFmpeg) has its own integration test with a real decode.
    """

    def _clip(self, *, end: float, meta: dict):
        from backend.timeline.models import TimelineClip

        return TimelineClip(
            id="clip-00warpaudio",
            media_id="media-aaaaaaaaaaaa",
            clip_index=0,
            source_in=10.0,
            source_out=16.0,
            timeline_start=0.0,
            timeline_end=end,
            metadata={"retime": meta},
        )

    def test_atempo_below_a_half_is_chained(self) -> None:
        # atempo refuses factors under 0.5, and the doctrine's slowdown is 0.4.
        assert audio_mix.atempo_steps(0.4) == "atempo=0.500000,atempo=0.800000"
        assert audio_mix.atempo_steps(0.25) == "atempo=0.500000,atempo=0.500000"
        assert audio_mix.atempo_steps(0.5) == "atempo=0.500000"
        assert audio_mix.atempo_steps(1.0) == "atempo=1.000000"
        with pytest.raises(ValueError, match="positive"):
            audio_mix.atempo_steps(0.0)

    def test_a_freeze_pads_silence_at_the_anchor(self) -> None:
        from backend.timeline import retime

        clip = self._clip(
            end=7.5, meta={"effect": "freeze_frame", "at": 2.5, "extra_seconds": 1.5}
        )
        graph = audio_mix.warped_clip_audio(
            0, clip, retime.clip_retime(clip), out_label="a0", fade_in=0.03, fade_out=0.03
        )

        assert "atrim=start=10.000000:end=16.000000" in graph, "the source span"
        assert "apad=pad_dur=1.500000" in graph, "the hold is silence, not looped sound"
        assert "atrim=end=2.500000" in graph, "split at the clip-relative anchor"
        assert "concat=n=2:v=0:a=1" in graph
        assert "afade=t=out:st=7.470:d=0.030" in graph, "the edge fade on the output clock"
        assert graph.endswith("[a0]")

    def test_a_ramp_is_pinned_to_its_arithmetic_length(self) -> None:
        # atempo's WSOLA measured 30 ms short on a 2 s stretch, and the concat
        # assembly hands that drift to every later clip; the slow piece is
        # padded up and trimmed down to exactly window + extra.
        from backend.timeline import retime

        clip = self._clip(
            end=7.2,
            meta={
                "effect": "speed_ramp",
                "at": 2.0,
                "extra_seconds": 1.2,
                "window_seconds": 0.8,
                "factor": 0.4,
            },
        )
        graph = audio_mix.warped_clip_audio(
            0, clip, retime.clip_retime(clip), out_label="a0", fade_in=0.03, fade_out=0.03
        )

        assert "atempo=0.500000,atempo=0.800000" in graph
        assert "apad=whole_dur=2.000000,atrim=end=2.000000" in graph
        assert "concat=n=3:v=0:a=1" in graph
        assert "areverse" in graph, "the true tail is faded, wherever atempo left it"

    def test_the_jl_delay_rides_the_same_chain(self) -> None:
        from backend.timeline import retime

        clip = self._clip(
            end=7.5, meta={"effect": "freeze_frame", "at": 2.5, "extra_seconds": 1.5}
        )
        graph = audio_mix.warped_clip_audio(
            2,
            clip,
            retime.clip_retime(clip),
            out_label="c2",
            fade_in=0.03,
            fade_out=0.03,
            delay_ms=8000,
        )

        assert graph.endswith("adelay=8000|8000[c2]")
        assert "[2:a:0]" in graph, "the input index names the caller's -i position"


class TestSoundEffects:
    """sound_effect has had triggers and budgets since Phase 1; nothing read
    the rows. The planner rationed an effect and then discarded it."""

    def _plan(self, tmp_path, stingers, duration=60.0):
        from backend.config.loader import load_config
        from backend.rendering import audio_mix

        game = tmp_path / "game.wav"
        game.write_bytes(b"")
        return audio_mix.plan_mix(
            game=game,
            microphone=None,
            music=(),
            music_envelope=None,
            game_envelope=None,
            config=load_config().audio,
            duration_seconds=duration,
            stingers=stingers,
        )

    def test_a_stinger_is_delayed_to_its_moment(self, tmp_path) -> None:
        from backend.rendering.audio_mix import Stinger

        asset = tmp_path / "boom.wav"
        asset.write_bytes(b"")
        plan = self._plan(tmp_path, [Stinger(path=asset, at_seconds=12.5)])

        assert "adelay=12500|12500" in plan.filter_complex
        assert "sfx0" in plan.filter_complex

    def test_a_stinger_is_padded_so_the_mix_is_not_truncated(self, tmp_path) -> None:
        # amix ends with its shortest input unless every stream reaches the end.
        from backend.rendering.audio_mix import Stinger

        asset = tmp_path / "boom.wav"
        asset.write_bytes(b"")
        plan = self._plan(tmp_path, [Stinger(path=asset, at_seconds=5.0)], duration=90.0)

        assert "apad=whole_dur=90.000" in plan.filter_complex

    def test_a_missing_asset_is_skipped_and_reported(self, tmp_path) -> None:
        # §73 is about consent: substituting a different sound is the wrong
        # way to be helpful.
        from backend.rendering.audio_mix import Stinger

        plan = self._plan(tmp_path, [Stinger(path=tmp_path / "nope.wav", at_seconds=1.0)])

        assert "sfx0" not in plan.filter_complex
        assert any("missing" in note for note in plan.notes)

    def test_no_stingers_leaves_the_graph_untouched(self, tmp_path) -> None:
        plan = self._plan(tmp_path, [])

        assert "sfx" not in plan.filter_complex


class _SynthFFmpeg:
    """Just enough runner: synthesis "succeeds" by touching the destination."""

    def base_arguments(self):
        return ["ffmpeg"]

    def run(self, argv, timeout_seconds=None, **_):
        Path(argv[-1]).write_bytes(b"RIFFfake")


class TestSynthesisedResolver:
    """resolve_stinger_asset: the project's own file wins; recipes answer next."""

    def test_a_project_file_wins_over_the_recipe(self, tmp_path: Path) -> None:
        from backend.rendering import sfx

        project = tmp_path / "assets"
        project.mkdir()
        theirs = project / "hit.wav"
        theirs.write_bytes(b"their sound")

        found = sfx.resolve_stinger_asset("hit.wav", project, tmp_path / "data", _SynthFFmpeg())

        assert found == theirs
        # Nothing was synthesised: their file answered outright.
        assert not (tmp_path / "data" / "assets").exists()

    def test_a_recipe_name_synthesises_on_first_use(self, tmp_path: Path) -> None:
        from backend.rendering import sfx

        found = sfx.resolve_stinger_asset(
            "impact.wav", tmp_path / "assets", tmp_path / "data", _SynthFFmpeg()
        )

        assert found is not None and found.is_file()
        assert found == sfx.sfx_dir(tmp_path / "data") / "impact.wav"

    def test_an_unknown_name_is_honestly_missing(self, tmp_path: Path) -> None:
        from backend.rendering import sfx

        assert (
            sfx.resolve_stinger_asset(
                "airhorn.wav", tmp_path / "assets", tmp_path / "data", _SynthFFmpeg()
            )
            is None
        )


class TestMusicShelves:
    """§15: the edit's own mean intensity picks the shelf, when shelves exist."""

    def _shelved(self, tmp_path: Path) -> Path:
        for shelf in ("low", "build", "peak"):
            (tmp_path / shelf).mkdir()
            (tmp_path / shelf / f"{shelf}.mp3").write_bytes(b"")
        return tmp_path

    def test_a_calm_edit_gets_the_calm_bed(self, tmp_path: Path) -> None:
        found = audio_mix.find_music(self._shelved(tmp_path), mean_intensity=0.3)
        assert [path.name for path in found] == ["low.mp3"]

    def test_a_hot_edit_gets_the_peak_shelf(self, tmp_path: Path) -> None:
        found = audio_mix.find_music(self._shelved(tmp_path), mean_intensity=0.7)
        assert [path.name for path in found] == ["peak.mp3"]

    def test_the_middle_is_the_build(self, tmp_path: Path) -> None:
        found = audio_mix.find_music(self._shelved(tmp_path), mean_intensity=0.5)
        assert [path.name for path in found] == ["build.mp3"]

    def test_a_missing_shelf_falls_back_to_whatever_exists(self, tmp_path: Path) -> None:
        (tmp_path / "low").mkdir()
        (tmp_path / "low" / "quiet.mp3").write_bytes(b"")

        found = audio_mix.find_music(tmp_path, mean_intensity=0.9)

        assert [path.name for path in found] == ["quiet.mp3"]

    def test_a_flat_directory_behaves_exactly_as_before(self, tmp_path: Path) -> None:
        (tmp_path / "song.mp3").write_bytes(b"")

        assert [p.name for p in audio_mix.find_music(tmp_path, mean_intensity=0.9)] == [
            "song.mp3"
        ]
        assert [p.name for p in audio_mix.find_music(tmp_path)] == ["song.mp3"]
