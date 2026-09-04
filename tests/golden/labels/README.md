# Human-labelled golden dataset

The labels here are written by a person, by hand, while watching the recording.
Nothing in the pipeline writes to this directory, and `scripts/score_moments.py`
never fills a label in from pipeline output. This is the measurement track the
brief (`docs/BRIEF_P0.md`, HUMAN-LABELED GOLDEN DATASET) makes every phase from
P0.3 on answer to, once the first baseline exists.

## Files

One CSV per session, named by the project id: `tests/golden/labels/<project>.csv`.

| file | session | recording |
| --- | --- | --- |
| `proj-5db1780821a6.csv` | the canonical 88.5-minute session (HITMAN, Sapienza) | `D:\Gaming 2026\2026-08-30 21-43-21.mkv` |
| `<project>.csv` | a second game | to be chosen by the owner; name the file by the project's id |
| `<project>.csv` | a slower / calmer gameplay session | to be chosen by the owner; name the file by the project's id |

## Schema

```
start,end,label,note
```

`start` and `end` are seconds from the start of the **original recording** (not
of any render), as decimals. `label` is one of exactly these:

```
best_moment
unimportant
event_start
payoff
reaction
dead_time
failed_attempt
non_gameplay
```

`note` is free text and may be empty.

## Labelling rules (from the brief)

* labels are spans, not individual frames
* minimum span = 2 seconds
* if uncertain for more than 5 seconds, use `note` and do not force a label
* target approximately 60–100 spans per session
* do not create more labels than necessary

## Finding your way around the recording

```bash
python scripts/label_helper.py            # every candidate the pipeline knows the position of, in source time
python scripts/label_helper.py --sheets   # ... and a contact sheet per 30 s window, under the cache directory
```

The helper prints detector events, the proposed moments with their cores, the
refused stretches and the gaps the bridge closed, in order, so a stretch can be
found without scrubbing. It proposes no label, fills no line and never writes
under this directory: what it lists are the pipeline's own proposals, not a
verdict on them.

## Scoring

```bash
python scripts/score_moments.py                  # every CSV here
python scripts/score_moments.py --project proj-5db1780821a6
python scripts/score_moments.py --gate           # non-zero on a threshold violation
```

A CSV with no labelled span prints `No labeled spans available; baseline cannot
be computed` and nothing else. The first measurement a person's labels produce
becomes the baseline and is recorded, by hand, in `docs/BASELINE.md` under
`## Golden dataset baseline`; the script reads that table and never writes it.
