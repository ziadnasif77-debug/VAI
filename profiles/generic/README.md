# Generic game profile

Used when the game is unknown or unsupported (SPEC §23). It declares no HUD
regions and no event rules, so detection falls back to vision + OCR + audio +
speech + temporal analysis over the whole frame.

A concrete profile (SPEC §22) adds HUD layout, kill-feed and score regions,
victory/defeat states and event rules. Per §111 the architecture is validated
with one real game before more profiles are written.
