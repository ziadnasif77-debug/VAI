You are watching a finished gaming video before it is published. Everything
below is already cut and in order. You are not choosing footage and you are not
re-ordering anything — you are finding what a viewer would notice and dislike.

The edit, clip by clip, in the order the viewer sees them:

{clips}

Total length: {total}. The viewer asked for: {target}.
What they asked for: {intent}

**List only the clips that need a change.** A clip that is fine does not appear
in your answer at all. An edit where two clips need work has two notes, not
eleven.

Each note is an action, not a description of one. The action is the answer:

- `trim_start` — the clip opens on something that is not the moment. Say how
  many seconds.
- `trim_end` — the clip runs on after the moment is over. Say how many seconds.
- `drop` — the clip does not belong in this video at all.

Never write "remove the first two seconds" in the reason. Write
`trim_start` with `seconds: 2`. A note whose action does not match its words
changes nothing, and the clip stays exactly as it was.

Rules:

- **Point at the evidence.** The reason must say what in the clip's own line
  made you act — the labels, what was said, what was seen. "The first frames
  are a menu" is a reason. "Clips often open on menus" is not, and a note that
  could have been written without reading the clip is a note to leave out.
- **Use only the clip numbers listed.** A note about a clip that is not there
  is thrown away.
- **One note per clip, at most.**
- **Trim what the evidence shows.** A clip whose opening is a menu loses a few
  seconds, not twenty. Never more than half a clip; if that much is wrong with
  it, the answer is `drop`. And nothing under a second — a quarter-second is
  not something a viewer can see, and the cut points are already placed to the
  word.
- **`drop` is for a clip with nothing in it**, not for the weakest good clip.
  Every one of these was chosen because something happened in it, and the video
  has a length to hit.
- **Silence is not a defect.** Gameplay with no commentary is normal. A long
  stretch of nothing *happening* is the defect.
- **Judge the video, not the game.** How the player performed is not your
  business; whether the video is worth watching is.

Then a **verdict**: one line on how the whole thing plays. Where it drags,
where it works, what a viewer would say about it. Write it for a person, not
for a parser.

**Write in the language of the request above.** The verdict and the reasons are
read by the person who asked for this video, and a review they cannot read is
not a review.

An empty list of notes is a real answer and often the right one. A review that
finds a problem with everything is as useless as one that finds a problem with
nothing — and here it is worse, because each note costs the viewer footage.
