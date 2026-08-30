# DIRECTION — the owner's editing doctrine

> Delivered by the owner on 2026-08-28 as the master prompt for the AI
> editor. This file is its canonical home. It does not replace SPEC.md —
> SPEC says what the system *is*; this says what the finished video must
> *feel like*, and every editing subsystem answers to it.
>
> How it lands in practice: VAI's architecture keeps the model as the
> proposer and deterministic code as the decider, so most of these rules are
> enforced in code (budgets, cooldowns, guards, §51's asking, §39's clock)
> rather than pasted into a 7B prompt that could not hold 34 sections. The
> Director and Critic prompts carry the doctrine's distilled decision
> filter; the gap table at the bottom tracks what is not yet built.

## The decision filter (every edit answers at least one)

1. Does it increase viewer retention?
2. Does it improve understanding?
3. Does it increase emotional impact?
4. Does it emphasize an important gameplay event?
5. Does it improve pacing?
6. Does it strengthen the narrative?

If none apply, the edit does not happen. The best edit is not the edit with
the most effects; every decision has a purpose, or it is noise.

## The grammar

```
SETUP → BUILDUP → TENSION → PAYOFF → REACTION → NEXT MOMENT
```

never

```
MOMENT → RANDOM EFFECT → MOMENT → RANDOM EFFECT
```

The edit breathes. Not every highlight gets an effect; not every kill gets a
zoom; escalation is built (kill 1 plain → kill 4 heavy), and repeated
extremes cancel themselves.

## Standing rules already carried in code

| Doctrine section | Where it lives |
| --- | --- |
| Never invent events; UNCERTAIN over hallucination | §21/§80 evidence chain; the Director's index guardrail; creative-text validation |
| Understand before editing (timeline of intensity) | the full analysis pipeline; episodes (Phase B); §46 |
| Cut menus/loading/dead time/recorder chrome | dead-time (§36), frame states (§77), screen guard + recorder probe |
| Effects with reason, target, budget, no stacking wars | effects engine: triggers, budgets (6/min), realiser registry, §69 |
| Hook first, intro ≤3 s, outro short | narrative hook config (§37), story structure; §19's escalating montage open (2–3 rapid flashes, weakest first, §42-safe) in `hook._montage_extras` |
| Audio ducked, dialogue intelligible, no invented music | §72–§73 mix; "no local music found" note |
| Text sparingly, never over UI | caption engine collision rules; effects text guards |
| Self-critique before finalising | the Critic (Phase E) with apply-and-veto |
| Thumbnail = advertisement, ≤5 words, readable small | metadata/hooks.py style + research-backed strokes and scrim |
| Title from the strongest real event, no fake claims | metadata/generation + creative writer with evidence-only prompt |
| Capability awareness (never request unsupported effects) | realisable-set registry; §92 prompt/version registry |
| EDIT_PLAN as machine-readable output | the EDL (§40–§42) — it *is* the edit plan |
| Named highlight tiers on every score | moments/scoring.py `tier_for` — master/major/good/supporting on the ten-dimension score; a second scoring system beside the first would only raise which one is real |
| Daily production & publishing policy (owner, 2026-08-29) | services/daily_producer.py: production ledger + 02:00/10:00 Europe/Oslo clock, 1 long + 2 Reels caps, platform-side scheduled publishing, idempotent by the `daily_runs` mutex |
| Nothing publishes that nobody authorised | 51 | `publish_worker._respect_authorisation`: a YouTube publication must name `human`, `daily_policy` or `project_auto_publish`. `publishing.youtube.require_explicit_confirmation` shipped as `true` and was read by no code at all until P0 |
| Configuration may not promise what code does not do | — | `scripts/config_coverage.py` + `tests/unit/test_config_coverage.py`: every YAML leaf must have a consumer or a written reason. Forty-eight keys describing absent capabilities were deleted rather than allow-listed |
| Quality score 0–100 + uncertainty list | qa_worker `_doctrine_summary` — arithmetic over failures, warnings and every §95 note upstream stages attached, plus those notes verbatim |
| Pacing tiers driving cut length (§7) | screen_guard `_cap_for` over `scoring.tier_for`: master/major slabs cap at 45 s, good at 75, filler at 100 |
| Escalation ladders + per-effect cooldowns (§9) | planner: `escalation` rungs scale strength by same-type count; `cooldown_seconds` suppresses on record ("cooldown" in the rejected reasons) |
| Speed ramps inside one clip (§11) | timeline/retime.py: ≤3 trim/setpts pieces, atempo chained under 0.5, pitch pinned; spans stay the duration truth |
| Freeze frames (§12) | retime freeze = trim + tpad clone + concat, ffprobe-exact; downstream captions/overlays/stingers map through `output_offset` |
| SFX layer, voiced per moment (§14) | rendering/sfx.py synthesises impact/hit/whoosh/riser from FFmpeg arithmetic (royalty-free by construction); `sound_effect.params.voices` maps events-then-types to a voice at planning time; transition whooshes ride the §46 fade metadata; risers honour `lead_seconds` |
| Music intensity mapping (§15) | `find_music(…, mean_intensity=…)`: low/build/peak shelves picked by the story's own mean intensity; flat directories unchanged |
| Thumbnail composition (§22–23) | metadata/composition.py: VL `locate_subject` → rule-of-thirds crop, zoom ≤1.6, vignette; confidence rails, ships-as-extracted on any doubt |

## Gap table (doctrine asked, not yet built)

| Gap | Doctrine § | Note |
| --- | --- | --- |
| Detected game steering the editing grammar | 29 | detection-level adaptation is shipped (GameProfile: per-game event rules, suppressions, fusion); what is missing is the wire from the detected game to `EditingIntent` defaults — a horror game asking for slower pacing on its own |
| Named style presets | 32 | every §32 axis already lives on `EditingIntent` (pacing, effects level, captions, music, variety); a preset is a named bundle of those fields in the phrase layer, and none ship yet |
| **Viewer retention as a measured criterion** | filter #1 | the doctrine's *first* question, and nothing in the pipeline can answer it: the app requests one OAuth scope (`auth/youtube`), calls no analytics endpoint, and has no column anywhere for a view, a watch-second or a retention point. Every "for retention" decision shipped today — pacing tiers, effect budgets, cooldowns — is a hand-calibrated heuristic that has never been checked against an outcome. Named here because a gap nobody tracks is a gap nobody closes |
| Effect composition grammar (SETUP→BUILDUP→TENSION→PAYOFF→REACTION) | the grammar | V2-P4: `backend/emphasis/` + `config/compositions.yaml`. Anchors carry real timestamps, members sit at signed offsets around them with `depends_on` between roles, admission is atomic, and a sentence costs one gesture against the effects budget rather than one per member. Moved out of the gap table on 2026-08-30 |

Each gap graduates from this table by shipping with tests and a PLAN entry,
in the order the owner's feedback pulls them.
