-- Every moment carries the spans its granters authorised (P0.3).
--
-- The AuthorizedSpan contract (docs/BRIEF_P0.md, PHASE B) starts at the
-- moment: the events' own span is the first grant, the expanded context the
-- second, and every widening after them is a new span from a listed granter.
-- The story stage plans from stored moments, so the chain has to survive the
-- table -- Moment.metadata is not persisted whole, only named columns are.
--
-- A JSON list of {media_id, start, end, granted_by, reason}. Empty for every
-- moment stored before this migration, and an empty chain is refused where
-- authorization is required: a grant nobody issued is not a grant, and those
-- projects re-run MOMENTS and STORY rather than being backfilled.

ALTER TABLE moments ADD COLUMN authorized TEXT NOT NULL DEFAULT '[]';
