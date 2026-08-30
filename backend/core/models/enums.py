"""Enumerations fixed by the specification.

Each enum cites the section that defines its members. Values are the strings
persisted in SQLite and sent over the API, so they are part of the schema
contract: adding a member is a schema change, renaming one needs a migration.
"""

from __future__ import annotations

from enum import Enum


class ProjectStatus(str, Enum):
    """Project lifecycle, following the §122 user workflow."""

    CREATED = "created"
    IMPORTING = "importing"
    ANALYZING = "analyzing"
    MOMENTS_READY = "moments_ready"
    EDIT_GENERATED = "edit_generated"
    REVIEW = "review"
    RENDERING = "rendering"
    QA = "qa"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


class VideoMode(str, Enum):
    """Video modes (§35)."""

    STORY = "story"
    BEST_MOMENTS = "best_moments"
    COMPILATION = "compilation"


class HardwareProfile(str, Enum):
    """Hardware strategy profiles (§53)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MediaRole(str, Enum):
    """What a source file contributes to the project (§4, §19).

    Gameplay audio and microphone audio are analysed independently because an
    explosion and a player scream carry very different semantic weight.
    """

    GAMEPLAY = "gameplay"
    MICROPHONE = "microphone"
    WEBCAM = "webcam"
    EXTERNAL_AUDIO = "external_audio"
    MUSIC = "music"


class MediaState(str, Enum):
    """Per-file processing state, advanced by the §46 stage chain."""

    REGISTERED = "registered"
    PROBED = "probed"
    PROXIED = "proxied"
    AUDIO_EXTRACTED = "audio_extracted"
    ANALYZED = "analyzed"
    FAILED = "failed"


class TrackKind(str, Enum):
    """Timeline tracks (§41)."""

    VIDEO = "video"
    AUDIO = "audio"
    CAPTION = "caption"
    MUSIC = "music"
    EFFECT = "effect"
    OVERLAY = "overlay"


class JobStage(str, Enum):
    """Pipeline stages (§46), in dependency order.

    ``FRAMES`` and ``OCR`` are explicit stages because §126 schedules them as
    separate development steps (06 Proxy/Frames, 11 OCR) and both produce
    independently cacheable artefacts.
    """

    IMPORT = "import"
    PROBE = "probe"
    PROXY = "proxy"
    AUDIO = "audio"
    FRAMES = "frames"
    TRANSCRIPT = "transcript"
    AUDIO_EVENTS = "audio_events"
    SCENES = "scenes"
    VISION = "vision"
    OCR = "ocr"
    GAME_EVENTS = "game_events"
    #: V2-P1. The session's meaning, fused from everything above it and read
    #: by everything below: selection, pacing, emphasis, audio, critic, QA.
    #: It sits before MOMENTS on purpose -- built after selection, as it was
    #: first written, it could shape how a moment was cut but never which
    #: moment was chosen.
    SEMANTIC = "semantic"
    MOMENTS = "moments"
    STORY = "story"
    EDL = "edl"
    #: Phase E. The first stage that reads the pipeline's own output rather
    #: than the recording: it reviews the assembled edit and may trim or drop
    #: a clip before anything is encoded.
    CRITIQUE = "critique"
    RENDER = "render"
    QA = "qa"
    #: Vertical cuts of the strongest moments (9:16), from the same analysis.
    #: Delivery-family and manual like its siblings: one long video plus N
    #: Shorts is the reach model every competitor is built on, and here it
    #: costs no second analysis.
    SHORTS = "shorts"
    #: Delivery stages. Never auto-queued -- the user triggers them from the
    #: export screen. PUBLISH exists so distribution (YouTube auto-publish and
    #: any later target) plugs into the finished pipeline instead of forcing a
    #: rewrite; the MVP ships the local-file publisher only.
    EXPORT = "export"
    PUBLISH = "publish"


class JobStatus(str, Enum):
    """Job states (§81)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether the job will not change state without user action."""
        return self in {JobStatus.COMPLETED, JobStatus.CANCELLED}

    @property
    def is_resumable(self) -> bool:
        """Whether crash recovery may re-queue this job (§47)."""
        return self in {JobStatus.RUNNING, JobStatus.FAILED}


class DetectorSource(str, Enum):
    """Where an event observation came from (§26, §27)."""

    VISION = "vision"
    AUDIO = "audio"
    OCR = "ocr"
    MICROPHONE = "microphone"
    SPEECH = "speech"
    SCENE = "scene"
    HUD = "hud"
    PROFILE = "profile"


class GameEventType(str, Enum):
    """Gameplay events (§21)."""

    KILL = "kill"
    MULTI_KILL = "multi_kill"
    DEATH = "death"
    NEAR_DEATH = "near_death"
    CLUTCH = "clutch"
    VICTORY = "victory"
    DEFEAT = "defeat"
    BOSS_FIGHT = "boss_fight"
    BOSS_DEFEAT = "boss_defeat"
    OBJECTIVE = "objective"
    OBJECTIVE_FAILURE = "objective_failure"
    RARE_LOOT = "rare_loot"
    RARE_EVENT = "rare_event"
    HIGH_DAMAGE = "high_damage"
    LOW_HEALTH = "low_health"
    ESCAPE = "escape"
    CHASE = "chase"
    OUTPLAY = "outplay"
    COMEBACK = "comeback"
    FAIL = "fail"
    FUNNY_MOMENT = "funny_moment"
    # -- open-world vocabulary (Phase 0.2) --------------------------------
    # The shooter words above (kill, multi_kill, clutch) have never been
    # detected on this footage and honestly cannot be: an open-world game has
    # no kill feed to read. What the evidence *can* support is that a fight
    # was on screen while something was heard, or that a vehicle hit something.
    COMBAT = "combat"
    COLLISION = "collision"
    #: Several detectors agreed something was there and none could name it.
    #: It was called ``unexpected_event`` until V2-P2, which claimed a
    #: surprise the evidence never established -- and it is the majority
    #: label on real footage (59% of the gate session). When the system does
    #: not know what happened it says so, rather than inventing a reading.
    UNKNOWN_EVENT = "unknown_event"


class FrameState(str, Enum):
    """What a frame is showing, as opposed to what is happening in it.

    Gameplay is the only state a highlight can be made of, and the vision
    model has been reporting the others all along -- ``menu``, ``loading`` and
    ``cutscene`` are three of the ten labels its prompt asks for. Nothing read
    them, so menus were edited into finished videos and the content QA
    reported them afterwards: 40 such moments in one real project.
    """

    GAMEPLAY = "gameplay"
    MENU = "menu"
    LOADING = "loading"
    CUTSCENE = "cutscene"
    HUD_ONLY = "hud_only"
    PAUSE = "pause"
    TRANSITION = "transition"
    #: The operating system or a recording/streaming app is on screen --
    #: OBS chrome, file windows, the desktop -- even when the game is alive
    #: inside a small preview. Measured 2026-08-28: a video opened on OBS
    #: itself and every sampled detector called the preview gameplay.
    DESKTOP = "desktop"
    UNKNOWN = "unknown"

    @property
    def is_gameplay(self) -> bool:
        """Whether footage in this state can carry a highlight.

        ``UNKNOWN`` counts as gameplay: an unlabelled frame is not evidence of
        a menu, and excluding it would silently drop footage for lack of a
        label rather than because of one.
        """
        return self in (FrameState.GAMEPLAY, FrameState.HUD_ONLY, FrameState.UNKNOWN)


class MomentType(str, Enum):
    """Moment taxonomy (§34)."""

    EPIC = "epic"
    CLUTCH = "clutch"
    FUNNY = "funny"
    FAIL = "fail"
    REACTION = "reaction"
    SKILL = "skill"
    OUTPLAY = "outplay"
    CHAOS = "chaos"
    TENSION = "tension"
    SURPRISE = "surprise"
    RAGE = "rage"
    COMEBACK = "comeback"
    BOSS = "boss"
    VICTORY = "victory"
    DEFEAT = "defeat"
    DISCOVERY = "discovery"
    RARE = "rare"


class ReactionType(str, Enum):
    """Player reactions (§20)."""

    LAUGH = "laugh"
    SCREAM = "scream"
    SURPRISE = "surprise"
    ANGER = "anger"
    CELEBRATION = "celebration"
    FEAR = "fear"
    DISAPPOINTMENT = "disappointment"
    CONFUSION = "confusion"
    EXCITEMENT = "excitement"


class DeadTimeCategory(str, Enum):
    """Low-value gameplay segments (§30)."""

    WALKING = "walking"
    WAITING = "waiting"
    LOADING = "loading"
    MENU = "menu"
    INVENTORY = "inventory"
    TRAVEL = "travel"
    AFK = "afk"
    LONG_SILENCE = "long_silence"
    REPETITIVE_FARMING = "repetitive_farming"
    REPEATED_FAILED_ATTEMPTS = "repeated_failed_attempts"


class AudioEventType(str, Enum):
    """Audio events (§18)."""

    SPIKE = "spike"
    SILENCE = "silence"
    SPEECH = "speech"
    SHOUT = "shout"
    LAUGH = "laugh"
    EXPLOSION = "explosion"
    GUNSHOT = "gunshot"
    TRANSIENT = "transient"
    MUSIC_CUE = "music_cue"


class EffectCategory(str, Enum):
    """How an effect reads on screen. Used to keep a video from stacking three
    effects of the same character on one moment."""

    CAMERA = "camera"
    TIME = "time"
    LIGHT = "light"
    GRAPHIC = "graphic"
    TEXT = "text"
    TRANSITION = "transition"
    AUDIO = "audio"
    FRAME = "frame"


class EffectEngine(str, Enum):
    """Which renderer realises an effect.

    Effects that alter the gameplay footage itself are FFmpeg's; effects drawn
    on top of it are Remotion's, rendered onto a transparent overlay.
    """

    FFMPEG = "ffmpeg"
    REMOTION = "remotion"


class EffectType(str, Enum):
    """The effects library (§68).

    Every entry is declarative: an effect is data on the timeline, not a call
    into a renderer. The engine that realises it is a property of the effect,
    not something the planner decides.
    """

    # -- camera: alters framing of the base footage -----------------------
    ZOOM = "zoom"
    PUNCH_IN = "punch_in"
    CAMERA_SHAKE = "camera_shake"
    CROP = "crop"

    # -- time -------------------------------------------------------------
    SLOW_MOTION = "slow_motion"
    SPEED_RAMP = "speed_ramp"
    FREEZE_FRAME = "freeze_frame"

    # -- light and focus --------------------------------------------------
    FLASH = "flash"
    BLUR = "blur"
    GLOW = "glow"
    MOTION_BLUR = "motion_blur"

    # -- graphics drawn over the footage ----------------------------------
    HIGHLIGHT_BOX = "highlight_box"
    CROSSHAIR = "crosshair"
    IMPACT = "impact"
    KILL_COUNTER = "kill_counter"
    MEME = "meme"

    # -- text -------------------------------------------------------------
    TEXT_POP = "text_pop"
    DYNAMIC_CAPTIONS = "dynamic_captions"

    # -- structural -------------------------------------------------------
    TRANSITION = "transition"
    FADE = "fade"
    CINEMATIC_BARS = "cinematic_bars"

    # -- audio ------------------------------------------------------------
    SOUND_EFFECT = "sound_effect"


class TransitionType(str, Enum):
    """Clip transitions. ``CUT`` is the default; others are used sparingly."""

    CUT = "cut"
    FADE = "fade"
    CROSSFADE = "crossfade"
    DIP_TO_BLACK = "dip_to_black"


class RenderEngine(str, Enum):
    """Rendering adapters (§67). The timeline never names one of these."""

    FFMPEG = "ffmpeg"
    REMOTION = "remotion"


class QAStatus(str, Enum):
    """QA outcome (§76)."""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class PublishTarget(str, Enum):
    """Where a finished render can be delivered.

    ``LOCAL_FILE`` is the only target implemented in the MVP: the render is
    written to a user-chosen location. ``YOUTUBE`` is declared so the database,
    the API and the job graph already have a slot for it; attempting to use it
    before its publisher is registered fails with a typed error rather than
    silently doing nothing.
    """

    LOCAL_FILE = "local_file"
    YOUTUBE = "youtube"


class PublishStatus(str, Enum):
    """Lifecycle of one publication attempt."""

    PENDING = "pending"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublishVisibility(str, Enum):
    """Visibility requested for a publication.

    Mirrors YouTube's three states. Meaningless for a local file, so the local
    publisher ignores it -- but storing it now means the metadata model does not
    change when a real destination arrives.
    """

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class HealthStatus(str, Enum):
    """Environment validation result for one dependency."""

    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


__all__ = [
    "AudioEventType",
    "DeadTimeCategory",
    "DetectorSource",
    "EffectCategory",
    "EffectEngine",
    "EffectType",
    "GameEventType",
    "HardwareProfile",
    "HealthStatus",
    "JobStage",
    "JobStatus",
    "MediaRole",
    "MediaState",
    "MomentType",
    "ProjectStatus",
    "PublishStatus",
    "PublishTarget",
    "PublishVisibility",
    "QAStatus",
    "ReactionType",
    "RenderEngine",
    "TrackKind",
    "TransitionType",
    "VideoMode",
]
