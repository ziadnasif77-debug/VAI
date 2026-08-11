A person editing a gameplay video typed this instruction:

    {text}

The editor already understands common phrasings by rule. This one was not
recognised, so your job is to read it and say which editing preferences it
changes — nothing more.

The current preferences are:

{intent}

Return only the dimensions this instruction actually changes. Leave everything
else out; an omitted field means "leave it alone", and a field you fill in
needlessly overwrites a choice the person made earlier.

The dimensions you may set, and what they mean:

- `pacing` — how quickly the video moves: `slow`, `medium`, `fast`, `very_fast`.
- `dead_time_policy` — how aggressively quiet stretches are cut:
  `keep`, `balanced`, `aggressive`.
- `context_preservation` — how much lead-up each moment keeps:
  `none`, `low`, `medium`, `high`.
- `effects` — how much visual decoration: `none`, `minimal`, `moderate`, `heavy`.
- `captions` — `none`, `important_only`, `standard`, `full`.
- `music` — `none`, `subtle`, `prominent`.
- `variety` — how much the video mixes kinds of moment:
  `none`, `low`, `medium`, `high`.
- `mode` — the shape of the video: `story`, `best_moments`, `compilation`.
- `priority_moment_types`, `avoid_moment_types` — kinds of moment to favour or
  avoid, as `{{"add": [...]}}` or `{{"remove": [...]}}`. Valid kinds:
  {moment_types}

Rules:

- If the instruction asks for something none of these dimensions expresses,
  return an empty object and say why in `unsupported`. Inventing the nearest
  available preference is worse than saying you cannot do it, because the
  person will believe their instruction was followed.
- You cannot set the video's length. If that is what they asked for, return an
  empty object and say so in `unsupported`; the editor reads durations by rule
  and the interface has a picker for them.
- `confidence` is how sure you are that this reading is what they meant.
