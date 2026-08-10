"""Media engine: probing, proxy, audio and frame extraction (Phase 2, SPEC §99).

FFmpeg and FFprobe are invoked here and nowhere else. Command lines are built
as explicit argument lists by trusted application code (§85).

Not yet implemented -- Phase 1 registers media; Phase 2 opens it.
"""
