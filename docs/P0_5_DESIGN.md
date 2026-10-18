# P0.5 — attempts: design report (measured 2026-10-18, nothing built)

Authorised as a report only (`docs/PLAN.md`, `AUTHORIZED: P0.5 — 2026-10-18`).
No code was written for it. Every number below comes from the benchmark
(`proj-5db1780821a6`, HITMAN Sapienza, 88.5 min) or from two Grounded sessions
on this machine, and every one was measured from what the pipeline already
stores. The owner's question, from PLAN.md: **on what evidence are two attempts
the *same* attempt, how sure can that evidence be, and where will it be wrong.**

## 1. What the benchmark actually contains

The owner named one case (the reload at 20:45). Measuring found **two**:

| | attempt that failed | the boundary | attempt that replaced it |
| --- | --- | --- | --- |
| **R1** | 0:12–3:44: the van at 1:12, a fight from 3:29 | `MISSION FAILED` / `AGENT DOWN` read at 3:51 (231.5 s); the mission's own opening "Welcome to Sapienza, 47." at 4:07 | from 4:07: the same white van at 4:21 (first seen at 1:12), a fight at 4:21 |
| **R2** (named) | Caruso killed at 16:42 (vision: "eliminated Silvio Caruso", OCR "Target down -- Next up, Francesca De Santis" at 16:47); the player then dies, `defeat` 19:31 | `MISSION FAILED` / `AGENT DOWN` at 19:43, "Are you sure you want to restart the mission?" at 19:49, `LOAD GAME` save slots 20:10–20:14, loading, a second pause-menu/load 20:41–20:49 | from 20:49: the same garden, the same kill; OCR "Target down -- Next up, Francesca De Santis" again at 21:00 and 21:03 |

**How the P0.6 gate edit (`rnd-fc5ad2cdfaf9`) shows them today:**

| | shown of the failed attempt | shown of the failure itself | shown of the replacing attempt |
| --- | --- | --- | --- |
| R1 | 61 s (source 2:17–3:21), video 0:00–1:00 | **nothing**: the edit cuts at 3:21, before the fight at 3:29 and the `MISSION FAILED` at 3:51 | 152 s, video 1:00–3:36 |
| R2 | 49 s (source 16:00–16:49, the first kill), video 6:27–7:04 | **nothing**: the death at 19:31 is not in the edit | **0 s**: the second kill -- the one that stood -- is not in the edit |

Both are the owner's rule broken in the same direction: the failed attempt is
shown without its failure, so the restart reads as an unexplained jump back,
and in R2 the payoff is the attempt the game erased.

## 2. The evidence, one source at a time

### 2.1 The boundary: `ContentState` (P0.1), the strongest and cheapest

The content layer already names the two states an attempt needs
(`MISSION_FAILED` "ends an attempt", `RESTART` "begins the next one",
`backend/gaming/content.py`). On the benchmark, the excluded spans whose kinds
include `mission_failed`, `restart` or `loading`:

| span (source) | kinds | a real attempt boundary? |
| --- | --- | --- |
| 3:43–3:57 | loading / menu / mission_failed | **yes** (R1) |
| 19:36–20:14, 20:24–20:31, 20:34, 20:41–20:49 | game_intro / loading / menu / mission_failed / pause / restart | **yes** (R2), one boundary read as four spans |
| 39:11–39:23 | menu / pause / restart | **no**: HITMAN's pause menu *lists* RESTART MISSION; play resumes |
| 39:58–40:27 | menu / pause / restart | **no**, same reason |
| 45:22, 45:33–45:51 | loading, pause | **no** rewind follows (see 2.2) |
| 1:14:23–1:14:29 | loading / menu | **no** rewind follows |
| 1:27:13–1:28:24 | cutscene / loading | the mission's end, not a retry |

So **`restart` read off a frame is not a restart**: on this game the word is on
every pause menu. `mission_failed` is far better -- 2 of 2 true on the
benchmark, with no false reading -- but one game, one session. Across every
project on this machine:

| failure text in stored OCR | where | what it is |
| --- | --- | --- |
| `MISSION FAILED` / `AGENT DOWN` | benchmark only (3 frames) | a real failure screen |
| `WASTED` | 3 GTA V sessions, 1 frame each | a death; GTA respawns, nothing is replayed |
| `YOU DIED` | 1 Grounded session, 2 frames | a death; Grounded respawns, nothing is replayed |
| `RESPAWN` | 10 Grounded sessions, up to 23 frames each | **not a failure**: the bed's "Set respawn point" |

A failure screen is the end of a *death*; it is the end of an *attempt* only in
a game that rewinds. GTA and Grounded do not, and on them the same reading
would cut a story at every death.

### 2.2 The same place again: the picture rewinds

Not proposed in PLAN.md and measured here because it is game-independent: after
a reload the player walks the same ground again, so the frames after the
boundary each match a frame a fixed time earlier. Measured over the stored base
frames (one every 3 s, 64×36 grey, normalised, a run of 8 frames compared with
the run L seconds earlier, menus and frozen frames left out):

* Per-frame similarity is useless alone: the R2 run scores 0.41–0.50, *below*
  the session's median best match (0.44), because HITMAN revisits the same
  places all session.
* The **lag** is the signal. After R2 the best lag stays at 255–261 s for 18
  consecutive windows (54 s): 21:09 matches 16:54, 21:21 matches 17:03, 21:36
  matches 17:15. It is the **longest stable-lag run in the session**; the next
  is 13.
* After R1 the lag holds at 180 s for 10 windows (4:12 matches 1:12).
* A stable run is not by itself a rewind: the benchmark has 37 runs of six
  windows or more, and two Grounded sessions with no reload in them reach 13.

| rule (benchmark) | true boundaries found | false ones |
| --- | ---: | ---: |
| `restart` on a frame | 2 of 2 | 2 (the pause menus) |
| `mission_failed` on a frame | 2 of 2 | 0 |
| stable-lag run ≥ 15 windows | 1 of 2 (R1 is 10) | 0 here, 0 on the two Grounded sessions |
| `mission_failed` **and** a stable-lag run (≥ 6) starting within 30 s of the boundary's end | **2 of 2** | **0** |

### 2.3 The same goal: text the game prints

"Target down -- Next up, Francesca De Santis" is read at 16:47 and again at
21:00/21:03: the same objective completed twice is as direct as evidence gets
that the second attempt redid the first. It exists on HITMAN only, would belong
in a game profile's rules, and appears 3 times in the session. The objective
list itself (`ELIMINATE …`) is **not** usable as state: it was read with
Caruso's line missing before his first kill, so what the OCR leaves out cannot
be read as done.

### 2.4 The same target in the vision reads

"Eliminated Silvio Caruso" is in the vision reads at 16:42, 16:47 and 16:52 and
in none after the reload, where the same kill is described as "crouching
behind a stone pillar… holding a sniper rifle". The descriptions are written
per frame, are generic about place ("lush garden", "dense foliage" on both
sides and all session), and name a target only when the HUD prints it.
**Weak, and one-directional**: a name on both sides would help; its absence
says nothing.

### 2.5 The player's words

10 transcript segments in 88.5 minutes; the only one near R2 is the game's own
voice ("Welcome to Sapienza 47. Next up, Francesca De Santis."). On this
session speech carries **nothing** about attempts. "Again" / "one more time"
are unmeasurable here and are not proposed.

## 3. What these can reach, and where they will be wrong

* **On the benchmark**, `mission_failed` + the picture rewinding finds both
  boundaries and no false one. That is two cases on one game, and with the
  owner's labels not yet recorded it is not a recall figure.
* **Where it will be wrong:**
  1. A game with no failure screen the OCR can read (or a silent reload from
     the pause menu, which is half of R2's boundary) -- the rewind alone
     catches long replays (R2) but not short ones (R1 is 10 windows).
  2. A game that respawns rather than rewinds (GTA, Grounded): the failure is
     real, the rewind never comes, and that conjunction correctly says "not an
     attempt" -- but a rule on the failure screen *alone* would cut there.
  3. A player who reloads and then goes somewhere else: no rewind, and the
     second attempt is not recognised as one. The cost is the edit as it is
     today, not a wrong cut.
  4. **Which** attempt succeeded: the rule says two attempts are related; it
     does not say the later one is the payoff. On R1 and R2 the later is the
     one that stood because the earlier ended in `MISSION FAILED`; a later
     attempt can fail too, and only the next boundary (or the mission's end)
     says it did not.
* **The dispositions** (PLAN.md: `PAYOFF`, `COMPRESS`, `DROP`, `KEEP`) need a
  *sameness* between failed attempts for `DROP` ("similar failed attempts in
  between"). The benchmark has no run of three attempts at one goal, so `DROP`
  and `KEEP` **cannot be measured on it at all**. Only `PAYOFF` (R2's second
  kill) and `COMPRESS` (R1's fight and death; R2's death) can.

## 4. What would be built, if the owner approves

The minimum PLAN.md asks for, from existing structures, nothing parallel:

1. **`Attempt`** -- a span between two boundaries, read from the stored
   `GameplayState`s (no new detector). A boundary is a `MISSION_FAILED`
   reading **followed by** a picture rewind starting within 30 s of the
   failure span's end. `RESTART` and `LOADING` alone are never boundaries.
2. **The rewind reading** -- the stable-lag measurement in 2.2, over the base
   frames the frames stage already extracted (no new decode; ~1,800 small
   reads on the benchmark). Its numbers (8-frame run, ≥ 6 windows, drift ≤ 2,
   30 s) come from one session and are to be reviewed after the owner's
   labels, as the vision threshold 0.60 was.
3. **Mission / Chapter** -- the span between a `game_intro` reading and the
   mission's end, used only to bound which attempts can be related. On the
   benchmark it is one mission; it is listed because the brief names it, and
   would carry no rule of its own until a session with two missions exists.
4. **Selection**: the later attempt of a related pair is `PAYOFF`; the earlier
   is `COMPRESS` -- its failure (the last event before the boundary) plus any
   reaction -- and never shown without that failure. `DROP` and `KEEP` are
   built only behind a test fixture, because the benchmark cannot exercise
   them. The mandatory safety rule from PLAN.md holds: compressing an attempt
   hands any event onset the kept attempt does not contain to the selection,
   the way P0.6's situations do.
5. **Acceptance on the benchmark**: R1 and R2 each appear as one attempt pair;
   the second Caruso kill is in the edit; no failed attempt is shown without
   its failure; 0 attempts are formed at the pause menus at 39:11 and 39:58;
   0 attempts on the two Grounded sessions.

## 5. Questions for the owner

1. **Is the picture rewind acceptable evidence?** It is not one of the four
   sources PLAN.md lists, and it is the only one that is not tied to a game's
   text. Without it, the boundary is `mission_failed` alone: right on this
   session, and wrong on every death in a respawning game.
2. **R1**: the edit opens on 61 s of an attempt that ends off-screen. Is the
   first attempt at the start of a session `COMPRESS` (fight + death, then the
   restart), or `DROP` (open on the attempt that counts)?
3. **`DROP` / `KEEP`**: build them now behind a fixture, or wait for a session
   with three attempts at one goal so they are measured before they are built?
</content>
</invoke>
