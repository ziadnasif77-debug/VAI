-- Effects gain the vocabulary a sentence needs (V2-P4).
--
-- Until now an effect knew its type, its time and its strength, and nothing
-- about any other effect. docs/DIRECTION.md has asked since day one for
-- SETUP -> BUILDUP -> TENSION -> PAYOFF -> REACTION, and the reason the
-- planner shipped a flat event->effect map instead was not effort: there was
-- no way to say that these four rows are one gesture, that this one is the
-- reaction to that one, or that this one sits 1.6 seconds BEFORE the beat.
--
-- Four columns, all optional. An effect with none of them is a decoration,
-- which is what every effect in this table was before today.

ALTER TABLE timeline_effects ADD COLUMN composition_id TEXT;
ALTER TABLE timeline_effects ADD COLUMN group_role TEXT;
ALTER TABLE timeline_effects ADD COLUMN anchor_seconds REAL;
ALTER TABLE timeline_effects ADD COLUMN offset_seconds REAL;

CREATE INDEX IF NOT EXISTS idx_timeline_effects_composition
    ON timeline_effects(project_id, composition_id);
