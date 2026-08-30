-- What the edit looked like before the critic corrected it (V2-P7).
--
-- CRITIC2 is the only stage permitted to change an edit without a person
-- asking, and its third lock is that a correction which lowers the quality
-- score is reverted. That lock needs somewhere to revert *to*.
--
-- The first version of the worker read ``edit_versions`` for this, which only
-- the interactive editor ever writes, found nothing, and reported a rollback
-- that had not happened. This table is written before the first correction is
-- applied and read by the second pass, and it holds effects as well as clips
-- because the critic deletes both.
--
-- One row per project per revision: the loop is one cycle by construction, so
-- there is never a second.

CREATE TABLE IF NOT EXISTS critic2_snapshots (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    revision        INTEGER NOT NULL,
    quality_before  REAL NOT NULL,
    clips           TEXT NOT NULL,
    effects         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_critic2_snapshots_revision
    ON critic2_snapshots(project_id, revision);
