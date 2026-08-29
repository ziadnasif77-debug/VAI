-- The owner's order (2026-08-29): the Reels publish too. One JSON column on
-- the ledger records what was uploaded and for when -- the idempotence truth
-- for reel uploads, exactly as the row itself is for the long video.

ALTER TABLE production_ledger ADD COLUMN reels TEXT;
