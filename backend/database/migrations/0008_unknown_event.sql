-- `unexpected_event` becomes `unknown_event`.
--
-- The name claims something the detector never established. An "unexpected
-- event" says a surprise occurred; what the correlator actually means is that
-- several detectors agreed something was *there* and none of them could name
-- it. On the gate session that is 59% of all events -- the majority of what
-- the system "knows" was, under the old name, a claim about surprise.
--
-- The doctrine this settles is the owner's: when the system does not know what
-- happened, it must say so rather than invent an explanation. A rename is the
-- cheapest possible form of that, and the only one available for a label that
-- is already correct in behaviour and wrong in wording.

UPDATE game_events SET event_type = 'unknown_event' WHERE event_type = 'unexpected_event';

-- Moments inherit their type from the events beneath them; none of the moment
-- types were ever named after this event, so nothing else moves.
