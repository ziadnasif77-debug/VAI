# Outcomes

What the audience did with a finished video, stored against the edit that
produced it. This is the first record in the system that is not the machine's
own decision written down.

## What it is, and what it is not

**Outcome correlation.** Two records placed side by side — a retention curve and
the timeline that was rendered — with the join made explicit and checkable.

It is **not** retention prediction, and it is **not** learning. Nothing here says
a future edit will hold viewers better, and nothing adjusts a decision because
of a number. That is P10's job, inside the bounds `config/style.yaml` declares,
and only once there is enough data to argue with.

## Getting it

```bash
python scripts/fetch_outcomes.py --dry-run
```

Explicit on purpose: no ambient polling and no automatic stage. This reads the
owner's own analytics with the owner's own credentials, and a network call made
on someone's behalf is asked for rather than assumed — the same rule §51 applies
to publishing, applied to reading.

| Flag | Meaning |
|---|---|
| `--days N` | how far back the window reaches (default 28) |
| `--video ID` | one video instead of all of them |
| `--dry-run` | list what would be fetched and ask YouTube for nothing |

Exit `2` means nothing was fetched and nothing was written, with the reason
printed.

## The two things it needs

**1. The analytics scope — and an OAuth client that can be granted it.**

A refresh token keeps the scopes it was issued with, so widening the request in
code does not widen a grant already on disk. The token store records what Google
actually granted, and the fetcher checks it before making a request rather than
discovering a 403 halfway through a report:

```bash
python scripts/youtube_auth.py            # what the stored grant covers
python scripts/youtube_auth.py --connect  # sign in again
```

**The client type decides what is even possible**, which cost this project an
afternoon to learn. Measured against Google's own endpoints:

| | |
|---|---|
| device flow + `youtube` | accepted |
| device flow + `yt-analytics.readonly` | `400 invalid_scope` |
| loopback flow, "TV and Limited Input" client | blocked |
| loopback flow, "Desktop app" client | works |

Google's device flow — a code typed on another screen — supports a restricted
scope list that excludes YouTube Analytics, and it requires a **TV and Limited
Input** client. That same client type is one Google blocks the loopback redirect
for. So with that client the analytics scope cannot be obtained *by any flow*.

`config/publishing.yaml` therefore names a **Desktop app** client, and
`scripts/youtube_auth.py` signs in through the loopback flow. If you are setting
this up fresh, create the client as *Desktop app* at
[console.cloud.google.com](https://console.cloud.google.com/apis/credentials) —
not TV, whatever the device flow's convenience suggests.

**And enable the API.** Separate from the sign-in, and easy to miss because a
perfectly good authorisation still fails without it:

```
https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com
```

The refusal reads `accessNotConfigured`, and the fetcher now says so rather than
blaming the scope — which it did, sending a reader back to a sign-in they had
just completed correctly.

**2. A video this system published.** Attribution comes from the PUBLISH job's
own result, which carries the video id YouTube assigned. A video on the channel
that this system did not publish has no edit behind it, so an outcome for it
could not be traced to any decision — and a number nobody can attribute is not
evidence. Those are refused rather than stored against a project nobody can
name.

> Note for anyone reading the schema: the `publications` table exists in the
> first migration and **nothing has ever written a row to it**. The publish
> worker says so in its own docstring, having decided that the job history *is*
> the publication history. Anything looking for published videos reads
> `analysis_jobs`.

## What is stored

`video_outcomes` — one row per video per window. Re-fetching the same window
updates the row, because two reads of one window are one fact measured twice;
two different windows stay two rows, because they are two different facts. The
whole API response is kept beside the named columns: a metric this schema does
not name yet is still evidence, and a window that has aged out cannot be
re-fetched.

`retention_points` — the curve, as fractions of the video against the share of
viewers still watching. Ratios, not seconds: a ratio is what was measured, and
seconds are an interpretation of it that needs the render's length.

**An absence is never written as a zero.** A video nobody has watched reports no
rows, and storing `views = 0` would make an unwatched video and an unmeasured
one indistinguishable. The columns stay null and every surface says "not
measured".

## Reading the curve against the edit

`backend/analytics/projection.py` turns a dip at 38% of the video into "the
third shot, cut at 2.1s in a calm band, under the patient style". It refuses
rather than guesses in two cases:

- **No rendered length.** A ratio is not a time until something says how long
  the video was.
- **The edit changed after the render.** Reading the curve against the current
  timeline would name shots the audience never saw, which is worse than saying
  nothing.

A dip is a measurement. What was on screen is a fact. *Why* people left is
neither, and this layer does not claim to know it.

## State on this machine

At the time of writing, with the sign-in and the API both in place, four
published videos were measured:

```
4 of 4 video(s) measured; 4 stored

  4-FvLE130Ck    0 views        ← a number: published, watched by nobody
  Z6FHb42UhqE    not measured   ← still private; private videos report nothing
  bN4BBCMc_0A    not measured
  NESJ1xeP60A    not measured
```

Those are two different states and the store keeps them apart. Writing `0` for
all four would have made an unwatched video and an unmeasured one
indistinguishable, which is the distinction this whole layer is built around.

Zero retention points, because a curve needs an audience. That is why nothing
in this project claims to learn from outcome data yet: the pipe is connected
and almost nothing has come through it.
