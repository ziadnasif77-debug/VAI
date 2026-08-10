# Remotion overlay project

Renders captions and motion graphics onto a **transparent** canvas. FFmpeg cuts,
concatenates and encodes the gameplay; this project only produces the overlay
layer that FFmpeg then composites (see `config/rendering.yaml`).

Remotion rasterises every frame through Chromium, which is the right cost for
graphics and the wrong cost for stitching twenty minutes of gameplay.

Remotion performs no selection: it receives a deterministic composition
description produced from the timeline (SPEC §64). Implemented in Phase 9.
