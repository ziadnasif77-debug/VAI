-- Which taste made this edit (V2-P8).
--
-- A style has selected an effects profile since V1's Phase 8, and nothing has
-- ever recorded which one produced a given video. That was survivable while a
-- style only changed decoration. It stops being survivable the moment a style
-- changes cut lengths, audio and what counts as a defect -- and it is exactly
-- the join P9 needs, because "did the patient cut hold viewers longer" is not
-- a question you can answer from a config file that has since been edited.
--
-- One row per project: the question is what made the video that exists, not
-- everything this project has ever been cut with. `resolved` keeps the whole
-- resolved body, so a later reading does not depend on config/style.yaml
-- still saying what it said that day.

CREATE TABLE IF NOT EXISTS edit_styles (
    project_id  TEXT PRIMARY KEY REFERENCES projects (id) ON DELETE CASCADE,
    -- What the project asked for, including a name with no body.
    asked       TEXT NOT NULL,
    -- The entry that actually answered.
    style       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    digest      TEXT NOT NULL,
    resolved    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edit_styles_style
    ON edit_styles(style, version);
