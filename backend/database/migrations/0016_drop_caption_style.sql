-- Remove `captions.style`, which never held anything (V2-P2.5).
--
-- The column was written, stored, and read back on every caption this system
-- has ever produced -- and every one of the 312 rows on the machine this was
-- measured on held the empty object. It could not have held anything else:
-- nothing ever wrote to it, and `_describe_caption` never passed it to the
-- renderer, so a value placed there would have been dropped before it reached
-- the picture.
--
-- A field that is written empty, stored empty, loaded empty and then discarded
-- is not a capability waiting to be used. It is a promise the schema makes and
-- the code cannot keep, and the honest move is to delete it rather than leave
-- the next reader to discover the round-trip goes nowhere.
--
-- Per-style caption appearance, which is what this column looked like it was
-- for, is decided in `config/style.yaml` and resolved into `CaptionsConfig`
-- before the render. That is a taste, so it belongs with the other tastes and
-- not on a per-row column.

ALTER TABLE captions DROP COLUMN style;
