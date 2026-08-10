-- Vision observations (SPEC sections 15, 26, 45, 49).
--
-- What the vision model reported about one candidate keyframe. Separate from
-- `frames` because the two answer different questions and change at different
-- times: `frames` is the sampling record -- which instants exist on disk --
-- while this is one model's reading of some of them. Re-running VISION with a
-- new model replaces every row here and touches none there.
--
-- Every row carries its model and prompt version (§49). An observation whose
-- provenance is unknown cannot be invalidated when the model changes, and
-- §48's cache identity depends on being able to.

CREATE TABLE vision_observations (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    media_id        TEXT NOT NULL REFERENCES media (id) ON DELETE CASCADE,

    -- Position in the source, in seconds. The keyframe this describes.
    timestamp       REAL NOT NULL,

    description     TEXT NOT NULL,
    -- Short tags the model justified from the image, JSON array.
    labels          TEXT NOT NULL DEFAULT '[]',
    confidence      REAL NOT NULL DEFAULT 0.0,
    -- HUD text read verbatim, JSON object. Empty when nothing was legible.
    hud             TEXT NOT NULL DEFAULT '{}',

    -- Which candidate region this keyframe belonged to, and why that region
    -- was nominated (§16). Kept so a surprising observation can be traced back
    -- to the detectors that asked for it.
    region_start    REAL,
    region_end      REAL,
    -- JSON array of trigger names: audio_spike, scene_change, ...
    sources         TEXT NOT NULL DEFAULT '[]',

    -- §49 provenance.
    model_name      TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    prompt_id       TEXT,
    prompt_version  INTEGER,

    created_at      TEXT NOT NULL,

    CHECK (timestamp >= 0),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    -- One observation per keyframe per media. A second VISION run replaces
    -- rather than duplicates.
    UNIQUE (media_id, timestamp)
);

CREATE INDEX idx_vision_media_time ON vision_observations (media_id, timestamp);
CREATE INDEX idx_vision_project ON vision_observations (project_id);
