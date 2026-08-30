-- A rollback happens once (V2-P7).
--
-- The second pass re-queues the render after it restores the previous edit,
-- because the file on disk is still the corrected one and leaving it there
-- would defeat the lock. That re-queued render brings CRITIC2 back, and the
-- snapshot is still present, so it enters the second pass again -- and if the
-- restored edit scores below the stored figure for any reason at all, encoder
-- variance included, it restores and re-queues once more. Forever.
--
-- This column is where that loop ends: a snapshot that has already been
-- restored refuses to restore again.

ALTER TABLE critic2_snapshots ADD COLUMN restored_at TEXT;
