# Phase 3 — Speech and Audio

SPEC §14, §18, §19, §20; §126 step 07. **Acceptance: transcript timestamps
align with the source, and a separate microphone track is analysed
independently of gameplay audio.**

Status: **complete and verified.** 580 tests pass, `ruff check` is clean, and
both halves of the acceptance criterion are checked against real files — not
against mocks of them.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Speech-to-text with word timestamps (§14) | `ai/speech/faster_whisper_provider.py` | `test_speech.py` · `TestRealWhisper` |
| Deterministic test double | `ai/speech/fake_provider.py` | `TestFakeProvider` — 8 tests |
| Windowed audio features (§18) | `backend/analysis/signal.py` | `TestFeatures`, `TestStreamReading` |
| Audio events (§18) | `backend/analysis/audio_events.py` | `TestAudioEventDetection` — 7 tests |
| Microphone independence (§19) | role assignment in `speech_workers.py` | `TestMicrophoneIndependence` |
| Player reactions (§20) | `backend/analysis/reactions.py` | `TestReactions` — 7 tests |
| TRANSCRIPT stage | `backend/pipeline/workers/speech_workers.py` | `TestTranscriptAlignment` — 7 tests |
| AUDIO_EVENTS stage | same | `TestMicrophoneIndependence` — 5 tests |
| Persistence | `repositories/transcript.py`, `repositories/audio_events.py` | pipeline tests |

`IMPORT → PROBE → PROXY → AUDIO → FRAMES → TRANSCRIPT → AUDIO_EVENTS` now runs
end to end. SCENES onward have no workers yet; the runner stops there and says
so.

---

## What is measured, and what is inferred

This distinction decides how much later stages should trust any of it, so it is
stated in the code as well as here.

**Measured.** Level, dynamics, silence, onsets, spectral change, amplitude
modulation. These are arithmetic on the waveform and are as reliable as the
recording.

**Inferred.** Whether an onset was a gunshot or a door. Whether raised voice
was excitement or anger. Nothing in a level curve knows that — and §26 does not
ask it to. An audio event is *one detector's observation*; a gameplay event
only exists once several sources agree (§27). So events describe the **signal**
— a spike, a transient, a sustained loud passage — with confidence derived from
how far the evidence sits above the recording's own baseline.

**Not attempted.** §20 lists nine reaction types. Three are separable
acoustically and are detected: laughter (amplitude modulation at 3–8 Hz),
screaming (far above the speaker's baseline and spectrally bright), and raised
animated speech. Anger, fear, disappointment and confusion are not, and are not
guessed at. A confident wrong label is worse than an honest coarse one:
correlation lets detectors outvote each other, and one that overstates its
certainty beats the ones that actually know.

---

## The decisions worth knowing

### Thresholds are relative to the recording's own baseline

Capture setups differ by twenty decibels. A fixed "−20 dBFS is loud" rule finds
everything in one recording and nothing in the next. Every level threshold is
measured against a rolling **median** — a median specifically, because a spike
is exactly what must not move the baseline it is being measured against.

### Laughter needs a finer envelope than the main pass provides

Laughter pulses at roughly 3–8 Hz. The main analysis pass runs at a 0.25 s hop,
which samples the envelope at 4 Hz — below the Nyquist limit for a 5 Hz pulse,
so that pass cannot represent laughter at all. Running the detector on it would
not be inaccurate; it would be meaningless.

Candidate spans are therefore re-read at a 25 ms hop, sampling the envelope at
40 Hz. Only the few seconds a candidate occupies are re-read, by seeking — which
is why `read_windows` grew a span argument.

### Reaction candidates come from spikes, gated by voice — not merged with it

The first implementation merged loud passages with voiced passages. The speech
detector routinely marks whole minutes at once (quiet room tone passes its level
and spectrum tests), so merging dissolved every localised burst into one span
covering the recording — which is precisely what a reaction is not. Voice
activity is now a **gate**: a spike that overlaps no voiced region is a desk
knock, not a player.

### One impact, one transient

Analysis windows overlap by design, so two adjacent windows share samples and
cannot have heard independent onsets. Without collapsing them, one explosion
arrives as two or three transients and every downstream count — candidate
regions, correlation weight, moment scoring — is inflated by an artefact of the
window layout.

### The model is loaded once per stage and released

Whisper large-v3 costs tens of seconds and ~5 GB of VRAM to load. Per chunk that
multiplies by the chunk count; held after the stage it leaves nothing for the
vision model on an 8 GB card (§54). A test asserts `load_count == 1` and
`unload_count == 1`, because the regression is invisible otherwise.

### CPU cannot run float16

CTranslate2 rejects it. The configured `compute_type` is written for the GPU
path, so falling back to CPU also means falling back to a compute type the CPU
supports — otherwise §52's "CPU fallback must exist" is true only on paper.

### Stages read their input from the upstream stage's result

Not by rebuilding paths from naming conventions. The job result is the contract
(§81). Guessing works right up until a naming rule changes in one place and not
the other, and then it fails silently by finding nothing.

---

## Acceptance

### Half one: timestamps align with the source

The stage chunks the audio and transcribes each chunk separately, so every
timestamp comes back relative to a slice and has to be moved onto the
recording's timeline. Two things can go wrong, and both are tested with a chunk
size small enough that a six-second clip produces several chunks:

* **Wrong offset** — chunk five's speech attributed to the first ten minutes.
  Tested by asserting coverage reaches the end of the recording and that the
  provider was called with non-zero offsets.
* **Double-counting the overlap** — chunks overlap so a sentence on a boundary
  is heard whole by one of them; keeping both copies stores it twice. A segment
  belongs to the chunk whose core contains its midpoint, and every instant has
  exactly one such chunk (the invariant `plan_chunks` guarantees).

### Half two: the microphone is analysed independently

Tested on a recording whose second audio track carries a **real laugh** (5 Hz
modulated) and a **real scream** (loud and bright), each following a gameplay
impact on the first track — built numerically, because no synthetic source
generator produces a 5 Hz amplitude modulation on request.

The test asserts more than "two tracks were processed":

* three impacts found on the game track, at 5 s, 15 s and 25 s;
* a laugh and a scream found on the microphone track, and no third reaction
  after the third impact — the detector does not invent one where there was
  nothing;
* each reaction correlated with the impact that preceded it, with the offset
  recorded;
* both track roles surviving persistence, which is what §27 will need.

---

## Verification status

| Check | Result |
| --- | --- |
| `pytest` | **580 passed**, 2 skipped |
| `ruff check .` | clean |
| Transcript alignment across chunks | passed |
| Microphone independence and correlation | passed |
| Real faster-whisper provider | passed (`VAI_TEST_MODELS=1`) |
| `doctor.py` | 1 warning — Ollama models, a Phase 4 dependency |

The two skips are the real-model tests, opt-in because the first run downloads
model weights:

```bash
VAI_TEST_MODELS=1 python -m pytest -m requires_models -q
```

Both were run on this machine and passed. They check the property that matters
most in practice: **VAD suppresses hallucination on silence.** Whisper invents
text over quiet audio, a gameplay recording is mostly silence between callouts,
and a hallucinated "thanks for watching" becomes a caption in the finished
video.

---

## Bugs found while building this

Not test failures — defects the work surfaced:

| Defect | Consequence had it shipped |
| --- | --- |
| Every media file wrote `analysis.wav` into the same directory | A project with two gameplay files: the second silently overwrote the first's analysis audio, and every transcript and audio event for it described the wrong recording |
| `ebur128` output was invisible at the configured `error` log level | Loudness silently measured nothing; `base_arguments` now takes a log-level override for measurement filters |
| Reaction spans merged speech with spikes | Every burst dissolved into one span covering the recording |
| The test suite ran the real Whisper provider | An unmarked `pytest` would download ~3 GB of weights |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| Fine-grained emotion classification | needs the transcript's words and the game situation; revisit with the LLM |
| Speaker diarization | not needed: §19's microphone-versus-gameplay split is the distinction that matters for the MVP |
| Explosion / gunshot classification | §27's job — one detector must not claim what several sources decide together |
| Music cue detection | §73 is local-music-only, so cues come from the timeline, not from analysis |

---

## Gate to Phase 4

Met: `pytest` green (580), `ruff` clean, both halves of the §126 step-07
acceptance criterion executed against real files, and the real speech provider
verified on the target machine.

Phase 4 begins at §126 steps 08–09: scene detection (§17) and the cascading
vision analysis (§15, §16). Its dependency is not yet installed —
`ollama pull qwen2.5vl:7b` — and its central risk is the one the cascade exists
to prevent: a two-hour recording sampled every three seconds is 2 400 frames,
and a local VLM at seconds per frame turns that into hours. Cheap detectors
nominate candidate regions first; only their keyframes reach the model, under a
hard ceiling per source hour.
