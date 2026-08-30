-- The Semantic Timeline becomes a stored artefact rather than a JSON file
-- beside the analysis.
--
-- It was cached in `projects/<id>/analysis/semantic/<media>.json`, keyed by a
-- signature that hashed the *row counts* of its inputs. Re-scoring an event's
-- importance without changing how many events there were returned the stale
-- timeline in silence -- and since every pacing decision is graded from these
-- lanes, a stale timeline is a whole edit cut to yesterday's heat.
--
-- Keyed by media, not by project: the lanes describe a recording, and two
-- projects over the same footage should read the same session.

CREATE TABLE IF NOT EXISTS semantic_timelines (
    media_id         TEXT PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    signature        TEXT NOT NULL,
    builder_version  TEXT NOT NULL,
    hz               INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    -- {lane_name: [value, ...]} at `hz` bins per second, values 0..1.
    lanes            TEXT NOT NULL,
    built_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_semantic_timelines_signature
    ON semantic_timelines(signature);
