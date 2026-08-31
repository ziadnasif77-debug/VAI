-- Every automatic change to the channel's taste, and the way back (V2-P10).
--
-- This is the only mechanism in the project permitted to change a decision
-- without a person asking. The table is shaped so that permission stays small.
--
--   * A delta is always relative to what `config/style.yaml` says, never to
--     the last delta. Cumulative adjustments creep: ten steps of a tenth would
--     leave the fence while each one looked reasonable. Base-relative means the
--     total displacement is bounded by the declared range, always.
--
--   * One active delta per (style, key). A new one supersedes the old rather
--     than stacking on it, and the superseded row stays for the record.
--
--   * `reason` and `evidence` are NOT NULL because a number that changed for
--     reasons nobody wrote down is indistinguishable from a bug. `evidence`
--     holds the comparison: the metric, both arms, their sizes, and the videos
--     each was measured from.
--
--   * `status` is how a change is undone. The base is never overwritten, so
--     reverting is marking a row rather than reconstructing a file.
--
-- Nothing has ever been written here, and nothing can be until fifteen videos
-- have been measured. The switch that would allow it is off in the file above.

CREATE TABLE IF NOT EXISTS tuning_deltas (
    id           TEXT PRIMARY KEY,
    style        TEXT NOT NULL,
    -- The dotted key exactly as `style.limits` names it, e.g. pacing.band_scale.
    key          TEXT NOT NULL,

    -- Signed, added to the file's value. Never to another delta.
    delta        REAL NOT NULL,
    -- What the file said when this was computed, so a later edit to the file
    -- is visible as a discrepancy rather than silently absorbed.
    base_value   REAL NOT NULL,

    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'reverted', 'superseded')),

    -- One sentence a person can read.
    reason       TEXT NOT NULL,
    -- The comparison it came from, as JSON.
    evidence     TEXT NOT NULL,
    -- How many measured videos stood behind it.
    videos       INTEGER NOT NULL,

    created_at   TEXT NOT NULL,
    ended_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tuning_deltas_active
    ON tuning_deltas (style, key, status);
