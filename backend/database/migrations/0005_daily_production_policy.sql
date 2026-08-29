-- The owner's daily production & publishing policy (2026-08-29).
--
-- production_ledger: one row per recording in the exclusive source, the
-- state machine that makes the policy idempotent -- a restart or crash can
-- never produce, publish or count the same file twice, because every
-- decision reads this table first.
--
-- daily_runs: one row per Europe/Oslo calendar day, inserted before the
-- cycle does anything. The primary key IS the mutex: a second 02:00 firing
-- on the same day conflicts and walks away.

CREATE TABLE production_ledger (
    source_path       TEXT PRIMARY KEY,  -- normalised absolute path
    signature         TEXT NOT NULL,     -- size:mtime at discovery
    state             TEXT NOT NULL CHECK (state IN
                        ('new', 'processing', 'edited', 'ready', 'published', 'failed')),
    project_id        TEXT,
    discovered_day    TEXT NOT NULL,     -- Oslo date
    produced_day      TEXT,              -- Oslo date it counted against the cap
    reels_produced    INTEGER NOT NULL DEFAULT 0,
    video_url         TEXT,
    scheduled_publish_utc TEXT,
    note              TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

CREATE INDEX idx_ledger_state ON production_ledger (state);
CREATE INDEX idx_ledger_produced_day ON production_ledger (produced_day);

CREATE TABLE daily_runs (
    day         TEXT PRIMARY KEY,        -- Oslo date
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    report      TEXT                     -- the policy's daily report, JSON
);
