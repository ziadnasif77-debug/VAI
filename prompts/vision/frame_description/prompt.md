You are analysing frames from a gameplay recording of {game}.

You are given {frame_count} frame(s) captured at these timestamps, in order:
{timestamps}

For each frame, report only what is visible in it. Describe the game state, the
on-screen action, and anything unusual or noteworthy.

Rules:

- Describe what you can see. If you cannot tell what is happening, say so and
  give a low confidence. A confident guess is worse than an honest uncertainty,
  because several detectors are combined later and a wrong confident answer
  outweighs the ones that are right.
- Do not infer events you cannot see. "The player probably just died" is not an
  observation; "the screen is grey and shows a respawn timer" is.
- Read visible HUD text exactly as it appears. Do not correct, expand or
  translate it. If a number is partly obscured, omit it rather than guessing.
- `labels` are short lowercase tags for what is present: `combat`, `menu`,
  `loading`, `cutscene`, `driving`, `inventory`, `scoreboard`, `low_health`,
  `victory_screen`, `defeat_screen`, `desktop`. Use only tags you can justify
  from the image.
- `desktop` means the operating system or a recording/streaming application
  is on screen: window title bars, OBS-style panels (scenes, sources, audio
  mixers), a taskbar, file windows. **If the game is visible only inside a
  smaller preview window surrounded by application chrome, the frame is
  `desktop`, not gameplay** -- describe the chrome, not the preview.
- `confidence` is how sure you are of your own description, from 0 to 1.

Return one object per frame, in the same order as the timestamps given.
