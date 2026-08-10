# The first real recording

Everything before this ran against fixtures: six-second colour-bar clips and
numerically generated tones. They are the right thing for a suite that has to
finish in fifteen minutes, and they cannot tell you whether the product works.

On 2026-08-11 the pipeline ran end to end on a real gameplay recording for the
first time — `D:\Gaming 2026\2026-05-16 00-10-49.mkv`, 21 minutes of 1080p60
with two audio tracks — and produced a 10.4-minute edit. It also found two
defects that 919 passing tests had not.

---

## What happened

| Stage | Time | Result |
| --- | --- | --- |
| probe | 0.1 s | 1272 s, 1920×1080@60, 2 audio tracks |
| proxy | 181 s | 720p proxy |
| audio | 3 s | 2 analysis streams |
| frames | 15 s | 424 base frames at 3 s intervals |
| transcript | 308 s | **0 segments** |
| audio_events | 8 s | 465 events, 15 reactions |
| scenes | 232 s | 176 scenes |
| vision | 171 s | 85 observations, 22 of 41 regions, 95 % coverage |
| ocr | 158 s | 63 detections over 85 frames |
| game_events | 0 s | 27 events, all multi-source |
| moments | 0 s | 18 moments |
| story | 0 s | 16 clips, 624 s |
| edl | 0 s | 16 clips, 10.4 min, valid |

**17.9 minutes to analyse 21 minutes of video.** Roughly real time, on an
RTX 3070, with nothing optimised.

The §15 cascade behaved exactly as specified: 41 candidate regions against a
budget of 85 frames — `max_frames_per_source_hour: 240` scaled to a 21-minute
source — 22 regions analysed, 19 dropped, and **95 % of candidate time still
covered**, because the allocator drops short regions before long ones. The
"frame budget could not cover every candidate region" warning is the system
reporting a bound it was told to respect, not a failure.

---

## Defect 1: the transcript read the wrong track

`TranscriptWorker` transcribed the **primary** stream. On a two-track
recording the primary is the game audio; §19 exists precisely because the
player's voice is on the other one. `_streams_for` already labelled track 2 as
the microphone, and `probe` already reported
`has_separate_microphone_track: True` — the pipeline knew, and read the game
track anyway.

Fixed: `_speech_stream` prefers the microphone, falls back to the primary when
there is none, and falls back **also** when the microphone track is silent — a
second track that was armed but never connected is a real recording shape, and
transcribing silence would replace a usable transcript with nothing. Measuring
the level costs a scan of a 16 kHz mono file against several minutes of a
transcription that would have produced nothing.

The job result now records `track_index`, `track_role` and `track_reason`,
because an empty transcript is otherwise ambiguous: nobody spoke, or the wrong
track was listened to?

### Where the first diagnosis was wrong

The empty transcript looked like proof of this bug, since the audio analysis
had found 255 speech events on the same recording. It was not. Fixing the
track selection changed the transcript from 0 segments to 0 segments — and
that is the correct answer, as Defect 2 explains. The bug was real and is worth
fixing; it was not the cause of what was observed.

---

## Defect 2: both audio tracks are the same audio

The two tracks in that recording are **byte-identical** — and so are the two
tracks in every other recording checked. The capture tool was told to record
two tracks, and wrote the same mix to both because the second was never routed
to a separate source.

That is not harmless. The pipeline extracted the same audio twice, analysed it
twice, and labelled the copy "microphone" — so every reaction §20 detected on
it was attributed to a player who was not the one making the sound.

Fixed: `extract_all_tracks` drops a stream whose content hash matches one
already kept. Compared after extraction rather than before, because two source
tracks can be encoded differently and still decode to identical samples, and it
is the samples every detector reads.

### Why the transcript really was empty

There is no speech in the recording. Running Whisper directly on it produces
only hallucination — "Thanks for watching!" repeated across four segments, with
the language guessed as Norwegian Nynorsk at 0.72 confidence — which is the
well-documented behaviour of the model on non-speech audio. The pipeline's own
path, with `vad_filter: true`, returned nothing, which is right.

**This is worth acting on for the user, not just for the code.** With the
microphone on its own track, §19 and §20 have something to work with: player
reactions, laughter and shouting are among the strongest signals §32 scores,
and right now the system is running without them.

---

## What this run proves, and what it does not

**Proved.** The chain from a real file to a real EDL works: 21 minutes of
1080p60 became 16 clips totalling 10.4 minutes, inside the requested target,
contiguous, validating, with every clip referencing source timestamps in the
original file. §15's budget held. Memory was never a problem.

**Not proved.** Nothing was rendered — RENDER is Phase 10, so no one has
watched this edit. Whether the moments chosen are the *right* moments is a
judgement no test makes; the pacing report says so itself, and it is worth
reading:

> 8 clips of the same type run consecutively (limit 2) · 1 clip longer than
> the maximum · intensity does not build across the video

The scoring found 18 moments of exactly **two types** — 5 `funny`, 13
`surprise` — because the generic profile (§23) can detect *that* something
happened without knowing *what*. §33's variety machinery has nothing to work
with when everything is one of two kinds. That is Phase 14's problem, and this
run is the argument for it.

One clip runs 2 minutes 14 seconds, because §29's context expansion snapped to
a scene boundary and the longest detected scene is 166 seconds. Worth revisiting
when there is a rendered video to watch.
