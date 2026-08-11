A person editing a gameplay video typed this:

    {text}

The editor already understands common commands by rule. This one was not
recognised, so your job is to say which single editing action it asks for — or
that it asks for none.

The video currently has {clip_count} clips, and is {duration} long. The clips
are numbered from 1.

The actions available:

- `delete_clip` — remove one clip, by its number in `clip_index`.
- `restore_clip` — bring back a clip that was removed, by number.
- `delete_at_timestamp` — remove whatever is playing at a moment in the
  finished video, in `timestamp_seconds`.
- `revert_version` — go back to an earlier saved edit, by `version`.

Rules:

- Return exactly one action, or `kind: "none"` when the text is not a command.
  A question about the video is not a command. A preference like "make it
  funnier" is not a command either — that is an instruction, handled elsewhere.
- Only use a clip number that exists. If they name a clip outside 1 to
  {clip_count}, return `kind: "none"` and explain in `reason`.
- Timestamps are positions in the *finished video*, not in the original
  recording.
- Changing the video's length is not one of these actions. Return
  `kind: "none"` if that is what they asked for.
- If the text is ambiguous between two actions, return `kind: "none"`. A wrong
  edit costs more than a clarifying question.
- `confidence` is how sure you are that this is what they asked for.
