"""Typed configuration schema (SPEC section 91).

Every model uses ``extra="forbid"``: a typo in a YAML key is a startup error,
not a silently ignored setting. Section 81's "no silent failure" applies to
configuration as much as to pipeline stages.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.duration import SECONDS_PER_MINUTE, DurationPolicy
from backend.core.models.enums import (
    EffectCategory,
    EffectEngine,
    EffectType,
    HardwareProfile,
    MomentType,
    PublishTarget,
    PublishVisibility,
    RenderEngine,
    VideoMode,
)

Device = Literal["auto", "cuda", "cpu"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# application.yaml
# ---------------------------------------------------------------------------


class DirectoriesConfig(_Section):
    projects: str = "projects"
    models: str = "models"
    logs: str = "logs"
    cache: str = ".cache"
    profiles: str = "profiles"

    #: Drop a recording in ``input`` and the finished video appears in
    #: ``output``. Neither is where the pipeline works -- a project still owns
    #: its own tree under ``projects`` (§43) -- they are the two ends a person
    #: touches, kept apart from the machinery in between.
    input: str = "input"
    output: str = "output"


class DatabaseConfig(_Section):
    filename: str = "gaming_editor.db"
    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"] = "WAL"
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "NORMAL"
    busy_timeout_ms: int = Field(default=5000, ge=0)
    foreign_keys: bool = True


class ApiConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=list)


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    console: bool = True
    # Named json_format rather than json: a field called `json` shadows a
    # BaseModel attribute and Pydantic warns about it at class creation.
    json_format: bool = True
    directory: str = "logs"
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    backup_count: int = Field(default=5, ge=0)


class StorageConfig(_Section):
    """Disk management for large intermediates (§84)."""

    max_cache_gb: float = Field(default=50.0, gt=0)
    keep_proxy_after_render: bool = True
    keep_frames_after_analysis: bool = False
    warn_free_space_gb: float = Field(default=20.0, ge=0)


class ApplicationConfig(_Section):
    name: str = "AI Gaming Video Editor"
    environment: Literal["development", "test", "production"] = "development"
    data_root: str | None = None
    directories: DirectoriesConfig = Field(default_factory=DirectoriesConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


# ---------------------------------------------------------------------------
# output.yaml
# ---------------------------------------------------------------------------


class OutputConfig(_Section):
    """Duration policy and video modes (§6, §35).

    ``min_minutes``/``max_minutes`` may narrow the 10-60 minute product band but
    never widen it; :meth:`duration_policy` enforces that.
    """

    min_minutes: float = Field(gt=0)
    max_minutes: float = Field(gt=0)
    duration_presets_minutes: list[int] = Field(min_length=1)
    default_duration_minutes: float = Field(gt=0)
    tolerance_seconds: float = Field(default=45.0, ge=0)
    tolerance_ratio: float = Field(default=0.05, ge=0, le=0.5)
    modes: list[VideoMode] = Field(min_length=1)
    default_mode: VideoMode = VideoMode.STORY

    @model_validator(mode="after")
    def _validate_band(self) -> OutputConfig:
        if self.default_mode not in self.modes:
            raise ValueError("output.default_mode must be listed in output.modes.")
        # Building the policy applies every band rule, including the absolute
        # product limits, so an invalid configuration fails at load time.
        self.duration_policy()
        return self

    def duration_policy(self) -> DurationPolicy:
        """Return the :class:`DurationPolicy` this configuration describes."""
        return DurationPolicy.from_minutes(
            min_minutes=self.min_minutes,
            max_minutes=self.max_minutes,
            presets_minutes=self.duration_presets_minutes,
            default_minutes=self.default_duration_minutes,
            tolerance_seconds=self.tolerance_seconds,
            tolerance_ratio=self.tolerance_ratio,
        )

    @property
    def min_seconds(self) -> int:
        return round(self.min_minutes * SECONDS_PER_MINUTE)

    @property
    def max_seconds(self) -> int:
        return round(self.max_minutes * SECONDS_PER_MINUTE)


# ---------------------------------------------------------------------------
# analysis.yaml
# ---------------------------------------------------------------------------


class SamplingTriggers(_Section):
    audio_spike: bool = True
    scene_change: bool = True
    hud_change: bool = True
    motion_spike: bool = True
    speech_activity: bool = True


class FrameSamplingConfig(_Section):
    base_interval_seconds: float = Field(default=3.0, gt=0)
    high_activity_interval_seconds: float = Field(default=1.0, gt=0)
    candidate_interval_seconds: float = Field(default=0.5, gt=0)
    candidate_pre_roll_seconds: float = Field(default=20.0, ge=0)
    candidate_post_roll_seconds: float = Field(default=20.0, ge=0)
    max_frames_per_hour: int = Field(default=1800, ge=1)
    max_frames_per_candidate: int = Field(default=40, ge=1)
    jpeg_quality: int = Field(default=4, ge=1, le=31)
    triggers: SamplingTriggers = Field(default_factory=SamplingTriggers)

    @model_validator(mode="after")
    def _hierarchical(self) -> FrameSamplingConfig:
        if not (
            self.base_interval_seconds
            > self.high_activity_interval_seconds
            >= self.candidate_interval_seconds
        ):
            raise ValueError(
                "Frame sampling must get denser, not sparser (§16): base > high_activity "
                ">= candidate interval."
            )
        return self


class SceneAnalysisConfig(_Section):
    detector: Literal["content", "threshold", "adaptive"] = "content"
    threshold: float = Field(default=27.0, gt=0)
    min_scene_seconds: float = Field(default=1.0, gt=0)
    downscale_factor: int = Field(default=2, ge=1)


class AudioDetectFlags(_Section):
    rms: bool = True
    peak: bool = True
    lufs: bool = True
    silence: bool = True
    transients: bool = True
    spectral_activity: bool = True


class AudioAnalysisConfig(_Section):
    window_seconds: float = Field(default=0.5, gt=0)
    hop_seconds: float = Field(default=0.25, gt=0)
    silence_threshold_db: float = -45.0
    min_silence_seconds: float = Field(default=1.5, ge=0)
    spike_threshold_db: float = Field(default=9.0, gt=0)
    loudness_target_lufs: float = -14.0
    detect: AudioDetectFlags = Field(default_factory=AudioDetectFlags)

    @model_validator(mode="after")
    def _hop_within_window(self) -> AudioAnalysisConfig:
        if self.hop_seconds > self.window_seconds:
            raise ValueError("analysis.audio.hop_seconds must not exceed window_seconds.")
        return self


class MicrophoneConfig(_Section):
    enabled: bool = True
    auto_detect_track: bool = True
    reaction_window_seconds: float = Field(default=4.0, gt=0)


class ReactionsConfig(_Section):
    enabled: bool = True
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    correlation_window_seconds: float = Field(default=6.0, gt=0)


class VisionAnalysisConfig(_Section):
    """Cascading vision analysis.

    A local VLM costs seconds per frame. Sending every sampled frame of a
    two-hour recording would take hours, so cheap detectors nominate candidate
    regions first and only their keyframes reach the model (§15, §16).
    """

    enabled: bool = True
    only_candidate_regions: bool = True
    max_frames_per_request: int = Field(default=4, ge=1)
    max_frames_per_source_hour: int = Field(default=240, ge=1)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    describe_hud: bool = True
    describe_scene: bool = True
    candidate_detectors: list[str] = Field(default_factory=list)
    frame_difference_threshold: float = Field(default=0.35, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _detectors_required_for_cascade(self) -> VisionAnalysisConfig:
        if self.enabled and self.only_candidate_regions and not self.candidate_detectors:
            raise ValueError(
                "analysis.vision.only_candidate_regions is true but no "
                "candidate_detectors are configured: nothing would nominate regions "
                "and the vision stage would analyse nothing."
            )
        return self


class OcrConfig(_Section):
    """Region-restricted OCR (§25).

    Reading only the HUD boxes a game profile declares is both cheaper and more
    accurate than scanning a whole frame of stylised game UI.
    """

    enabled: bool = True
    engine: Literal["paddleocr", "easyocr", "tesseract", "auto"] = "paddleocr"
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    use_profile_regions: bool = True
    full_frame_fallback: bool = True
    full_frame_max_width: int = Field(default=960, ge=160)


class HudConfig(_Section):
    enabled: bool = True
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    fields: list[str] = Field(default_factory=list)


class NarrationConfig(_Section):
    """Reading the transcript for complete incidents (§19, §21).

    The player narrating is the only source that already contains the story in
    words. Without this the pipeline had a vocabulary of two moment types for a
    recording with eleven minutes of speech in it.
    """

    enabled: bool = True
    #: Transcript is read in windows: a model asked to segment forty minutes at
    #: once returns fewer and vaguer incidents than one asked about six.
    window_seconds: float = Field(default=360.0, gt=0)
    #: Consecutive windows overlap so an incident on a boundary is seen whole.
    overlap_seconds: float = Field(default=45.0, ge=0)
    #: Below this an incident is not worth a clip.
    min_importance: float = Field(default=0.35, ge=0.0, le=1.0)
    #: Longer than this is the model failing to find a seam, not a long
    #: situation.
    max_incident_seconds: float = Field(default=150.0, gt=0)

    @model_validator(mode="after")
    def _overlap_below_window(self) -> NarrationConfig:
        if self.overlap_seconds >= self.window_seconds:
            raise ValueError(
                "analysis.narration.overlap_seconds must be smaller than "
                "window_seconds, otherwise the windows make no progress."
            )
        return self


class AnalysisConfig(_Section):
    """Chunked analysis so an 8-hour recording never enters RAM whole (§7)."""

    chunk_seconds: float = Field(default=600.0, gt=0)
    chunk_overlap_seconds: float = Field(default=15.0, ge=0)
    event_overlap_seconds: float = Field(default=5.0, ge=0)
    frame_sampling: FrameSamplingConfig = Field(default_factory=FrameSamplingConfig)
    scenes: SceneAnalysisConfig = Field(default_factory=SceneAnalysisConfig)
    audio: AudioAnalysisConfig = Field(default_factory=AudioAnalysisConfig)
    microphone: MicrophoneConfig = Field(default_factory=MicrophoneConfig)
    reactions: ReactionsConfig = Field(default_factory=ReactionsConfig)
    vision: VisionAnalysisConfig = Field(default_factory=VisionAnalysisConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    hud: HudConfig = Field(default_factory=HudConfig)
    narration: NarrationConfig = Field(default_factory=NarrationConfig)

    @model_validator(mode="after")
    def _overlap_below_chunk(self) -> AnalysisConfig:
        if self.chunk_overlap_seconds >= self.chunk_seconds:
            raise ValueError(
                "analysis.chunk_overlap_seconds must be smaller than chunk_seconds, "
                "otherwise chunking makes no progress."
            )
        return self


# ---------------------------------------------------------------------------
# models.yaml
# ---------------------------------------------------------------------------


class _ModelBase(_Section):
    provider: str
    model: str
    version: str = Field(description="Stored with every analysis result (§49).")
    timeout_seconds: int = Field(default=300, ge=1)
    estimated_vram_mb: int = Field(default=0, ge=0)


class SpeechModelConfig(_ModelBase):
    device: Device = "auto"
    compute_type: Literal["float16", "float32", "int8", "int8_float16"] = "float16"
    #: >1 routes through CTranslate2's BatchedInferencePipeline, which
    #: transcribes the VAD-split segments of a long file in parallel batches.
    #: This field existed decoratively for months before anything read it.
    batch_size: int = Field(default=8, ge=1)
    #: 5 is quality; 1 is greedy decoding, roughly 2x faster with a small
    #: accuracy cost. Captions are user-visible (§71), so quality by default.
    beam_size: int = Field(default=5, ge=1)
    #: Off: a gameplay track is music and gunfire between callouts, and
    #: conditioning on previous text is how Whisper repeats one hallucinated
    #: sentence for minutes.
    condition_on_previous_text: bool = False
    #: Silero VAD tuning. The pad keeps word onsets that a hard gate clips.
    vad_min_silence_ms: int = Field(default=500, ge=0)
    vad_speech_pad_ms: int = Field(default=200, ge=0)
    #: Feeding threads for CTranslate2; they prepare audio while the GPU works.
    cpu_threads: int = Field(default=4, ge=0)
    num_workers: int = Field(default=2, ge=1)
    language: str | None = None
    word_timestamps: bool = True
    vad_filter: bool = True


class VisionModelConfig(_ModelBase):
    quantization: str | None = None
    endpoint: str = "http://127.0.0.1:11434"
    max_output_tokens: int = Field(default=1024, ge=64)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)


class LLMModelConfig(_ModelBase):
    quantization: str | None = None
    endpoint: str = "http://127.0.0.1:11434"
    context_tokens: int = Field(default=32768, ge=1024)
    max_output_tokens: int = Field(default=4096, ge=64)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


class OcrModelConfig(_ModelBase):
    device: Device = "cpu"


class ModelsConfig(_Section):
    speech: SpeechModelConfig
    vision: VisionModelConfig
    llm: LLMModelConfig
    ocr: OcrModelConfig


class CloudConfig(_Section):
    """Optional cloud provider (§13). Off by default; nothing uploads (§51)."""

    enabled: bool = False
    provider: str | None = None

    @model_validator(mode="after")
    def _provider_required_when_enabled(self) -> CloudConfig:
        if self.enabled and not self.provider:
            raise ValueError("cloud.enabled is true but cloud.provider is not set.")
        return self


class GpuConfig(_Section):
    """VRAM budget and the sequential model policy (§52, §54).

    On an 8 GB card Whisper large-v3, a 7B VLM and an NVENC session cannot
    coexist. Stages run one at a time and each releases its VRAM -- including
    the allocator's cached blocks -- before the next model loads.
    """

    enabled: bool = True
    device_index: int = Field(default=0, ge=0)
    total_vram_mb: int = Field(default=8192, ge=0)
    reserved_vram_mb: int = Field(default=1024, ge=0)
    exclusive_model_slot: bool = True
    empty_cache_between_stages: bool = True
    preflight_vram_check: bool = True
    fallback_to_cpu: bool = True

    @model_validator(mode="after")
    def _reserved_fits(self) -> GpuConfig:
        if self.reserved_vram_mb > self.total_vram_mb:
            raise ValueError("gpu.reserved_vram_mb cannot exceed gpu.total_vram_mb.")
        return self

    @property
    def usable_vram_mb(self) -> int:
        return max(self.total_vram_mb - self.reserved_vram_mb, 0)

    def fits(self, estimated_vram_mb: int) -> bool:
        """Whether a model of this size may be loaded on the GPU."""
        return estimated_vram_mb <= self.usable_vram_mb


class HardwareProfileConfig(_Section):
    description: str = ""
    min_vram_mb: int = Field(default=0, ge=0)
    vision_enabled: bool = True
    speech_compute_type: Literal["float16", "float32", "int8", "int8_float16"] = "float16"
    frame_sampling_multiplier: float = Field(default=1.0, gt=0)
    max_parallel_stages: int = Field(default=1, ge=1)


class HardwareConfig(_Section):
    """Hardware strategy (§53). ``auto`` picks a profile from detected VRAM."""

    profile: Literal["auto", "low", "medium", "high"] = "auto"
    profiles: dict[HardwareProfile, HardwareProfileConfig]

    @model_validator(mode="after")
    def _all_profiles_present(self) -> HardwareConfig:
        missing = sorted(
            profile.value for profile in HardwareProfile if profile not in self.profiles
        )
        if missing:
            raise ValueError(
                "hardware.profiles is missing: " + ", ".join(missing) + " (§53)."
            )
        return self

    def select(self, available_vram_mb: int | None) -> HardwareProfile:
        """Choose a profile for the detected hardware.

        A pinned profile wins. Otherwise the highest profile whose
        ``min_vram_mb`` the machine satisfies is used; with no GPU that is
        always ``LOW``, which disables the vision model (§53).
        """
        if self.profile != "auto":
            return HardwareProfile(self.profile)
        vram = available_vram_mb or 0
        best = HardwareProfile.LOW
        for profile in (HardwareProfile.LOW, HardwareProfile.MEDIUM, HardwareProfile.HIGH):
            if vram >= self.profiles[profile].min_vram_mb:
                best = profile
        return best


class FallbacksConfig(_Section):
    """Graceful degradation chains (§95)."""

    vision_failed: list[str] = Field(default_factory=list)
    speech_failed: list[str] = Field(default_factory=list)
    llm_failed: list[str] = Field(default_factory=list)
    ocr_failed: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# moments.yaml
# ---------------------------------------------------------------------------


class CorrelationConfig(_Section):
    window_seconds: float = Field(default=3.0, gt=0)
    min_sources: int = Field(default=1, ge=1)
    multi_source_bonus: float = Field(default=0.12, ge=0.0, le=1.0)
    max_confidence: float = Field(default=0.99, gt=0.0, le=1.0)


class FormationConfig(_Section):
    max_event_gap_seconds: float = Field(default=12.0, gt=0)
    min_moment_seconds: float = Field(default=4.0, gt=0)
    max_moment_seconds: float = Field(default=180.0, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> FormationConfig:
        if self.min_moment_seconds >= self.max_moment_seconds:
            raise ValueError("moments.formation.min_moment_seconds must be below max.")
        return self


class ContextExpansionConfig(_Section):
    """Adaptive pre/post roll (§29), not a fixed +/- 20 seconds."""

    base_pre_roll_seconds: float = Field(default=8.0, ge=0)
    base_post_roll_seconds: float = Field(default=6.0, ge=0)
    max_pre_roll_seconds: float = Field(default=25.0, ge=0)
    max_post_roll_seconds: float = Field(default=20.0, ge=0)
    snap_to_scene_boundary: bool = True
    snap_window_seconds: float = Field(default=2.5, ge=0)
    extend_to_speech_boundary: bool = True

    @model_validator(mode="after")
    def _base_below_max(self) -> ContextExpansionConfig:
        if self.base_pre_roll_seconds > self.max_pre_roll_seconds:
            raise ValueError("moments.context base_pre_roll exceeds max_pre_roll.")
        if self.base_post_roll_seconds > self.max_post_roll_seconds:
            raise ValueError("moments.context base_post_roll exceeds max_post_roll.")
        return self


class MomentScoringWeights(_Section):
    """The ten positive dimensions of §32."""

    gameplay: float = Field(ge=0)
    visual: float = Field(ge=0)
    audio: float = Field(ge=0)
    reaction: float = Field(ge=0)
    novelty: float = Field(ge=0)
    skill: float = Field(ge=0)
    emotion: float = Field(ge=0)
    narrative: float = Field(ge=0)
    context: float = Field(ge=0)
    entertainment: float = Field(ge=0)

    @model_validator(mode="after")
    def _not_all_zero(self) -> MomentScoringWeights:
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("moments.scoring.weights must not all be zero.")
        return self

    def total(self) -> float:
        return float(sum(self.model_dump().values()))

    def normalised(self) -> dict[str, float]:
        """Weights scaled to sum to 1.0 so scores stay comparable."""
        total = self.total()
        return {key: value / total for key, value in self.model_dump().items()}


class MomentScoringPenalties(_Section):
    """Subtracted, not added (§32)."""

    dead_time: float = Field(ge=0)
    repetition: float = Field(ge=0)
    low_confidence: float = Field(ge=0)


class MomentScoringConfig(_Section):
    weights: MomentScoringWeights
    penalties: MomentScoringPenalties
    minimum_score: float = Field(default=0.20, ge=0.0, le=1.0)
    needs_review_confidence: float = Field(default=0.60, ge=0.0, le=1.0)


class DeadTimeConfig(_Section):
    enabled: bool = True
    categories: list[str] = Field(default_factory=list)
    min_segment_seconds: float = Field(default=6.0, gt=0)
    silence_seconds_for_dead_time: float = Field(default=8.0, gt=0)
    afk_motion_threshold: float = Field(default=0.02, ge=0.0, le=1.0)
    protect_context: bool = True
    max_protected_seconds: float = Field(default=20.0, ge=0)


class RepetitionConfig(_Section):
    enabled: bool = True
    similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    max_repeats_kept: int = Field(default=2, ge=1)
    keep_strategy: Literal["highest_score", "first", "last", "longest"] = "highest_score"
    compare: list[str] = Field(default_factory=list)


class VarietyConfig(_Section):
    """§33: the highest scores alone make a monotonous video."""

    enabled: bool = True
    max_same_type_ratio: float = Field(default=0.4, gt=0.0, le=1.0)
    min_distinct_types: int = Field(default=3, ge=1)
    saturation_penalty: float = Field(default=0.25, ge=0.0, le=1.0)


class MomentsConfig(_Section):
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    formation: FormationConfig = Field(default_factory=FormationConfig)
    context: ContextExpansionConfig = Field(default_factory=ContextExpansionConfig)
    scoring: MomentScoringConfig
    dead_time: DeadTimeConfig = Field(default_factory=DeadTimeConfig)
    repetition: RepetitionConfig = Field(default_factory=RepetitionConfig)
    variety: VarietyConfig = Field(default_factory=VarietyConfig)


# ---------------------------------------------------------------------------
# narrative.yaml
# ---------------------------------------------------------------------------


class StoryConfig(_Section):
    structure: list[str] = Field(min_length=1)
    coherence_weight: float = Field(default=0.55, ge=0.0, le=1.0)
    score_weight: float = Field(default=0.45, ge=0.0, le=1.0)
    require_climax: bool = True


class BestMomentsConfig(_Section):
    ordering: Literal["descending_score", "chronological"] = "descending_score"
    interleave_types: bool = True


class CompilationConfig(_Section):
    group_by: Literal["moment_type", "game_event", "chronological"] = "moment_type"
    group_order: list[MomentType] = Field(default_factory=list)
    separator_seconds: float = Field(default=0.5, ge=0)


class HookConfig(_Section):
    """§37. The hook is an existing moment; narration is never invented."""

    enabled: bool = True
    max_seconds: float = Field(default=25.0, gt=0)
    min_seconds: float = Field(default=5.0, gt=0)
    sources: list[str] = Field(default_factory=list)
    allow_replay_in_body: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> HookConfig:
        if self.min_seconds > self.max_seconds:
            raise ValueError("narrative.hook.min_seconds must not exceed max_seconds.")
        return self


class PacingConfig(_Section):
    min_clip_seconds: float = Field(default=2.0, gt=0)
    max_clip_seconds: float = Field(default=120.0, gt=0)
    target_clips_per_minute: float = Field(default=2.2, gt=0)
    max_consecutive_same_type: int = Field(default=2, ge=1)
    max_consecutive_low_intensity_seconds: float = Field(default=45.0, gt=0)
    intensity_curve: Literal["rising", "flat", "wave"] = "rising"

    @model_validator(mode="after")
    def _ordered(self) -> PacingConfig:
        if self.min_clip_seconds >= self.max_clip_seconds:
            raise ValueError("narrative.pacing.min_clip_seconds must be below max.")
        return self


class OptimizerObjectiveWeights(_Section):
    entertainment: float = Field(ge=0)
    narrative: float = Field(ge=0)
    variety: float = Field(ge=0)


class OptimizerPenalties(_Section):
    repetition: float = Field(ge=0)
    dead_time: float = Field(ge=0)


class DurationOptimizerConfig(_Section):
    """§39: constrained optimisation, not a sort."""

    strategy: Literal["weighted_knapsack", "greedy"] = "weighted_knapsack"
    objective_weights: OptimizerObjectiveWeights
    objective_penalties: OptimizerPenalties
    max_iterations: int = Field(default=500, ge=1)
    allow_context_trim: bool = True
    max_context_trim_ratio: float = Field(default=0.5, ge=0.0, le=1.0)


class RefinementConfig(_Section):
    """The last pass over the final cut points (backend/narrative/refinement.py).

    Exists because §29 snaps boundaries and §39's duration trim then re-cuts
    them blind: 8 of 26 cut points on a finished video landed mid-sentence.
    """

    enabled: bool = True
    #: How far a mid-speech cut may move to reach a pause. Small on purpose --
    #: a cut that wanders four seconds has changed the clip, not refined it.
    snap_window_seconds: float = Field(default=2.5, gt=0)
    #: The shortest inter-word gap that counts as a pause worth cutting in.
    min_pause_seconds: float = Field(default=0.35, gt=0)
    #: Where inside a pause the cut lands, as a fraction of the gap after the
    #: outgoing words. 0.1 follows Leake et al. (SIGGRAPH 2017): ~90% of a
    #: retained gap belongs before the incoming line, so speech resumes almost
    #: as soon as the new shot lands. 0.5 is the old midpoint behaviour.
    pause_split: float = Field(default=0.1, ge=0.0, le=1.0)
    #: The tail a cut always keeps after the last word. The floor cannot go
    #: below ``WORD_PAD_SECONDS + 0.05`` (0.2) -- the pad is what already
    #: covers Whisper's clipped trailing S/P sounds, and a cut inside it would
    #: count as mid-speech by the module's own test. Values below 0.2 are
    #: clamped up to it; the default states the effective floor honestly.
    min_trailing_seconds: float = Field(default=0.2, ge=0.0)
    #: How far a cut may retreat *into* the core span to reach a pause. §28's
    #: core is content and stays protected in substance -- but its edges come
    #: from event detectors with seconds of slop, and when the optimiser trims
    #: all context the boundary IS the core edge, pinned mid-word by a rule
    #: meant to protect a kill, not a syllable.
    core_give_seconds: float = Field(default=1.0, ge=0.0)
    #: Reach in the direction that only adds context -- earlier for a start,
    #: later for an end. Wider than the window because extending an end to let
    #: a sentence finish is the classic edit, and it cannot lose content.
    stretch_window_seconds: float = Field(default=4.0, gt=0)


class TransitionsConfig(_Section):
    """Film grammar at the joins (§40): a hard cut says "continuous time".

    Every cut in the edit was hard, and a chronological gaming edit jumps
    minutes of session time at nearly every join -- so each join *claimed*
    continuity it did not have, which is one reason the result read as "clips
    from everywhere". The dip to black is the standard signal for time passing;
    kept short enough (well under blackdetect's 0.5s floor) that QA's black
    check never mistakes it for a broken render.
    """

    enabled: bool = True
    #: A same-recording jump at least this long reads as "later", not "next".
    time_jump_seconds: float = Field(default=45.0, gt=0)
    #: The dip itself, each side of the join.
    dip_seconds: float = Field(default=0.3, gt=0, le=0.45)


class NarrativeConfig(_Section):
    story: StoryConfig
    best_moments: BestMomentsConfig = Field(default_factory=BestMomentsConfig)
    compilation: CompilationConfig = Field(default_factory=CompilationConfig)
    hook: HookConfig = Field(default_factory=HookConfig)
    pacing: PacingConfig = Field(default_factory=PacingConfig)
    optimizer: DurationOptimizerConfig
    refinement: RefinementConfig = Field(default_factory=RefinementConfig)
    transitions: TransitionsConfig = Field(default_factory=TransitionsConfig)


# ---------------------------------------------------------------------------
# rendering.yaml
# ---------------------------------------------------------------------------


class VideoConfig(_Section):
    default_resolution: int = Field(default=1080, ge=360)
    default_fps: int = Field(default=60, ge=1)
    aspect_ratio: str = "16:9"
    fps_mode: Literal["explicit", "source"] = "explicit"
    supported_resolutions: list[int] = Field(min_length=1)
    supported_fps: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def _defaults_supported(self) -> VideoConfig:
        if self.default_resolution not in self.supported_resolutions:
            raise ValueError("video.default_resolution is not in video.supported_resolutions.")
        if self.default_fps not in self.supported_fps:
            raise ValueError("video.default_fps is not in video.supported_fps.")
        return self


class NvencConfig(_Section):
    preset: str = "p5"
    tune: str = "hq"
    rate_control: Literal["vbr", "cbr", "constqp"] = "vbr"
    rc_lookahead: int = Field(default=20, ge=0)


class X264Config(_Section):
    preset: str = "medium"
    crf: int = Field(default=19, ge=0, le=51)


class RenderConfig(_Section):
    container: Literal["mp4", "mkv", "mov"] = "mp4"
    video_codec: Literal["h264", "hevc"] = "h264"
    audio_codec: Literal["aac", "opus", "flac"] = "aac"
    pixel_format: str = "yuv420p"
    faststart: bool = True
    gop_seconds: float = Field(default=2.0, gt=0)
    encoder_preference: list[str] = Field(min_length=1)
    bitrate: dict[str, str]
    max_bitrate_multiplier: float = Field(default=1.35, ge=1.0)
    nvenc: NvencConfig = Field(default_factory=NvencConfig)
    libx264: X264Config = Field(default_factory=X264Config)

    def bitrate_for(self, resolution: int, fps: int) -> str | None:
        """Return the configured bitrate for a resolution/fps pair."""
        return self.bitrate.get(f"{resolution}p{fps}")


class YoutubePresetConfig(_Section):
    name: str = "YouTube 1080p60"
    resolution: int = Field(default=1080, ge=360)
    fps: int = Field(default=60, ge=1)
    video_codec: str = "h264"
    audio_codec: str = "aac"
    audio_bitrate: str = "384k"
    audio_sample_rate: int = Field(default=48000, ge=8000)
    audio_channels: int = Field(default=2, ge=1, le=8)


class FfmpegConfig(_Section):
    binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    threads: int = Field(default=0, ge=0)
    loglevel: str = "error"
    overwrite_output: bool = True
    timeout_seconds: int = Field(default=28800, ge=1)
    hwaccel: str = "auto"


class ProxyConfig(_Section):
    enabled: bool = True
    resolution: int = Field(default=720, ge=180)
    fps: int = Field(default=30, ge=1)
    codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=28, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"


class ThumbnailsConfig(_Section):
    enabled: bool = True
    width: int = Field(default=480, ge=64)
    jpeg_quality: int = Field(default=5, ge=1, le=31)
    per_moment: int = Field(default=3, ge=1)
    per_scene: int = Field(default=1, ge=1)


class RemotionConfig(_Section):
    """Remotion renders motion graphics only, as a transparent overlay.

    Remotion rasterises each frame through Chromium: right for captions and
    graphics, wrong for stitching twenty minutes of gameplay. The render is
    therefore FFmpeg (cut/concat/audio) -> Remotion (alpha overlay) -> FFmpeg
    (composite + encode).
    """

    enabled: bool = True
    mode: Literal["overlay", "full"] = "overlay"
    project_dir: str = "remotion"
    composition_id: str = "OverlayLayer"
    #: Chromium tabs rendering in parallel. 0 means "size to this machine"
    #: (cores minus two). The shipped 2 on a 12-thread machine was measured
    #: leaving an hour-long overlay pass on the table.
    concurrency: int = Field(default=0, ge=0)
    #: Frame rate of the overlay layer. 0 means "match the output video".
    #: Captions and lower thirds at 30 composite invisibly over 60 fps
    #: gameplay -- the overlay filter holds frames by timestamp -- and halving
    #: the frames halves the Chromium pass.
    overlay_fps: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(default=28800, ge=1)
    input_filename: str = "composition.json"
    overlay_format: str = "webm_vp9_alpha"
    overlay_codec_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    skip_when_no_overlays: bool = True

    @model_validator(mode="after")
    def _overlay_format_known(self) -> RemotionConfig:
        if self.overlay_codec_options and self.overlay_format not in self.overlay_codec_options:
            raise ValueError(
                f"remotion.overlay_format {self.overlay_format!r} has no entry in "
                "remotion.overlay_codec_options."
            )
        return self

    def overlay_codec(self) -> dict[str, Any]:
        """Return the codec options for the selected overlay format."""
        return dict(self.overlay_codec_options.get(self.overlay_format, {}))


# ---------------------------------------------------------------------------
# effects.yaml
# ---------------------------------------------------------------------------


class EffectTriggers(_Section):
    """What makes an effect appropriate (§68, §69).

    An effect with no triggers and a zero threshold is structural (transitions,
    captions); everything else must be earned by a moment or an event.
    """

    moment_types: list[MomentType] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def is_structural(self) -> bool:
        return not self.moment_types and not self.events and self.min_score == 0.0


class EffectLimits(_Section):
    """Rate limits. An effect that fires constantly stops being an effect (§69)."""

    max_per_minute: float = Field(default=2.0, ge=0)
    max_per_video: int = Field(default=20, ge=0)
    min_gap_seconds: float = Field(default=5.0, ge=0)


class EffectSpecConfig(_Section):
    """One entry in the effects library."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = True
    engine: EffectEngine
    category: EffectCategory
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    triggers: EffectTriggers = Field(default_factory=EffectTriggers)
    limits: EffectLimits = Field(default_factory=EffectLimits)


class EffectGlobalLimits(_Section):
    max_effects_per_minute: float = Field(default=6.0, ge=0)
    max_effects_per_moment: int = Field(default=3, ge=0)
    min_gap_seconds: float = Field(default=1.5, ge=0)
    max_consecutive_same_category: int = Field(default=1, ge=0)


class EffectStyleProfile(_Section):
    """How a style scales the library, rather than re-declaring it (§75)."""

    intensity: float = Field(default=0.6, ge=0.0, le=1.0)
    boost: list[EffectType] = Field(default_factory=list)
    suppress: list[EffectType] = Field(default_factory=list)

    @model_validator(mode="after")
    def _disjoint(self) -> EffectStyleProfile:
        overlap = set(self.boost) & set(self.suppress)
        if overlap:
            raise ValueError(
                "An effect cannot be both boosted and suppressed: "
                + ", ".join(sorted(item.value for item in overlap))
            )
        return self


class EffectRealisationConfig(_Section):
    """Which renderer wires are live (§68).

    The planner stored seventeen effects across three real projects before
    either wire existed, and not one reached the finished video -- the render
    worker passed an empty tuple to the overlay and nothing read the rows for
    FFmpeg at all. Each wire has its own switch so a misbehaving realiser can
    be turned off without silencing the planner or the other engine.
    """

    #: Bake duration-neutral FFmpeg effects (zoom, punch-in, bars, flash,
    #: shake) into the clip segments.
    ffmpeg_filters: bool = True
    #: Pass Remotion-engine effects to the overlay composition.
    remotion_overlay: bool = True


class EffectsConfig(_Section):
    """The effects engine (§68-§70).

    Effects are declarative data. Nothing here calls a renderer, and no effect
    is ever applied globally.
    """

    enabled: bool = True
    realisation: EffectRealisationConfig = Field(default_factory=EffectRealisationConfig)
    intensity: float = Field(default=0.6, ge=0.0, le=1.0)
    #: Rank each moment against *this* video's own scores before matching a
    #: trigger's ``min_score``. Absolute thresholds were calibrated on footage
    #: with a game profile, a microphone and detected reactions; without those
    #: the whole distribution shifts down and no effect ever fires -- which
    #: silently breaks §23's promise that the app works without a profile.
    relative_thresholds: bool = True
    #: Below this raw score no amount of ranking earns an effect. A video
    #: where nothing happened should stay undecorated, however its best
    #: moment ranks among its peers.
    absolute_floor: float = Field(default=0.2, ge=0.0, le=1.0)
    global_limits: EffectGlobalLimits = Field(default_factory=EffectGlobalLimits)
    library: dict[EffectType, EffectSpecConfig]
    style_profiles: dict[str, EffectStyleProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_profile_exists(self) -> EffectsConfig:
        if self.style_profiles and "default" not in self.style_profiles:
            raise ValueError(
                "effects.style_profiles must define a 'default' profile, used when a "
                "style has no entry of its own."
            )
        return self

    def spec(self, effect: EffectType) -> EffectSpecConfig | None:
        return self.library.get(effect)

    def enabled_effects(self) -> list[EffectType]:
        return [effect for effect, spec in self.library.items() if spec.enabled]

    def profile(self, style: str) -> EffectStyleProfile:
        """Return the style profile, falling back to ``default``."""
        return self.style_profiles.get(
            style, self.style_profiles.get("default", EffectStyleProfile())
        )


# ---------------------------------------------------------------------------
# audio.yaml
# ---------------------------------------------------------------------------


class AnalysisAudioConfig(_Section):
    sample_rate: int = Field(default=16000, ge=8000)
    channels: int = Field(default=1, ge=1, le=2)
    codec: str = "pcm_s16le"
    format: str = "wav"


class MixGains(_Section):
    game: float = 0.0
    microphone: float = 0.0
    music: float = -18.0
    effects: float = -3.0


class MixNormalization(_Section):
    enabled: bool = True
    target_lufs: float = -14.0
    true_peak_db: float = -1.0
    loudness_range: float = Field(default=11.0, gt=0)


class LimiterConfig(_Section):
    enabled: bool = True
    ceiling_db: float = -0.3


class MixConfig(_Section):
    tracks: list[str] = Field(min_length=1)
    gains_db: MixGains = Field(default_factory=MixGains)
    normalization: MixNormalization = Field(default_factory=MixNormalization)
    limiter: LimiterConfig = Field(default_factory=LimiterConfig)


class DuckingConfig(_Section):
    """§74: music drops for speech and for important game audio."""

    enabled: bool = True
    speech_duck_db: float = -14.0
    game_event_duck_db: float = -8.0
    #: How far the *gameplay* track drops under speech. §72 orders the mix
    #: Speech > Important Game Audio > Music, and without this the order holds
    #: only until the game gets loud -- which is exactly when someone shouts.
    #: Much shallower than the music duck: the gameplay is the subject of the
    #: video, and burying it would be a worse mix than a slightly crowded one.
    game_under_speech_db: float = -4.0
    attack_ms: int = Field(default=120, ge=0)
    release_ms: int = Field(default=700, ge=0)
    hold_ms: int = Field(default=250, ge=0)


class MusicConfig(_Section):
    """Local, user-provided music only. Nothing is downloaded (§73)."""

    enabled: bool = True
    source: Literal["local_only"] = "local_only"
    directory: str = "assets/music"
    loop: bool = True
    fade_in_ms: int = Field(default=800, ge=0)
    fade_out_ms: int = Field(default=1200, ge=0)
    change_on_section: bool = True


class FadesConfig(_Section):
    clip_fade_in_ms: int = Field(default=0, ge=0)
    clip_fade_out_ms: int = Field(default=0, ge=0)
    program_fade_in_ms: int = Field(default=250, ge=0)
    program_fade_out_ms: int = Field(default=600, ge=0)
    crossfade_ms: int = Field(default=60, ge=0)


class ClippingConfig(_Section):
    detect: bool = True
    threshold_db: float = -0.1


class SyncConfig(_Section):
    max_offset_ms: int = Field(default=40, ge=0)


class AudioConfig(_Section):
    analysis: AnalysisAudioConfig = Field(default_factory=AnalysisAudioConfig)
    mix: MixConfig
    ducking: DuckingConfig = Field(default_factory=DuckingConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    fades: FadesConfig = Field(default_factory=FadesConfig)
    clipping: ClippingConfig = Field(default_factory=ClippingConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)


# ---------------------------------------------------------------------------
# captions.yaml
# ---------------------------------------------------------------------------


class CaptionTimingConfig(_Section):
    min_duration_ms: int = Field(default=700, ge=1)
    max_duration_ms: int = Field(default=5000, ge=1)
    gap_ms: int = Field(default=60, ge=0)
    lead_in_ms: int = 0
    min_readable_ms: int = Field(default=400, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> CaptionTimingConfig:
        if self.min_duration_ms > self.max_duration_ms:
            raise ValueError("captions.timing.min_duration_ms exceeds max_duration_ms.")
        return self


class CaptionLayoutConfig(_Section):
    max_chars_per_line: int = Field(default=38, ge=8)
    max_lines: int = Field(default=2, ge=1, le=4)
    position: Literal["bottom_center", "top_center", "center"] = "bottom_center"
    safe_area_bottom: float = Field(default=0.12, ge=0.0, lt=0.5)
    safe_area_horizontal: float = Field(default=0.06, ge=0.0, lt=0.5)
    avoid_hud_regions: bool = True


class CaptionAppearanceConfig(_Section):
    font_family: str = "Inter"
    font_weight: int = Field(default=800, ge=100, le=900)
    font_size_ratio: float = Field(default=0.048, gt=0, lt=0.5)
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD400"
    outline_color: str = "#000000"
    outline_width: int = Field(default=3, ge=0)
    shadow_opacity: float = Field(default=0.55, ge=0.0, le=1.0)


class CaptionLanguagesConfig(_Section):
    default: str = "auto"
    rtl: list[str] = Field(default_factory=list)


class CaptionsConfig(_Section):
    enabled: bool = True
    #: Below this transcription confidence a segment is not captioned. Whisper
    #: reports exp(avg_logprob) -- its own belief in what it wrote -- and on a
    #: real recording 95 of 129 segments sat under 0.45, several of them the
    #: classic Arabic subtitle hallucinations ("المترجم...") at 0.08. Burning
    #: text the model itself did not believe into the picture reads as broken.
    #: The speech still plays; only the caption is withheld.
    min_confidence: float = Field(default=0.30, ge=0.0, le=1.0)
    formats: list[Literal["srt", "vtt"]] = Field(min_length=1)
    burn_in: bool = True
    style: Literal["standard", "animated"] = "animated"
    word_highlighting: bool = True
    emphasis: bool = True
    timing: CaptionTimingConfig = Field(default_factory=CaptionTimingConfig)
    layout: CaptionLayoutConfig = Field(default_factory=CaptionLayoutConfig)
    appearance: CaptionAppearanceConfig = Field(default_factory=CaptionAppearanceConfig)
    languages: CaptionLanguagesConfig = Field(default_factory=CaptionLanguagesConfig)

    def is_rtl(self, language: str) -> bool:
        """Whether ``language`` needs right-to-left caption layout."""
        return language.split("-")[0].lower() in {code.lower() for code in self.languages.rtl}


# ---------------------------------------------------------------------------
# qa.yaml
# ---------------------------------------------------------------------------


class TechnicalQaThresholds(_Section):
    duration_tolerance_seconds: float = Field(default=2.0, ge=0)
    fps_tolerance: float = Field(default=0.5, ge=0)
    black_frame_ratio: float = Field(default=0.98, ge=0.0, le=1.0)
    max_black_run_seconds: float = Field(default=2.0, ge=0)
    max_frozen_run_seconds: float = Field(default=3.0, ge=0)

    #: How different two frames may be and still count as the same picture,
    #: as ``freezedetect``'s noise floor in dBFS (-60dB is a 0.001 difference
    #: ratio, -50dB is 0.00316).
    #:
    #: This is the setting that decides whether the check measures the video or
    #: the encoder. Below about -50dB the answer depends on which encoder wrote
    #: the file -- the same footage through libx264 and NVENC disagrees, because
    #: at that scale the difference between frames is quantisation noise rather
    #: than picture. Measured against real menu, pause and gameplay footage
    #: through both encoders; see docs/PHASE_11.md.
    freeze_noise_db: float = Field(default=-45.0, le=0)

    max_av_desync_ms: int = Field(default=80, ge=0)
    max_caption_desync_ms: int = Field(default=250, ge=0)
    min_audio_lufs: float = -30.0


class TechnicalQaConfig(_Section):
    enabled: bool = True
    checks: dict[str, bool] = Field(default_factory=dict)
    thresholds: TechnicalQaThresholds = Field(default_factory=TechnicalQaThresholds)


class ContentQaThresholds(_Section):
    max_silence_seconds: float = Field(default=12.0, ge=0)
    min_sequence_coherence: float = Field(default=0.5, ge=0.0, le=1.0)


class ContentQaConfig(_Section):
    enabled: bool = True
    checks: dict[str, bool] = Field(default_factory=dict)
    thresholds: ContentQaThresholds = Field(default_factory=ContentQaThresholds)


class QaPolicyConfig(_Section):
    fail_on_technical_error: bool = True
    fail_on_content_warning: bool = False
    max_warnings_before_review: int = Field(default=5, ge=0)


class QaConfig(_Section):
    technical: TechnicalQaConfig = Field(default_factory=TechnicalQaConfig)
    content: ContentQaConfig = Field(default_factory=ContentQaConfig)
    policy: QaPolicyConfig = Field(default_factory=QaPolicyConfig)


# ---------------------------------------------------------------------------
# publishing.yaml
# ---------------------------------------------------------------------------


class PublishDefaultsConfig(_Section):
    visibility: PublishVisibility = PublishVisibility.PRIVATE
    made_for_kids: bool = False
    category: str | None = None
    generate_chapters: bool = True
    min_chapter_seconds: float = Field(default=30.0, gt=0)


class LocalFilePublishConfig(_Section):
    default_directory: str | None = None
    overwrite: bool = False


class YoutubePublishConfig(_Section):
    """Planned destination. Disabled; no implementation is registered yet.

    Credentials are referenced by identifier only -- the secret itself lives in
    the OS credential store, never in configuration.
    """

    enabled: bool = False
    credential_id: str | None = None
    channel_id: str | None = None
    default_playlist: str | None = None
    default_visibility: PublishVisibility = PublishVisibility.PRIVATE
    require_explicit_confirmation: bool = True
    upload_chunk_bytes: int = Field(default=8 * 1024 * 1024, ge=262144)
    max_retries: int = Field(default=5, ge=0)


class PublishingConfig(_Section):
    """Delivery of a finished render.

    Nothing is uploaded automatically (§51): a publication always originates
    from an explicit user action.
    """

    enabled: bool = True
    enabled_targets: list[PublishTarget] = Field(min_length=1)
    default_target: PublishTarget = PublishTarget.LOCAL_FILE
    defaults: PublishDefaultsConfig = Field(default_factory=PublishDefaultsConfig)
    local_file: LocalFilePublishConfig = Field(default_factory=LocalFilePublishConfig)
    youtube: YoutubePublishConfig = Field(default_factory=YoutubePublishConfig)

    @model_validator(mode="after")
    def _consistent(self) -> PublishingConfig:
        if self.default_target not in self.enabled_targets:
            raise ValueError(
                "publishing.default_target must be listed in publishing.enabled_targets."
            )
        if self.youtube.enabled and PublishTarget.YOUTUBE not in self.enabled_targets:
            raise ValueError(
                "publishing.youtube.enabled is true but 'youtube' is not in "
                "publishing.enabled_targets."
            )
        return self


# ---------------------------------------------------------------------------
# interaction.yaml
# ---------------------------------------------------------------------------


class IntentResolutionConfig(_Section):
    project_settings_override_preset: bool = True
    max_updates_per_project: int = Field(default=500, ge=1)


class QaConfigInteraction(_Section):
    require_analysis_complete: bool = True
    max_results: int = Field(default=10, ge=1)
    timestamp_context_seconds: float = Field(default=15.0, gt=0)
    min_confidence_to_answer: float = Field(default=0.35, ge=0.0, le=1.0)


class LlmFallbackConfig(_Section):
    """An LLM is the fallback, not the default (§20)."""

    enabled: bool = True
    max_input_records: int = Field(default=60, ge=1)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)


class CommandsConfig(_Section):
    enabled: bool = True
    auto_rerender: bool = False
    keep_versions: int = Field(default=20, ge=1)


class ConversationConfig(_Section):
    enabled: bool = True
    max_messages: int = Field(default=500, ge=1)
    context_window_messages: int = Field(default=12, ge=1)


class InteractionConfig(_Section):
    """Editing instructions, questions and commands.

    Optional by design: with no instructions the default preset produces a
    finished video end to end.
    """

    enabled: bool = True
    default_preset: str = "best_moments"
    intent: IntentResolutionConfig = Field(default_factory=IntentResolutionConfig)
    qa: QaConfigInteraction = Field(default_factory=QaConfigInteraction)
    llm_fallback: LlmFallbackConfig = Field(default_factory=LlmFallbackConfig)
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)


class PresetConfig(_Section):
    """One editing preset (§4).

    Unknown keys are allowed here alone: a preset may carry a dimension the
    intent schema has not grown yet, and it lands in ``EditingIntent.custom``
    rather than being rejected.
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    label: str
    description: str = ""
    mode: VideoMode | None = None
    pacing: str | None = None
    dead_time_policy: str | None = None
    context_preservation: str | None = None
    effects: str | None = None
    captions: str | None = None
    music: str | None = None
    variety: str | None = None
    style: str | None = None
    priority_moment_types: list[MomentType] = Field(default_factory=list)
    priority_events: list[str] = Field(default_factory=list)
    avoid_moment_types: list[MomentType] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


class AppConfig(_Section):
    """The complete validated configuration."""

    application: ApplicationConfig = Field(default_factory=ApplicationConfig)
    output: OutputConfig
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    models: ModelsConfig
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    gpu: GpuConfig = Field(default_factory=GpuConfig)
    hardware: HardwareConfig
    fallbacks: FallbacksConfig = Field(default_factory=FallbacksConfig)
    moments: MomentsConfig
    narrative: NarrativeConfig
    video: VideoConfig
    render: RenderConfig
    youtube_preset: YoutubePresetConfig = Field(default_factory=YoutubePresetConfig)
    ffmpeg: FfmpegConfig = Field(default_factory=FfmpegConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    thumbnails: ThumbnailsConfig = Field(default_factory=ThumbnailsConfig)
    remotion: RemotionConfig = Field(default_factory=RemotionConfig)
    effects: EffectsConfig
    audio: AudioConfig
    captions: CaptionsConfig
    qa: QaConfig = Field(default_factory=QaConfig)
    publishing: PublishingConfig
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    presets: dict[str, PresetConfig]

    @model_validator(mode="after")
    def _default_preset_exists(self) -> AppConfig:
        if self.interaction.default_preset not in self.presets:
            raise ValueError(
                f"interaction.default_preset {self.interaction.default_preset!r} is not "
                f"defined in presets. Available: {', '.join(sorted(self.presets))}."
            )
        return self

    @property
    def duration_policy(self) -> DurationPolicy:
        """Effective 10-60 minute policy for this installation (§6)."""
        return self.output.duration_policy()

    @property
    def render_engines(self) -> tuple[RenderEngine, ...]:
        """Engines available for the final render (§67)."""
        engines = [RenderEngine.FFMPEG]
        if self.remotion.enabled:
            engines.insert(0, RenderEngine.REMOTION)
        return tuple(engines)


__all__ = ["AppConfig", "HardwareProfileConfig"]
