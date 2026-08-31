-- What happened after the video was published (V2-P9).
--
-- The first table in this schema that holds a fact this system did not decide.
-- Everything else records what the machine chose; these record what an
-- audience did with it, which is the only evidence that any of those choices
-- were good ones.
--
-- Two rules are built into the shape rather than left to the code:
--
--   * An outcome belongs to a project. A video this system did not publish
--     has no edit behind it, and is refused rather than stored against a
--     project nobody can name -- "which edit produced this retention" would
--     have no answer, and an outcome nobody can attribute is a number rather
--     than evidence.
--
--     The attribution comes from the PUBLISH job's own result, which carries
--     the video id YouTube assigned. Not from the `publications` table: it has
--     existed since the first schema and nothing has ever written a row to it,
--     the publish worker having decided that the job history *is* the
--     publication history. `publish_job_id` therefore points at a job.
--
--   * A fetch is a measurement with a window. `(video_id, start_date,
--     end_date)` is unique, so re-fetching the same window updates the row
--     instead of adding a second opinion, and two windows of the same video
--     stay two rows because they are two different facts.
--
-- Nothing here is learned from, and nothing predicts. P10 is the phase allowed
-- to change a decision, and only inside the bounds P8 declared.

CREATE TABLE IF NOT EXISTS video_outcomes (
    id                            TEXT PRIMARY KEY,
    project_id                    TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    publish_job_id                TEXT REFERENCES analysis_jobs (id) ON DELETE SET NULL,
    video_id                      TEXT NOT NULL,

    -- The window the numbers describe, in the channel's own reporting dates.
    start_date                    TEXT NOT NULL,
    end_date                      TEXT NOT NULL,
    fetched_at                    TEXT NOT NULL,

    views                         INTEGER,
    estimated_minutes_watched     REAL,
    average_view_duration_seconds REAL,
    average_view_percentage       REAL,
    likes                         INTEGER,
    comments                      INTEGER,
    shares                        INTEGER,
    subscribers_gained            INTEGER,

    -- The response as it arrived. A metric this schema does not name yet is
    -- still evidence, and re-fetching a window that has since aged out is not
    -- always possible.
    raw                           TEXT NOT NULL DEFAULT '{}',

    UNIQUE (video_id, start_date, end_date)
);

CREATE INDEX IF NOT EXISTS idx_video_outcomes_project
    ON video_outcomes (project_id, end_date DESC);

-- The retention curve, one row per sampled point.
--
-- `elapsed_ratio` is a fraction of the video's length, which is what the API
-- reports; turning it into seconds needs the render's duration and is done by
-- the projector, not stored here -- a ratio is what was measured and seconds
-- are an interpretation of it.
CREATE TABLE IF NOT EXISTS retention_points (
    outcome_id           TEXT NOT NULL REFERENCES video_outcomes (id) ON DELETE CASCADE,
    elapsed_ratio        REAL NOT NULL,
    audience_watch_ratio REAL NOT NULL,
    -- Present only once a video has enough traffic for YouTube to compare it
    -- with others of similar length. Null is normal and means "not yet".
    relative_performance REAL,

    PRIMARY KEY (outcome_id, elapsed_ratio),
    CHECK (elapsed_ratio >= 0.0 AND elapsed_ratio <= 1.0)
);
