# Phase 15 — Whether any of it is any good

SPEC §112, §117–§119; §126 step 21. The last phase, and the first that can say
whether the pipeline picks the moments a person would have picked.

Status: **complete**, with the first numbers this project has ever had about *quality* rather than behaviour.

---

## What the earlier phases could not measure

Every number this project has reported so far has been about *behaviour*:

| Phase | Claim | How it was checked |
| --- | --- | --- |
| 2 | An eight-hour source does not enter RAM | Peak memory moved 0.2 MB over a 150x longer input |
| 9 | The overlay composites without a seam | Zero pixels changed before the caption |
| 10 | The render is a real file | Decoded end to end with empty stderr |
| 11 | QA catches a broken video | Five deliberately broken renders, each caught by name |
| 14 | A profile buys something | 3 events the generic path cannot produce |

Not one of them says whether the *moments it chose* were the right moments,
because nothing here knew what the right moments were. That is the gap §117
exists to close, and it cannot be closed by writing more code.

---

## The dataset (§117)

    Real gameplay, manually annotated with important events, boring segments,
    best moments, reactions and game state. This is the benchmark.

One rule shapes the whole design: **the labels must not come from the system's
own output.** Reading the pipeline's moments and ticking the plausible ones
would produce excellent precision and mean nothing.

So `scripts/annotate.py` samples at a **fixed interval** and shows contact
sheets. It cannot sample where the pipeline found something, because it does
not ask. What it produces is a skeleton with the recording identified and no
spans; the spans are written by a person looking at frames.

`datasets/gta_v_2026-05-16.dataset.json` is the first one: ten minutes of a GTA
V session, 16 spans, every one carrying a note saying why it is there.

Two limits worth stating before any number is quoted against it:

* **It is a seed, not a benchmark.** One window, one recording, one game.
* **The session is cheat-enabled sandbox play**, not a mission run. "Boring"
  here includes several stretches of standing in a hospital car park toggling
  cheats, which is genuinely what the footage contains and is not what a
  typical recording looks like.

---

## The metrics (§118)

The arithmetic was never the risk. Two decisions were:

**Events and moments match differently.** An event is an instant and matches by
proximity -- asking a kill detected at 812.4s to *overlap* a label written as
812-815 would fail a correct detection. A moment is a stretch and matches on
overlap, because a clip sharing one second with a highlight has not found it.

**Predictions outside the watched window are discarded.** An annotator who
watched ten minutes of an hour has not found the other fifty minutes' events,
and scoring a prediction there as a false positive measures how long they
watched rather than how well the system works. This is the easiest way to make
a benchmark lie, and it lies in the flattering direction.

One prediction never matches two labels and one label is never found twice, so
five clips over one highlight are one true positive and four false ones --
which is right, because they are four clips a person would delete.

---

## User edits (§119)

    AI-selected moments accepted · deleted · AI-rejected moments restored ·
    average manual edits.

§118 scores the system against an opinion written down once. §119 scores it
against the opinion someone acts on every time they use it, and that is the
better signal for the reason it is harder to collect: nobody labels a golden
dataset for fun, and everybody deletes a clip they did not want.

Nothing new is recorded. The interaction layer already keeps what this needs,
because §42 asked for non-destructive editing and §78 gave the human the last
word: the timeline's `enabled` flag, `moments.user_state`, and one
`edit_versions` row per command. So `backend/quality/user_edits.py` reads, and
can be pointed at any project that has ever been edited.

The numbers only mean something once somebody has edited. A project nobody
touched reports 100% acceptance, which says nothing at all -- so `edited` is
carried beside the rates, and `aggregate()` excludes untouched projects rather
than letting them drive acceptance toward 1.0 in proportion to how many videos
were made and never opened.

---

## The first measurement

Ten minutes of real GTA V, analysed by the real pipeline (514 s end to end),
scored against the 16 labels written before any of it ran.

| | Precision | Recall | F1 |
| --- | --- | --- | --- |
| Events | 0.38 | **0.86** | 0.52 |
| Moments | 0.33 | **1.00** | 0.50 |

* 6 of 7 labelled events found; the miss is a large fire at 35:50.
* 3 of 3 labelled highlights found.
* 2 selected moments touch a stretch marked boring, **6 seconds in total** —
  the dead-time pass (§30) is doing its job.

**Recall is strong and precision is weak**, and the second half of that needs a
caveat before anyone acts on it: 16 labels over ten minutes is *sparse*. Many
of the 10 unlabelled events are probably real things nobody wrote down. So
0.38 is a **lower bound on precision**, not a measurement of it, and the number
that would improve it is more labelling rather than more code.

That shape is also what the architecture asks for. §32 scores and §39
optimises; the detectors' job is to nominate generously and let ranking cut.
Precision at the *selected clip* level is the number that matters to a viewer,
and it is the one to grow this dataset toward.

### The measurement tool was wrong first

The first run reported events at **0.31 / 0.71** and listed the second death as
missed. It had not been missed: the pipeline found it at **0.97 confidence** and
said so. §27 merges detectors that saw the same instant, so the death arrived
as a 26-second span — and the matcher compared *its* midpoint to the label,
which sat 8 seconds away.

Matching now succeeds in either direction: the prediction's midpoint near the
label, or the label's midpoint inside the prediction. Recall went 0.71 → 0.86
on the same data, because a correct detection stopped being scored as a miss.

Worth stating plainly: **the first thing this benchmark caught was itself.**
A measurement tool is code, it can be wrong, and the only way to find out is to
point it at something real.

### Two things it surfaced that are not scored

Neither is a §118 metric; both are the kind of thing running the whole pipeline
against a real recording turns up.

**Duration error is −225 s (−37.6%)**, and that one is my fault, not the
optimiser's: the source is a 600-second cut and the target was 600 seconds.
Asking for a ten-minute video out of ten minutes of footage means keeping
everything, which the dead-time and repetition passes will never do. The number
is real and the conclusion it invites is wrong, which is exactly why §118's
metrics need the setup recorded beside them.

**QA blocked the render**: 5.3 s of black video across 3 runs. The cause is not
established. Two clips in the edit overlap heavily — `31.4-56.4` and
`19.6-56.4`, ending at the same instant — which §31's repetition pass should
have collapsed and which may or may not be related. Recorded rather than
explained away.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| The event matcher compared midpoints only | A correct detection at 0.97 confidence scored as a miss, and recall under-reported by 15 points |
| `scripts/serve.py` exits silently when its port is taken, **taking the worker with it** | The log reads "worker started … API started … worker stopped", which looks like a completed run. It cost one wasted analysis before it was spotted |

---

## Not built, and why

| Deferred | Why |
| --- | --- |
| A larger dataset | The machinery is what a phase can deliver; labels are hours of watching, and a second recording adds nothing until this one's misses are understood |
| Reaction and game-state scoring | Both are labelled in the dataset and neither has a scorer yet. `SpanKind.REACTION` and `GAME_STATE` exist so the labels do not have to be rewritten when they do |
| Type-aware event matching | Matching is currently time-only. The labels carry types and the predictions carry types, and comparing them is the obvious next metric — but on 7 labels it would report noise |
| §119 in the interface | The numbers compute; nothing displays them. A quality panel wants a design conversation, not a table dumped into the export screen |
