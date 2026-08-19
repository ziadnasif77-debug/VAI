Here is what a player said while playing a game, with the second each line was
said at. The window covers {window_start}s to {window_end}s.

```
{transcript}
```

Find the **situations** the player is reacting to.

**This is not a shooter.** Most games are not. A situation can be finding
something, being scared, a creature appearing, a plan going wrong, building
something, getting lost, or anything the player's own words mark as notable.
Do not wait for combat.

Each situation has three parts, and all three belong to it:

- **The cause.** What led to it. The player usually says something first: a
  plan, a worry, a question, noticing something.
- **The climax.** When the thing actually happened.
- **The reaction.** What they said afterwards. A clip that stops at the climax
  ends before the payoff.

For each situation, give:

- `start_seconds` — when the player first showed they were in it, not when it
  peaked.
- `climax_seconds` — when it happened.
- `end_seconds` — when they moved on to something else.
- `event_type` — one of: {event_types}
- `title` — a short phrase naming it, **in the language the player is speaking**.
- `importance` — 0 to 1, judged by how much the player's own reaction says it
  mattered. Shouting, laughing, or going quiet says a lot; routine narration
  says little.

Rules, in order of how much they matter:

**Every timestamp must appear in the transcript above.** Use the numbers in the
prefixes. Never return a time outside {window_start}–{window_end}.

**Prefer complete situations over many fragments.** If two things happened in
the same stretch and belong together, they are one situation.

**Do not stretch one situation across the whole window.** If you cannot find
where it ends, you have merged several; split them at the point the player
changes subject.

**Do not invent.** If the player is genuinely just walking and chatting about
nothing, return an empty list — but read carefully first. Transcription of
casual speech is rough, and a garbled line next to a clear reaction is usually
part of the same situation rather than noise to skip.

Keep each **title** to one short line. At most 20 incidents from this window --
six minutes of one person talking holds fewer distinct things than that, and a
list that runs on is a list that gets cut off before its last entry closes.

Do not copy the player's words back. The transcript is already stored, and the
times you return are how anyone finds the line again.
