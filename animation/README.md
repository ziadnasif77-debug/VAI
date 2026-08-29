# Animation P0 — the spine, proven before the factory

Scope per the approved architecture v2 and the owner's order: **Audit →
Extraction → P0** — nothing else. No rabbit, no SDXL, no worker fleet. This
directory imports nothing from `backend/` on purpose (see
`docs/PLATFORM_AUDIT.md` §3): P0 proves the renderer, not the platform.

## The layering contract (amendment #1)

    compile.py (Python: validate, synth stand-in audio, master)   ← business side
        └── Godot headless + ScenePlayer.gd                        ← renderer ONLY
                └── PNG frames + WAV
                        └── ffmpeg NVENC → MP4

Godot decides nothing: it plays `scenes/*.json` (Scene Document v1,
`schemas/scene_document.schema.json`) with frame-indexed arithmetic and a
seeded RNG. Same JSON = same performance.

## What the test scene exercises

`scenes/p0_hello_walk.json`, 10 s, exactly the owner's P0 checklist:
placeholder character → **walk** (bob, arm/leg swing, ear follow-through) →
**stop-settle** (overshoot damping) → **look** left/right (eased head + leading
pupils) → worried brows → **talk** (viseme-driven mouth from
`line1.visemes.json`) → **camera** follow then push-in → **lighting** fade to
warm → **SFX** (footsteps, voice stand-in, sad chirp) → 1080p24 MP4. Blinks:
seeded, precomputed. Particles/physics: absent by design in P0.

## Run

    python animation/compile.py animation/scenes/p0_hello_walk.json --out animation/out/run_a
    python animation/verify_determinism.py animation/scenes/p0_hello_walk.json

Requires the Godot 4.x executable at `tools/godot/godot.exe` (or `GODOT_EXE`).

## The two gates

1. **Determinism (amendment #2)**: `verify_determinism.py` renders twice —
   ≥ 99.5% byte-identical frames, any stragglers ≥ 48 dB PSNR, audio exact.
   MP4 byte-identity is out of contract (encoder/container metadata).
2. **The eyeball**: if the character reads as a robotic puppet, the animation
   engine gets fixed here — before any factory exists.

---

## PARKED (2026-08-29, owner's order) — resume notes

The owner paused the kids/animation project after P0. State at parking:

**Proven and committed**: 2D + 3D ScenePlayer twins off one Scene Document
(dimension field, Boot router); determinism gate PASSED at 100% (241/241
byte-identical, twice) for both dimensions pre-speech; **Arabic speech
works** -- Windows OneCore *Microsoft Naayf* (ar-SA, local, free) through
scratch say_arabic.ps1, bunny-pitched (+15% via asetrate at the SOURCE rate
-- it is 16 kHz, not 48), and `lipsync.py` turns Arabic text into the nine-
viseme timeline over the measured duration (harakat honoured, shadda doubles,
long vowels weighted). Demo: out/arabic_demo/p0_arabic.mp4.

**Open issue #1 (the real one)**: windowed Movie Maker presents are hostage
to desktop occlusion -- a covered window skips presents while logic ticks,
injecting a frame-lag (measured: run_b f27 == run_a f22) that breaks the
gate. `--headless` writes zero frames; `window/size/mode=1` (minimized)
made it worse (8% identical). **Next step decided**: SubViewport self-capture --
ScenePlayer renders into an offscreen SubViewport (UPDATE_ALWAYS) and saves
each frame itself via get_image().save_png(frame_idx); keep --write-movie
only for the WAV. Present-skips then cannot touch the contract.

**Open issue #2**: compile.py's godot invocation started returning rc=0
with zero frames right after a project.godot edit, while the identical
direct command line works. Un-diagnosed; suspect first-launch reimport
interplay. Reproduce before trusting compile.py again.

