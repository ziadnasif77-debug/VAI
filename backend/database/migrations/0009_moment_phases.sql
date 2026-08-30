-- What each moment is doing, second by second (V2-P2).
--
-- A moment has always been a story fragment -- setup, the thing happening, the
-- reaction -- stored as one span with a type, so every stage downstream had to
-- guess where inside it the interesting part was. The effects planner placed
-- by a fixed fraction of the length; context expansion added a constant
-- pre-roll chosen by type; the Critic could only trim from the ends.
--
-- The phases are measured from the session's own lanes, not invented over the
-- detectors' labels, and each carries the confidence its evidence supports. A
-- moment whose lanes are flat stores one `unknown` phase, which is a different
-- statement from "this was calm".
--
-- JSON in one column rather than a table: they are read whole, always with
-- their moment, and never queried by value.

ALTER TABLE moments ADD COLUMN phases TEXT;
